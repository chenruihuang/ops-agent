"""Provider 装配。切换 OCR / 描述实现只改一个名字，入库链路代码不动。

刻意**不依赖 `app.core.config`**：配置文件走 pydantic，
而 build 阶段（切块 + 建索引）需要在最小依赖环境里可跑。
把 pydantic 拖进 import 链，整条入库链路就会在离线机器上直接 ImportError。

需要 API Key 的场景（vision provider）由调用方显式传进来，
而不是在这里反向 import 全局配置。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.ingest.images import (
    CaptionProvider,
    OcrProvider,
    RapidOcrProvider,
    RuleCaptionProvider,
    TranscriptOcrProvider,
    VisionCaptionProvider,
)

log = logging.getLogger(__name__)

TRANSCRIPT_SUBDIR = "transcripts"

OCR_CHOICES = ("transcript", "rapidocr")
CAPTION_CHOICES = ("rule", "vision")


def env_or(name: str, default: str) -> str:
    """读环境变量，缺失时用默认值。用于在无 pydantic 环境下也能配置。"""
    value = os.environ.get(name.upper())
    return value.strip() if value and value.strip() else default


def build_ocr_provider(name: str, *, raw_dir: Path) -> OcrProvider:
    name = (name or "transcript").strip().lower()
    if name == "rapidocr":
        log.info("OCR provider = rapidocr（真实 OCR，首次运行需联网下载模型）")
        return RapidOcrProvider()
    if name == "transcript":
        log.info("OCR provider = transcript（渲染文本回放，非真实 OCR；仅用于离线链路验证）")
        return TranscriptOcrProvider(transcript_dir=raw_dir / "images" / TRANSCRIPT_SUBDIR)
    raise ValueError(f"未知 OCR provider：{name}（可选 {OCR_CHOICES}）")


def build_caption_provider(
    name: str,
    *,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> CaptionProvider:
    name = (name or "rule").strip().lower()
    if name == "vision":
        if not api_key:
            raise ValueError("caption_provider=vision 需要 API Key，请通过参数或 DEEPSEEK_API_KEY 提供")
        log.info("Caption provider = vision（多模态 LLM：%s）", model or "默认模型")
        return VisionCaptionProvider(
            api_key=api_key, base_url=base_url or "https://api.deepseek.com", model=model
        )
    if name == "rule":
        log.info("Caption provider = rule（规则抽取，离线降级方案）")
        return RuleCaptionProvider()
    raise ValueError(f"未知 Caption provider：{name}（可选 {CAPTION_CHOICES}）")


def resolve_ocr_name(explicit: str | None = None) -> str:
    return (explicit or env_or("ocr_provider", "transcript")).lower()


def resolve_caption_name(explicit: str | None = None) -> str:
    return (explicit or env_or("caption_provider", "rule")).lower()
