"""工具输出的文本格式化：Markdown 表格 + 截断提示。"""

from __future__ import annotations

import json
from typing import Any


def escape_cell(value: Any) -> str:
    """把单元格值转成 Markdown 表格里安全的一格。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, bytes):
        text = f"<{len(value)} bytes>"
    else:
        text = str(value)
    # 竖线与换行会破坏表格结构
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_table(columns: list[str], rows: list[list[Any]]) -> str:
    """把行列渲染成 Markdown 表格；空结果给出明确提示。"""
    if not rows:
        return "(空结果)"
    header = "| " + " | ".join(columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    body = [
        "| " + " | ".join(escape_cell(cell) for cell in row) + " |"
        for row in rows
    ]
    return "\n".join([header, sep, *body])


def truncate_text(text: str, limit: int, hint: str = "") -> tuple[str, bool]:
    """按字符上限截断文本，返回 (文本, 是否截断)。"""
    if len(text) <= limit:
        return text, False
    suffix = f"\n\n...（响应已截断，原长度 {len(text)} 字符）"
    if hint:
        suffix += f" {hint}"
    return text[:limit] + suffix, True
