"""PDF 提取器：PyMuPDF 文本 + 版式信息提取，基于字号/加粗/位置推断标题层级。

本模块从文字版 PDF 中提取文本，并利用版式特征（字号、字体是否加粗、行位置）
推断标题层级，输出统一的 Block 中间表示（app.schema.Document）。

对齐说明：本模块只消费 schema 中定义的 Source / Block / BlockType / Document，
不依赖任何 schema 之外的类。
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Optional

import pymupdf  # PyMuPDF

from app.schema import Block, BlockType, Document, Source


class _Line:
    """PDF 中的一行文本及其版式特征。"""

    __slots__ = ("text", "size", "bold", "x0", "x1", "y0", "y1", "page")

    def __init__(
        self, text: str, size: float, bold: bool,
        x0: float, x1: float, y0: float, y1: float, page: int,
    ):
        self.text = text
        self.size = size
        self.bold = bold
        self.x0 = x0
        self.x1 = x1
        self.y0 = y0
        self.y1 = y1
        self.page = page


def _expand_spans(page: "pymupdf.Page", page_no: int) -> list[_Line]:
    """把页面的所有 span 展开成行级文本，附带字号/加粗/横纵坐标。

    用整行最大的字号、以及"是否任一 span 加粗"来代表该行的版式特征。
    """
    lines: list[_Line] = []
    try:
        data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
    except Exception:
        return lines
    for block in data.get("blocks", []):
        if block.get("type") != 0:  # 0 = 文本块
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(s.get("text", "") for s in spans)
            if not text.strip():
                continue
            size = max((s.get("size", 0) or 0) for s in spans)
            bold = any(_is_bold(s.get("font", "")) for s in spans)
            xs = [s.get("bbox", [0, 0, 0, 0]) for s in spans] or [[0, 0, 0, 0]]
            x0 = min(b[0] for b in xs)
            x1 = max(b[2] for b in xs)
            y0 = min(b[1] for b in xs)
            y1 = max(b[3] for b in xs)
            lines.append(_Line(
                text=text, size=size, bold=bold,
                x0=x0, x1=x1, y0=y0, y1=y1, page=page_no,
            ))
    return lines


def _is_bold(font: str) -> bool:
    f = (font or "").lower()
    return any(k in f for k in ("bold", "black", "heavy", "semibold"))


def _text_width_guess(line: _Line) -> float:
    """估算行宽（用于居中判断）：中文全角≈字号，ASCII≈0.5*字号。"""
    width = 0.0
    for ch in line.text:
        width += line.size if ord(ch) > 0x2E7F else line.size * 0.5
    return width


def _looks_like_heading(line: _Line, body_size: float, page_width: float) -> Optional[int]:
    """根据版式推断该行是否是标题，返回标题级别（1-6），否则 None。

    推断依据（务实规则）：
    - 行文本短（标题不会是一整段）
    - 字号显著大于正文（约 1.15 倍以上）或明显加粗
    - 居中排布（文档主标题/章节标题常见）加分并提级
    - 用实际测量的行宽判断居中，比估算更可靠
    """
    text = line.text.strip()
    if not text or len(text) > 120:
        return None
    # 末尾是句读标点的多是正文句，不是标题
    if text.endswith(("。", ".", "，", ",", "；", ";", "：", ":")):
        return None
    size_ratio = line.size / body_size if body_size else 0
    # 居中：行实际宽度两侧留白近似相等（容忍 40pt 偏差）
    measured_width = line.x1 - line.x0
    left_margin = line.x0
    right_margin = page_width - line.x1
    is_centered = abs(left_margin - right_margin) < 40

    # 标题硬条件
    if size_ratio < 1.15 and not line.bold:
        return None

    if size_ratio >= 1.5:
        # 显著大字号（约 1.5 倍以上）必然是章/节标题：
        # 居中通常是一级（文档/章标题），不居中给二级（左对齐的常见章标题）
        return 1 if is_centered else 2
    if size_ratio >= 1.25:
        # 中等偏大字号：居中或加粗 -> 二级，否则三级
        return 2 if (is_centered or line.bold) else 3
    # 1.15 - 1.25：加粗短行 -> 三级/四级
    if line.bold and is_centered:
        return 2
    if line.bold and len(text) <= 60:
        return 4
    if line.bold:
        return 3
    return None


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def _smart_join(parts: list[str]) -> str:
    """拼接段落内的多行文本。

    决策规则：若上一行行尾字符与下一行行首字符都是 CJK（中文字符），
    直接相连（中文不需要空格分隔）；否则以单个空格衔接（如英文单词断裂处）。
    这能避免 PDF 换行导致的 "采 用 / 结 果" 之类凭空空格。
    """
    if not parts:
        return ""
    out = parts[0]
    for p in parts[1:]:
        prev_last = out[-1] if out else ""
        curr_first = p[0] if p else ""
        if _CJK_RE.match(prev_last) and _CJK_RE.match(curr_first):
            out += p
        else:
            out += " " + p
    return out


def _estimate_body_size(lines: list[_Line]) -> float:
    """估计正文基准字号：取出现频次最高的字号（去掉极小/极大的离群值）。"""
    sizes = [l.size for l in lines if l.size > 1]
    if not sizes:
        return 11.0
    counter = Counter(round(s * 2) / 2 for s in sizes)
    return max(counter, key=counter.get)


def extract(
    path: str | Path,
    *,
    mode: str = "standard",
    output_dir: Path | None = None,
) -> Document:
    """从文字版 PDF 提取内容为 Document 中间表示。

    output_dir 预留用于未来图片/资源落盘；文字版 PDF 当前不落盘图片，
    调用方可传入以保持与 docx 链路一致的签名。
    """
    doc = pymupdf.open(str(path))
    try:
        lines: list[_Line] = []
        for pno in range(doc.page_count):
            lines.extend(_expand_spans(doc[pno], pno + 1))
        body_size = _estimate_body_size(lines)
        page_width = doc[0].rect.width if doc.page_count else 612.0

        blocks: list[Block] = []
        current_para: list[str] = []
        current_page = 1
        para_start_idx = 0
        # 段落聚合用的辅助状态：上一正文行的底部 y（用于判断段间距）
        last_body_y1: float | None = None

        def flush_para() -> None:
            nonlocal current_para
            if current_para:
                # 中文行间不额外插空格（PDF 会按页宽硬断行，"采用"可能被拆成"采/用"，
                # 若简单拼空格会得到 "采 用"，这里按 CJK 边界智能拼接）
                text = _smart_join(current_para)
                if text:
                    blocks.append(Block(
                        id=f"p{para_start_idx}",
                        type=BlockType.PARAGRAPH,
                        data={"text": text},
                        source=Source(page=current_page, description=f"第{current_page}页 段落"),
                    ))
                current_para = []

        for idx, line in enumerate(lines):
            level = _looks_like_heading(line, body_size, page_width)
            if level is not None:
                flush_para()
                blocks.append(Block(
                    id=f"h{idx}",
                    type=BlockType.HEADING,
                    data={"level": level, "text": line.text.strip()},
                    source=Source(page=line.page, description=f"第{line.page}页 行{idx}"),
                ))
                current_page = line.page
                last_body_y1 = None  # 标题后重置，下一段从标题下方重新累计
                continue

            # 正文行
            if line.page != current_page or current_para and last_body_y1 is not None \
                    and line.y0 - last_body_y1 > line.size * 1.8:
                # 翻页，或与上一正文行的垂直间距显著大于行高 -> 段落边界
                flush_para()
                current_page = line.page
            current_para.append(line.text)
            if len(current_para) == 1:
                para_start_idx = idx
            last_body_y1 = line.y1
        flush_para()

        return Document(
            source_file=str(path),
            mode=mode,
            detected_kind="pdf_text",
            blocks=blocks,
        )
    finally:
        doc.close()
