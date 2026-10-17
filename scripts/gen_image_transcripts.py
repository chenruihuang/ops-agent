"""从截图生成器导出 OCR 转录（离线回放的 ground truth）。

为什么要有这一步
----------------
真实 OCR 需要联网下载模型，导致入库链路在离线环境下**完全不可验证**。
而不可验证的链路不能写进简历 —— 面试官一问"你怎么确认 OCR 那路是对的"就卡住。

截图是用 Chrome 渲染 HTML 得到的，所以**渲染前的那份文本就是像素里的文本**。
把它导出成转录文件，等价于"OCR 完美识别"的理想结果，可以：
    1. 让分块 / 索引 / 检索 / 评估在离线环境跑通并被单测覆盖
    2. 作为真实 OCR 的**对照基准**：跑 rapidocr 与转录做字符级 diff，
       差异率就是 OCR 错误率 —— 这个数字必须单独测，不能拿转录冒充

诚实红线：转录不是 OCR 结果。`OCR_PROVIDER=transcript` 时必须显式说明这一点，
不能拿"OCR 原文"的名头演示。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.gen_screenshots import SCENES  # noqa: E402

TRANSCRIPT_DIR = ROOT / "data" / "raw" / "images" / "transcripts"


def render_terminal_text(scene: dict) -> str:
    """终端截图里的可视文本：窗口标题 + 逐行输出。"""
    lines = [scene["title"]]
    lines.extend(text for _, text in scene["lines"])
    return "\n".join(lines)


def render_panel_text(scene: dict) -> str:
    """监控面板截图里的可视文本：顶栏 + 指标卡 + 表格 + 判读注释。"""
    lines = [
        "运维监控 · 值班总览",
        "prod · es-prod / ops-monitor",
        "2026-10-16 10:24 CST",
    ]
    for name, value, _color, note in scene["metrics"]:
        lines.append(f"{name}  {value}  {note}")
    rows = scene["table"]
    if rows:
        lines.append("  ".join(str(c) for c in rows[0]))
        for row in rows[1:]:
            lines.append("  ".join(str(c) for c in row))
    lines.append(scene.get("annotation", ""))
    return "\n".join(l for l in lines if l)


def build_transcript(scene: dict) -> str:
    if scene.get("kind") == "panel":
        return render_panel_text(scene)
    return render_terminal_text(scene)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出截图的 OCR 转录（离线回放用的 ground truth）")
    parser.add_argument("--out", default=str(TRANSCRIPT_DIR), help="转录输出目录")
    parser.add_argument("--only", help="只导出指定 image_id")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    scenes = [s for s in SCENES if not args.only or s["id"] == args.only]
    manifest: list[dict] = []

    for scene in scenes:
        text = build_transcript(scene)
        path = out_dir / f"{scene['id']}.txt"
        path.write_text(text + "\n", encoding="utf-8")

        line_count = len(text.splitlines())
        manifest.append(
            {
                "image_id": scene["id"],
                "transcript": path.name,
                "lines": line_count,
                "chars": len(text),
                "kind": scene.get("kind", "terminal"),
                # 显式标注来源性质，避免后续被误当作真实 OCR 结果引用
                "source": "rendered-text-replay",
            }
        )
        print(f"  [OK] {scene['id']}.txt  {line_count:3d} 行 / {len(text):5d} 字符")

    mpath = out_dir / "MANIFEST.json"
    mpath.write_text(
        json.dumps(
            {
                "total": len(manifest),
                "note": "渲染文本回放，非真实 OCR 输出；仅用于离线链路验证与 OCR 对照基准",
                "transcripts": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    total_chars = sum(m["chars"] for m in manifest)
    print()
    print(f"共导出 {len(manifest)} 份转录，合计 {total_chars} 字符")
    print(f"清单：{mpath.relative_to(ROOT).as_posix()}")
    if manifest:
        avg = total_chars / len(manifest)
        print(f"平均 {avg:.0f} 字符/张（OCR 路太短说明截图信息量不足，会影响多模态检索）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
