"""多模态入库：截图 → 可检索文本。

为什么截图必须变成文本才能进同一套检索
------------------------------------
用户贴一张报错截图时，检索系统拿到的是**文字查询**。要让截图能被检索到，
必须先把截图转成文本再入向量库。两条路径各有不可替代的作用，所以两块都进：

    路径 A · OCR 原文      —— 保留原始字符。错误码、堆栈行、PromQL 表达式
                              必须逐字精确，任何"意译"都会让检索失效。
    路径 B · 结构化描述    —— 补 OCR 拿不到的语义。OCR 出的是"Threads_connected
                              1741 max_connections 2000"这种词序列，
                              描述补上"MySQL 连接数逼近上限，疑似连接泄漏"，
                              才能被口语化提问召回。

Provider 抽象：为什么要做可插拔
-------------------------------
真实 OCR（RapidOCR/PaddleOCR）与多模态 LLM 都需要联网下载模型或调用 API。
这让"入库链路"在离线机器上完全不可验证 —— 而不可验证的链路不能写进简历。

所以这里把两个环节都抽象成 provider：
    - `transcript` 回放 provider：读取**渲染截图时的原始文本**，
      等价于"OCR 完美识别"的 ground truth。用于离线自测与回归。
      必须显式标注，不能冒充真实 OCR 结果。
    - `rapidocr` / `vision` 真实 provider：生产路径，需要联网。
切换 provider 只改一个环境变量，链路代码不变。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.ingest.chunker import MAX_CHARS, Chunk, pack_blocks, split_blocks

log = logging.getLogger(__name__)

# OCR 文本的切窗上限。同样倒推自 embedding 模型上下文（见 chunker.EMBED_MAX_CHARS）。
# 截图的 OCR 文本普遍在 500~1300 字符，**必须切窗**，否则尾部被静默截断 ——
# 而截图最后一行往往正是"判读/处置"的结论，截掉它就等于把最有价值的信息丢了。
OCR_MAX_CHARS = MAX_CHARS
OCR_OVERLAP = 60

# ---- 从截图文本里抽取"关键信号"的正则。运维截图的信息密度集中在这些模式上。----
_ERROR_CODE = re.compile(r"\b[A-Z]{2,6}-\d{3}-\d{3}\b")
_EXCEPTION = re.compile(
    r"\b(?:[a-zA-Z][\w.]*\.)?[A-Z][A-Za-z]*(?:Error|Exception|Timeout|[Ee]xhausted)\b"
)
_K8S_REASON = re.compile(
    r"\b(?:OOMKilled|CrashLoopBackOff|ImagePullBackOff|Evicted|BackOff|"
    r"Pending|Terminated|Unhealthy|FailedScheduling)\b"
)
# 单位必须紧贴数字，否则 "orderNo=SO2026...118 sku=..." 会把 sku 的 s 当单位吃掉
_NUMBER_UNIT = re.compile(
    r"\d+(?:\.\d+)?(?:%|ms|s\b|Gi|Mi|MB|GB|cores|条/分钟|条/小时)"
)
_CMD_LINE = re.compile(r"^\$\s*(.+)$")
_ANNOTATION = re.compile(r"^#\s*判读[:：]\s*(.+)$")
# 面板型截图的注释行没有 `# 判读：` 前缀，靠"结论箭头 + 文档引用"识别
_ANNOTATION_FALLBACK = re.compile(r"(?:->|→).*(?:(?:rb|svc|err|alert|pol|arch|oncall|obs|cmd|faq)-[a-z0-9-]{3,}|判读)")
_DOC_ID_IN_TEXT = re.compile(
    r"\b(?:rb|svc|err|alert|pol|arch|oncall|obs|cmd|faq)-[a-z0-9][a-z0-9-]{2,}\b"
)
_KNOWN_SERVICES = re.compile(
    r"\b(?:order-service|user-center|payment-gateway|notify-service|inventory-service|"
    r"search-service|gateway|auth-service|coupon-service|logistics-service|"
    r"report-service|config-center)\b"
)


# =====================================================================
# Provider 协议
# =====================================================================
class OcrProvider(Protocol):
    name: str

    async def ocr(self, image_path: Path) -> str: ...


class CaptionProvider(Protocol):
    name: str

    async def caption(self, image_path: Path, ocr_text: str) -> str: ...


# =====================================================================
# OCR providers
# =====================================================================
@dataclass
class TranscriptOcrProvider:
    """离线回放：读取渲染截图时的原文。

    这是 **ground truth 回放**，不是真实 OCR。它的作用是把入库链路
    从"依赖 OCR 模型"里解耦出来，让分块/索引/检索/评估可以被单元测试覆盖。
    真实 OCR 的字符错误率必须单独用 `rapidocr` provider 测，两者不能混用。
    """

    transcript_dir: Path
    name: str = "transcript"
    missing: list[str] = field(default_factory=list)

    async def ocr(self, image_path: Path) -> str:
        path = self.transcript_dir / f"{image_path.stem}.txt"
        if not path.exists():
            self.missing.append(image_path.stem)
            log.warning("缺少 OCR 转录文件: %s", path)
            return ""
        return path.read_text(encoding="utf-8").strip()


@dataclass
class RapidOcrProvider:
    """真实 OCR。需要 `rapidocr-onnxruntime`，首次运行会联网下载模型。"""

    name: str = "rapidocr"
    _engine: Any = None

    def _get(self) -> Any:
        if self._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:  # pragma: no cover - 取决于环境
                raise RuntimeError(
                    "未安装 rapidocr-onnxruntime。安装：pip install rapidocr-onnxruntime，"
                    "或改用 OCR_PROVIDER=transcript 走离线回放。"
                ) from exc
            self._engine = RapidOCR()
        return self._engine

    async def ocr(self, image_path: Path) -> str:
        import asyncio

        def run() -> str:
            result, _ = self._get()(str(image_path))
            if not result:
                return ""
            # result 形如 [[box, text, score], ...]，按识别框纵向位置排序还原阅读顺序
            rows = sorted(result, key=lambda r: (r[0][0][1], r[0][0][0]))
            return "\n".join(str(r[1]) for r in rows)

        return await asyncio.to_thread(run)


# =====================================================================
# Caption providers
# =====================================================================
@dataclass
class RuleCaptionProvider:
    """规则描述：从 OCR 文本里抽关键信号，拼成结构化现象描述。

    它不是"替代多模态模型"，而是**离线可验证的降级方案**。
    相比 LLM 描述的差别在于：它只做模式抽取，不做归因推理。
    真实场景下应换成 `vision` provider —— 但在没有 API 的环境里，
    有确定性的规则描述，远好过让整条链路跑不起来。
    """

    name: str = "rule"

    async def caption(self, image_path: Path, ocr_text: str) -> str:
        return build_rule_caption(ocr_text)


@dataclass
class VisionCaptionProvider:
    """多模态 LLM 结构化描述（生产路径）。"""

    api_key: str
    base_url: str
    model: str
    name: str = "vision"
    timeout: float = 60.0

    PROMPT = (
        "这是一张企业 IT 运维的报错截图（终端日志或监控面板）。"
        "请输出一段结构化中文描述，供检索系统使用。必须包含：\n"
        "1) 现象一句话总结（含具体服务名与异常类型）\n"
        "2) 出现的错误码 / 异常类 / K8s 事件原因（逐字保留）\n"
        "3) 关键数值与阈值\n"
        "4) 涉及的服务或组件\n"
        "5) 最可能的故障方向（一句话）\n"
        "只输出描述本身，不要客套话，不要 Markdown 标题。"
    )

    async def caption(self, image_path: Path, ocr_text: str) -> str:
        from openai import AsyncOpenAI

        b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=self.timeout)
        resp = await client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                    ],
                }
            ],
            temperature=0.1,
        )
        return (resp.choices[0].message.content or "").strip()


def build_rule_caption(ocr_text: str) -> str:
    """规则抽取 → 结构化描述。抽不到的字段明确写"未识别"，不猜。"""
    lines = [l.rstrip() for l in ocr_text.splitlines() if l.strip()]

    errors: list[str] = []
    commands: list[str] = []
    doc_ids: list[str] = []
    annotation = ""

    for line in lines:
        commands.extend(m.group(1).strip() for m in [_CMD_LINE.match(line)] if m)
        doc_ids.extend(_DOC_ID_IN_TEXT.findall(line))
        if not annotation:
            m = _ANNOTATION.match(line.strip())
            if m:
                annotation = m.group(1).strip()
        errors.extend(_ERROR_CODE.findall(line))
        errors.extend(_K8S_REASON.findall(line))
        errors.extend(_EXCEPTION.findall(line))

    if not annotation:
        # 面板截图没有 `# 判读：` 前缀，退化为识别"结论箭头 + 文档引用"的行
        for line in lines:
            if _ANNOTATION_FALLBACK.search(line):
                annotation = line.strip().lstrip("#").strip()
                break

    numbers: list[str] = []
    for line in lines:
        numbers.extend(_NUMBER_UNIT.findall(line))

    services = []
    for line in lines:
        services.extend(_KNOWN_SERVICES.findall(line))

    parts = [f"运维报错截图，包含 {len(lines)} 行文本。"]
    if services:
        parts.append(f"涉及服务：{'、'.join(_ordered_unique(services))}。")
    if errors:
        parts.append(f"关键错误码 / 异常：{'、'.join(_ordered_unique(errors)[:8])}。")
    if numbers:
        parts.append(f"关键数值与阈值：{'、'.join(_ordered_unique(numbers)[:8])}。")
    if commands:
        parts.append(f"涉及排查命令：{'；'.join(_ordered_unique(commands)[:3])}。")
    # 截图里出现的文档 id 是最强的检索线索：它们直接指向该看哪篇文档
    if doc_ids:
        parts.append(f"截图内引用的文档：{'、'.join(_ordered_unique(doc_ids)[:8])}。")
    if annotation:
        parts.append(f"现场判读：{annotation}")
    if len(parts) == 1:
        parts.append("未识别到明显的错误码、异常类或阈值，需人工确认。")
    return "".join(parts)


def _ordered_unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        i = i.strip()
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


# =====================================================================
# 图片 → Chunk
# =====================================================================
@dataclass(slots=True)
class ImageDoc:
    image_id: str
    file: str
    caption: str
    related_docs: list[str]
    kind: str
    width: int = 0
    height: int = 0
    chunks: list[Chunk] = field(default_factory=list)
    ocr_chars: int = 0
    caption_text: str = ""

    @property
    def related(self) -> list[str]:
        return self.related_docs


CAPTION_HEADER = "运维截图 · {caption}"
OCR_HEADER = "运维截图 OCR 原文 · {caption}"
_MAX_TAG_CHARS = 10          # "(12/15)" 类后缀的最坏长度，预留给预算


def split_with_header(
    text: str,
    *,
    header: str,
    max_chars: int,
    overlap: int,
) -> list[tuple[str, str]]:
    """按"最终文本"不超过 max_chars 切窗，返回 [(分窗后缀, 正文)]。

    分窗后缀（如 `(1/3)`）会加在 header 之后，所以预算里必须预留它的长度。
    这样得到的最终文本长度满足 `len(header) + len(tag) + 1 + len(part) <= max_chars`，
    从而保证不会被 tokenizer 静默截断。切完再断言一次，防止预算算错却无人发现。
    """
    budget = max(80, max_chars - len(header) - _MAX_TAG_CHARS - 1)
    parts = pack_blocks(split_blocks(text), budget, overlap)
    total = len(parts)
    tagged: list[tuple[str, str]] = [
        ("" if total == 1 else f"({i + 1}/{total})", p) for i, p in enumerate(parts)
    ]
    for tag, part in tagged:
        final_len = len(header) + len(tag) + 1 + len(part)
        if final_len > max_chars:  # pragma: no cover - 预算算错时立刻暴露而不是静默截断
            raise AssertionError(
                f"切窗超出上限：{final_len} > {max_chars}（header={len(header)}, tag={tag}）"
            )
    return tagged


def _emit_windowed(
    doc: ImageDoc,
    *,
    text: str,
    header_template: str,
    modality: str,
    kind: str,
    heading_prefix: str,
    id_prefix: str,
    caption: str,
    image_id: str,
    source_rel: str,
) -> None:
    """把一个文本通道按上下文上限切窗并落成 chunk。

    OCR 通道与描述通道共用它 —— "任何块都不得超出模型上下文"是通用不变式，
    不能因为"描述通常不长"就豁免：实测含命令列表的描述会涨到 560 字符，
    一样会被静默截断。
    """
    header = header_template.format(caption=caption)
    parts = split_with_header(
        text, header=header, max_chars=OCR_MAX_CHARS, overlap=OCR_OVERLAP
    )
    for i, (tag, part) in enumerate(parts):
        full = f"{header}{tag}\n{part}"
        doc.chunks.append(
            Chunk(
                doc_id=image_id,
                chunk_id=f"{image_id}#{id_prefix}{i:02d}",
                text=full,
                body=part,
                heading=f"{heading_prefix}{tag}",
                section_path=(heading_prefix,),
                category="image",
                title=caption or image_id,
                tags=("截图", kind),
                source=source_rel,
                modality=modality,
                kind=kind,
                char_len=len(full),
            )
        )


def build_image_chunks(
    image_path: Path,
    meta: dict,
    *,
    ocr_text: str,
    caption_text: str,
    source_rel: str,
) -> ImageDoc:
    """把一张截图的两路文本落成 chunk 集合。

    `doc_id` 直接用 manifest 的 image_id（如 `img-java-oom-heap`）——
    与评估集 notes 里标注的截图 id 同源，避免多一层映射。
    """
    image_id = str(meta.get("image_id") or image_path.stem)
    caption = str(meta.get("caption") or "")
    related = [str(r) for r in (meta.get("related_docs") or ())]
    kind = str(meta.get("kind") or "terminal")

    doc = ImageDoc(
        image_id=image_id,
        file=source_rel,
        caption=caption,
        related_docs=related,
        kind=kind,
        width=int(meta.get("width") or 0),
        height=int(meta.get("height") or 0),
        ocr_chars=len(ocr_text),
        caption_text=caption_text,
    )

    if ocr_text.strip():
        _emit_windowed(
            doc,
            text=ocr_text.strip(),
            header_template=OCR_HEADER,
            modality="image_ocr",
            kind="image_ocr",
            heading_prefix="OCR 原文",
            id_prefix="ocr",
            caption=caption,
            image_id=image_id,
            source_rel=source_rel,
        )

    if caption_text.strip():
        _emit_windowed(
            doc,
            text=caption_text.strip(),
            header_template=CAPTION_HEADER,
            modality="image_caption",
            kind="image_caption",
            heading_prefix="结构化描述",
            id_prefix="cap",
            caption=caption,
            image_id=image_id,
            source_rel=source_rel,
        )
    return doc


async def load_images(
    *,
    root: Path,
    manifest_path: Path,
    ocr: OcrProvider,
    captioner: CaptionProvider,
) -> list[ImageDoc]:
    """读取截图清单 → 逐张 OCR + 描述 → 落成 ImageDoc。"""
    if not manifest_path.exists():
        log.warning("截图清单不存在：%s", manifest_path)
        return []

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    docs: list[ImageDoc] = []
    for item in manifest.get("images", []):
        image_path = root / Path(item["file"]).name
        if not image_path.exists():
            # 清单里是相对仓库根的路径，兜底按 images 目录解析
            image_path = manifest_path.parent / Path(item["file"]).name
        if not image_path.exists():
            log.warning("截图缺失，跳过：%s", item.get("file"))
            continue

        ocr_text = await ocr.ocr(image_path)
        caption_text = await captioner.caption(image_path, ocr_text)
        doc = build_image_chunks(
            image_path,
            item,
            ocr_text=ocr_text,
            caption_text=caption_text,
            source_rel=Path(item["file"]).as_posix(),
        )
        docs.append(doc)
    return docs
