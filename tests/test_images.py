"""多模态入库测试。

覆盖三类容易出问题的地方：
    1. 规则描述的抽取准确性（抽错了不会报错，只会让检索变差）
    2. 切窗不变式（超出模型上下文会被**静默**截断）
    3. 离线回放 provider 的缺失处理（缺转录文件必须留痕，不能静默产出空块）
"""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from pathlib import Path

from app.ingest.chunker import EMBED_MAX_CHARS
from app.ingest.images import (
    OCR_HEADER,
    _NUMBER_UNIT,
    RapidOcrProvider,
    RuleCaptionProvider,
    TranscriptOcrProvider,
    build_image_chunks,
    build_rule_caption,
    load_images,
    split_with_header,
)

logging.disable(logging.CRITICAL)   # 被测代码的 warning 会淹没测试输出

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


TERMINAL_OCR = """order-service - kubectl logs -f (容器 3)
2026-10-16 09:41:20.455 WARN [GC-alarm] Old Gen used 3812MB/4096MB (93.1%)
2026-10-16 09:41:23.190 ERROR java.lang.OutOfMemoryError: Java heap space
  Reason:     OOMKilled
  Exit Code:  137
  memory:  5Gi
$ kubectl top pod order-service-7d9f8b6c4-x2k9n -n trade
# 判读：内存贴 limit + OOMKilled -> 处置见 rb-pod-oomkilled；历史泄漏先看 heap dump
"""

PANEL_OCR = """运维监控 · 值班总览
网关 5xx 比例  5.42%  阈值 0.5%
缓存命中率  31.7%  阈值 90%
时间  告警  级别  状态
10:21:08  alert-p0-cache-hit 缓存命中率骤降  P0  Firing
多告警级联 = 雪崩特征：缓存先崩 → DB 连接耗尽 → 网关 5xx。先断链止血，见 rb-full-link-timeout
"""


def main() -> int:
    print("=" * 70)
    print("多模态入库测试")
    print("=" * 70)

    # ---------- 规则描述抽取 ----------
    cap = build_rule_caption(TERMINAL_OCR)
    check("抽到服务名", "order-service" in cap, cap)
    check("抽到异常类", "OutOfMemoryError" in cap, cap)
    check("抽到 K8s 事件原因", "OOMKilled" in cap, cap)
    check("抽到含单位阈值", "3812MB" in cap and "93.1%" in cap, cap)
    check("抽到排查命令", "kubectl top pod" in cap, cap)
    check("抽到文档线索", "rb-pod-oomkilled" in cap, cap)
    check("抽到现场判读", "内存贴 limit" in cap, cap)
    # 单位必须紧跟数字：否则 "SO2026...118 sku=88213" 会把 sku 首字母当单位吃掉
    check("单位正则不跨空格误匹配",
          _NUMBER_UNIT.findall("SO20261016094102118 sku=88213") == [],
          str(_NUMBER_UNIT.findall("SO20261016094102118 sku=88213")))
    check("单位正则能吃到紧跟的阈值",
          _NUMBER_UNIT.findall("cost=3.002s limit 5Gi 命中率 93.1%") == ["3.002s", "5Gi", "93.1%"],
          str(_NUMBER_UNIT.findall("cost=3.002s limit 5Gi 命中率 93.1%")))
    check("描述里的数值都带单位",
          all(any(u in n for u in ("%", "s", "Gi", "Mi", "MB", "GB")) for n in
              [x for x in _NUMBER_UNIT.findall(TERMINAL_OCR)]),
          str(_NUMBER_UNIT.findall(TERMINAL_OCR)))
    check("未识别时明确说明而非编造",
          "未识别到明显" in build_rule_caption("随便一段没有信号的话"), build_rule_caption("随便一段"))

    panel_cap = build_rule_caption(PANEL_OCR)
    check("面板型截图能识别注释行（无 `# 判读：` 前缀）",
          "雪崩特征" in panel_cap, panel_cap)
    check("面板型截图能抽出多条文档线索",
          "alert-p0-cache-hit" in panel_cap and "rb-full-link-timeout" in panel_cap, panel_cap)

    # ---------- 切窗不变式 ----------
    long_text = "\n".join(f"第 {i} 行：这是一条比较长的日志内容，用于测试切窗边界是否被正确遵守。" for i in range(80))
    header = OCR_HEADER.format(caption="很长的截图说明" * 4)
    parts = split_with_header(long_text, header=header, max_chars=EMBED_MAX_CHARS, overlap=60)
    check("长文本被切成多窗", len(parts) > 1, str(len(parts)))
    worst = max(len(header) + len(tag) + 1 + len(p) for tag, p in parts)
    check(f"每窗最终长度都不超过 {EMBED_MAX_CHARS}", worst <= EMBED_MAX_CHARS, str(worst))
    check("分窗带序号后缀", parts[0][0] == f"(1/{len(parts)})", parts[0][0])

    # 单行超长（无中文标点、无换行）也必须被硬切
    giant = "x" * 3000
    parts2 = split_with_header(giant, header="H", max_chars=400, overlap=20)
    check("无标点超长文本被硬切", len(parts2) > 1 and max(len(p) for _, p in parts2) <= 400,
          str([len(p) for _, p in parts2][:4]))

    # ---------- 图片 → chunk ----------
    meta = {
        "image_id": "img-test-1",
        "caption": "测试截图 —— 与 rb-pod-oomkilled 对应",
        "related_docs": ["rb-pod-oomkilled", "svc-order-service"],
        "kind": "terminal",
        "width": 2480,
        "height": 1380,
    }
    doc = build_image_chunks(
        Path("img-test-1.png"), meta,
        ocr_text=TERMINAL_OCR,
        caption_text=build_rule_caption(TERMINAL_OCR),
        source_rel="data/raw/images/img-test-1.png",
    )
    check("doc_id 与 manifest 的 image_id 一致", doc.image_id == "img-test-1")
    check("每个 chunk 的 doc_id 都等于 image_id", all(c.doc_id == "img-test-1" for c in doc.chunks))
    check("同时产出 OCR 与描述两类块",
          {c.modality for c in doc.chunks} == {"image_ocr", "image_caption"},
          str({c.modality for c in doc.chunks}))
    check("OCR 块保留原文关键串", any("OutOfMemoryError" in c.body for c in doc.chunks))
    check("chunk_id 前缀区分通道",
          any(c.chunk_id.startswith("img-test-1#ocr") for c in doc.chunks)
          and any(c.chunk_id.startswith("img-test-1#cap") for c in doc.chunks),
          str([c.chunk_id for c in doc.chunks]))
    check("所有图片块都在上下文上限内",
          all(c.char_len <= EMBED_MAX_CHARS for c in doc.chunks),
          str([c.char_len for c in doc.chunks]))

    # ---------- 真实语料 ----------
    raw = Path("data/raw")
    if (raw / "images" / "MANIFEST.json").exists():
        async def run() -> list:
            return await load_images(
                root=raw / "images",
                manifest_path=raw / "images" / "MANIFEST.json",
                ocr=TranscriptOcrProvider(transcript_dir=raw / "images" / "transcripts"),
                captioner=RuleCaptionProvider(),
            )

        docs = asyncio.run(run())
        chunks = [c for d in docs for c in d.chunks]
        check("真实截图全部装载（20 张）", len(docs) == 20, str(len(docs)))
        check("OCR 转录未缺失", all(d.ocr_chars > 0 for d in docs),
              str([d.image_id for d in docs if d.ocr_chars == 0]))
        check("每张图至少产出 2 个块", all(len(d.chunks) >= 2 for d in docs),
              str([(d.image_id, len(d.chunks)) for d in docs if len(d.chunks) < 2]))
        check("真实语料下也无超长块",
              all(c.char_len <= EMBED_MAX_CHARS for c in chunks),
              f"超长 {sum(1 for c in chunks if c.char_len > EMBED_MAX_CHARS)} 块")
        check("每张图都有图谱边", all(d.related_docs for d in docs))

        # 缺转录必须留痕
        async def run_missing() -> tuple:
            provider = TranscriptOcrProvider(transcript_dir=raw / "images")
            out = await load_images(
                root=raw / "images",
                manifest_path=raw / "images" / "MANIFEST.json",
                ocr=provider,
                captioner=RuleCaptionProvider(),
            )
            return provider, out

        provider, out = asyncio.run(run_missing())
        check("缺转录文件时留痕而非静默", len(provider.missing) == 20, str(len(provider.missing)))
        check("缺 OCR 时仍产出描述块", sum(len(d.chunks) for d in out) == 20,
              str(sum(len(d.chunks) for d in out)))
    else:
        print("  [SKIP] 真实截图清单不存在")

    # ---------- provider 装配校验 ----------
    try:
        RapidOcrProvider()._get()
        print("  [SKIP] rapidocr 已安装，跳过缺依赖提示测试")
    except RuntimeError as exc:
        check("未装真实 OCR 时给出可操作提示", "transcript" in str(exc), str(exc))
    except Exception as exc:  # pragma: no cover
        check("未装真实 OCR 时抛出明确异常", False, f"{type(exc).__name__}: {exc}")

    print("-" * 70)
    print(f"通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
