"""PDF 提取链路测试。"""
from __future__ import annotations

from pathlib import Path

from app.core import extract_pdf
from app.core.ingest import DetectedKind, inspect
from app.schema import BlockType

FIXTURE = Path(__file__).parent / "fixtures" / "sample.pdf"
SCANNED = Path(__file__).parent / "fixtures" / "scanned_sample.pdf"


def test_pdf_text_extraction():
    if not FIXTURE.exists():
        return
    doc = extract_pdf.extract(FIXTURE)
    assert doc.detected_kind == "pdf_text"
    types = {b.type for b in doc.blocks}
    assert BlockType.HEADING in types
    assert BlockType.PARAGRAPH in types


def test_pdf_paragraph_not_overmerged():
    """同页多段不应被合并为一段（冒烟测试已发现过此问题）。"""
    if not FIXTURE.exists():
        return
    doc = extract_pdf.extract(FIXTURE)
    paras = [b for b in doc.blocks if b.type == BlockType.PARAGRAPH]
    assert len(paras) >= 2, "同一页的两个独立段落应被分别识别"
    assert "一段" in paras[0].data["text"] or "一段" in paras[1].data["text"]


def test_ingest_detects_pdf_text():
    if not FIXTURE.exists():
        return
    info = inspect(FIXTURE)
    assert info.detected_kind == DetectedKind.PDF_TEXT
    assert info.can_extract_standard


def test_ingest_detects_scanned():
    if not SCANNED.exists():
        return
    info = inspect(SCANNED)
    assert info.detected_kind == DetectedKind.PDF_SCANNED
    assert not info.can_extract_standard


def test_ingest_unknown_ext():
    info = inspect("foo.xyz")
    assert info.detected_kind == DetectedKind.UNKNOWN
