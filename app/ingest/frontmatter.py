"""YAML frontmatter 解析 —— 纯标准库实现。

为什么不用 PyYAML
-----------------
1. **依赖最小化**：入库脚本需要能在离线机器 / CI 里跑。少一个依赖就少一处踩坑。
2. **避免隐式类型陷阱**：PyYAML 会把 `updated: 2026-10-16` 自动转成 `datetime.date`，
   把 `kb_version: 1` 转成 int，把我们自己写的东西又"翻译"了一遍。
   存进向量库 payload 时 json.dumps 直接抛 TypeError —— 这个坑很隐蔽。
   我们只解析自己生成的受限子集（标量 + 字符串数组），显式控制类型反而更安全。

支持的语法（即 gen_corpus.py 生成的全部语法）：
    key: value
    key: "带 空格 的 值"
    key: [a, b, "c, 带逗号的"]
"""

from __future__ import annotations

import json
import re
from typing import Any

_FM_DELIM = re.compile(r"^---\s*$")
_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _split_list(inner: str) -> list[str]:
    """拆 `a, b, "c, d"` —— 逗号在引号内时不切。"""
    items: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ",":
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        items.append("".join(buf).strip())
    return [i for i in (_unquote(x) for x in items) if i != ""]


def coerce_scalar(raw: str) -> Any:
    """把值转成合适的 Python 类型。

    注意：**不做日期转换**。`2026-10-16` 保持字符串，
    否则写进 Qdrant payload 时无法 JSON 序列化。
    """
    value = raw.strip()
    if value == "":
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        # 先尝试 JSON（生成器写的就是合法 JSON 数组）
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (ValueError, TypeError):
            pass
        return _split_list(inner)
    value = _unquote(value)
    if _INT_RE.match(value):
        return int(value)
    if _FLOAT_RE.match(value):
        return float(value)
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """返回 (meta, body)。没有 frontmatter 时 meta 为空 dict，body 为原文。"""
    if not text.startswith("---"):
        return {}, text

    lines = text.splitlines()
    if not lines or not _FM_DELIM.match(lines[0]):
        return {}, text

    meta: dict[str, Any] = {}
    end = None
    for idx in range(1, len(lines)):
        if _FM_DELIM.match(lines[idx]):
            end = idx
            break
        line = lines[idx]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _KEY_LINE.match(line)
        if not m:
            # 不支持嵌套结构；遇到就跳过而不是崩，保证入库不因一行格式中断
            continue
        meta[m.group(1)] = coerce_scalar(m.group(2))

    if end is None:
        return {}, text

    body = "\n".join(lines[end + 1 :])
    return meta, body.lstrip("\n")


def dump_frontmatter(meta: dict[str, Any]) -> str:
    """反向序列化，供测试与调试用（键顺序保持插入顺序）。"""
    out = ["---"]
    for key, value in meta.items():
        if isinstance(value, list):
            out.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        elif isinstance(value, bool):
            out.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            out.append(f"{key}: {value}")
        else:
            out.append(f'{key}: "{value}"')
    out.append("---")
    return "\n".join(out)
