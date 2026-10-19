"""格式识别与扫描件预检。

职责：
1. 根据文件扩展名/MIME 判断文档类型（docx / pdf / unknown）。
2. 对 PDF 做「文字密度预检」：区分文字版 PDF 与扫描件。
3. 返回统一的 ingest 结果，供上层决定走哪条提取链路、用什么模式。

设计说明（相对交接文档的优化点）：
- 扫描件判定阈值做成可配置（环境变量 PDF_TEXT_DENSITY_THRESHOLD），
  并且按页统计后取「文字页占比」，避免整份 PDF 因个别空洞页被判为扫描件。
"""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class DetectedKind(str, Enum):
    DOCX = "docx"
    PDF_TEXT = "pdf_text"
    PDF_SCANNED = "pdf_scanned"
    UNKNOWN = "unknown"


class DocInspect(BaseModel):
    """ingest 阶段的输出：格式识别 + 模式判定的结论。"""

    source_file: str
    ext: str
    detected_kind: DetectedKind
    # PDF 才有：各页文字密度、文字页占比，用于日志与 UI 提示
    pdf_page_stats: list[dict] = Field(default_factory=list)
    pdf_text_page_ratio: float | None = None
    # 是否为「可安全走 standard 模式」的文字版 PDF
    can_extract_standard: bool = False
    message: str = ""

    @property
    def is_pdf(self) -> bool:
        return self.detected_kind in (
            DetectedKind.PDF_TEXT,
            DetectedKind.PDF_SCANNED,
        )


def get_text_density_threshold() -> int:
    """扫描件预检阈值：每页字符数低于该值视为图页。可配置。"""
    return int(os.getenv("PDF_TEXT_DENSITY_THRESHOLD", "80"))


def detect_ext(path: str | Path) -> str:
    """返回小写扩展名（含点），无扩展名返回 ''。"""
    return Path(path).suffix.lower()


def inspect(path: str | Path) -> DocInspect:
    """识别文档类型，并对 PDF 做扫描件预检。

    返回 DocInspect；任何异常（文件不存在、非支持类型）也以
    detected_kind=UNKNOWN + message 形式返回，由上层决定报错文案。
    """
    src = str(path)
    ext = detect_ext(src)

    if ext == ".docx":
        return DocInspect(
            source_file=src, ext=ext, detected_kind=DetectedKind.DOCX,
            can_extract_standard=True, message="docx 文档，走 pandoc/mammoth 链路",
        )

    if ext == ".pdf":
        return _inspect_pdf(src)

    return DocInspect(
        source_file=src, ext=ext, detected_kind=DetectedKind.UNKNOWN,
        message=f"不支持的文件类型 '{ext or '(无扩展名)'}'（支持 .docx / .pdf）",
    )


def _inspect_pdf(path: str) -> DocInspect:
    """PDF 文字密度预检。"""
    try:
        import pymupdf  # PyMuPDF
    except ImportError as exc:  # pragma: no cover
        return DocInspect(
            source_file=path, ext=".pdf", detected_kind=DetectedKind.UNKNOWN,
            message="PyMuPDF 未安装，无法解析 PDF（pip install pymupdf）",
        )

    threshold = get_text_density_threshold()
    page_stats: list[dict] = []
    text_pages = 0
    total_pages = 0

    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        return DocInspect(
            source_file=path, ext=".pdf", detected_kind=DetectedKind.UNKNOWN,
            message=f"PDF 打开失败：{exc}",
        )

    with doc:
        total_pages = doc.page_count
        for i, page in enumerate(doc):
            # text 提取纯文字；若几乎无文字，视为图页/扫描页
            text = page.get_text("text") or ""
            char_count = len(text.strip())
            kind = (
                DetectedKind.PDF_TEXT
                if char_count >= threshold
                else DetectedKind.PDF_SCANNED
            )
            if kind == DetectedKind.PDF_TEXT:
                text_pages += 1
            page_stats.append(
                {"page": i + 1, "chars": char_count, "kind": kind.value}
            )

    ratio = (text_pages / total_pages) if total_pages else 0.0
    is_text_pdf = ratio >= 0.6  # 60% 以上页面有文字才算文字版
    detected = DetectedKind.PDF_TEXT if is_text_pdf else DetectedKind.PDF_SCANNED

    msg = (
        f"文字版 PDF：{text_pages}/{total_pages} 页有文字层"
        if is_text_pdf
        else f"扫描件/图版 PDF：仅 {text_pages}/{total_pages} 页有文字层，"
        "请使用 OCR 或高质量模式"
    )
    return DocInspect(
        source_file=path,
        ext=".pdf",
        detected_kind=detected,
        pdf_page_stats=page_stats,
        pdf_text_page_ratio=round(ratio, 3),
        can_extract_standard=is_text_pdf,
        message=msg,
    )
