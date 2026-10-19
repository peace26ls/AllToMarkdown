"""中间表示 -> 最终 Markdown 渲染层。

约定：本模块只消费 schema.Document（Block 列表），本地生成 md 文本，
不接触任何外部服务，是校验「LLM 不接触最终 md」这条决策的最后一道关卡。
"""
from __future__ import annotations

import re
from typing import Iterable

from app.schema import Block, BlockType, Document


class RenderError(Exception):
    """渲染过程中出现的结构性错误。"""


def _escape_table_cell(text: str) -> str:
    """表格单元格转义：换行替换为 <br>，管道符转义。"""
    text = text.replace("\\", "\\\\")
    text = text.replace("|", "\\|")
    text = text.replace("\n", "<br>")
    return text


def _render_table(data: dict) -> str:
    rows: list[list[str]] = data.get("rows", [])
    if not rows:
        return ""
    caption = data.get("caption") or ""
    header = data.get("header") or ""
    align = data.get("align") or []

    # 统一列宽：以最长行决定列数
    ncols = max(len(r) for r in rows)
    header_row = header.split("\t") if header else (rows[0] if rows else [""] * ncols)
    while len(header_row) < ncols:
        header_row.append("")
    header_row = header_row[:ncols]

    lines: list[str] = []
    if caption:
        lines.append(f"**{caption}**")
    lines.append("| " + " | ".join(_escape_table_cell(c) for c in header_row) + " |")

    # 分隔行，支持对齐：---  / :--- / ---: / :---:
    sep_cells: list[str] = []
    for i in range(ncols):
        a = (align[i] if i < len(align) else "") or ""
        if a in ("center", "centre"):
            sep_cells.append(":---:")
        elif a == "right":
            sep_cells.append("---:")
        elif a == "left":
            sep_cells.append(":---")
        else:
            sep_cells.append("---")
    lines.append("| " + " | ".join(sep_cells) + " |")

    # 数据行：默认 rows[0] 即为表头（GFM/pandoc/mammoth 均把首行当表头），
    # 数据从第 2 行起；仅当 header 字段单独提供时才从第 1 行起。
    start = 0 if header else 1
    for r in rows[start:]:
        cells = [r[i] if i < len(r) else "" for i in range(ncols)]
        lines.append("| " + " | ".join(_escape_table_cell(c) for c in cells) + " |")
    return "\n".join(lines)


def _render_list(data: dict) -> str:
    items: list[str] = data.get("items", [])
    ordered: bool = data.get("ordered", False)
    if not items:
        return ""
    lines: list[str] = []
    n = 1
    for item in items:
        # 支持嵌套（item 首字符缩进数量决定层级）
        indent = 0
        text = item
        while text.startswith(("  ", "\t")):
            indent += 1
            text = text[2:] if text.startswith("  ") else text[1:]
        prefix = f"{n}. " if ordered else "- "
        lines.append("  " * indent + prefix + text.strip())
        if ordered:
            n += 1
    return "\n".join(lines)


def _render_code(data: dict) -> str:
    code = data.get("text", data.get("code", ""))
    lang = data.get("language") or ""
    return f"```{lang}\n{code.rstrip()}\n```"


def _render_formula(data: dict) -> str:
    latex = data.get("latex", "")
    inline = data.get("inline", False)
    if inline:
        return f"${latex}$"
    return f"$$\n{latex}\n$$"


def _render_image(data: dict, base_dir: str) -> str:
    path = data.get("path", "")
    if not path:
        return ""
    # 规整为相对引用路径（正斜杠，来自 tasks/{id}/ 的相对路径）
    p = path.replace("\\", "/").lstrip("/")
    caption = data.get("caption")
    alt = caption or "image"
    if not p.startswith("images/"):
        p = f"images/{p}"
    if caption:
        return f"![{alt}]({p})\n\n*{caption}*"
    return f"![{alt}]({p})"


def _render_block(block: Block, base_dir: str) -> str:
    if block.type == BlockType.HEADING:
        level = min(max(int(block.data.get("level", 1)), 1), 6)
        text = block.data.get("text", "").strip()
        return f"{'#' * level} {text}"
    if block.type == BlockType.PARAGRAPH:
        return block.data.get("text", "").strip()
    if block.type == BlockType.LIST:
        return _render_list(block.data)
    if block.type == BlockType.TABLE:
        return _render_table(block.data)
    if block.type == BlockType.CODE:
        return _render_code(block.data)
    if block.type == BlockType.FORMULA:
        return _render_formula(block.data)
    if block.type == BlockType.IMAGE:
        return _render_image(block.data, base_dir)
    if block.type == BlockType.RULE:
        return "---"
    raise RenderError(f"未知块类型: {block.type}")


def render_document(doc: Document, base_dir: str = "") -> str:
    """把 Document 的 blocks 渲染为完整 markdown 文本。

    base_dir 可选，用于规整图片路径（一般传 tasks/{id}/ 目录）。
    """
    parts: list[str] = []
    for block in doc.blocks:
        md = _render_block(block, base_dir)
        if md:
            parts.append(md.strip())
    # 块之间统一用空行分隔
    return "\n\n".join(parts) + "\n"


def blocks_to_markdown(blocks: Iterable[Block], base_dir: str = "") -> str:
    """便捷入口：直接给定 block 列表渲染 md。"""
    return render_document(Document(id="", source_file="", blocks=list(blocks)), base_dir)


def _dedupe_blank_lines(text: str) -> str:
    """压缩连续空行为单个空行。"""
    return re.sub(r"\n{3,}", "\n\n", text).strip()
