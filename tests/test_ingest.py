"""ingest.py 单元测试：格式识别与扫描件预检。"""

from pathlib import Path

from app.core.ingest import DetectedKind, detect_ext, inspect

FIXTURES = Path(__file__).parent / "fixtures"


def test_detect_ext():
    assert detect_ext("a.DOCX") == ".docx"
    assert detect_ext("b.pdf") == ".pdf"
    assert detect_ext("c.txt") == ".txt"
    assert detect_ext("noext") == ""


def test_inspect_docx():
    info = inspect(FIXTURES / "sample.docx")
    assert info.detected_kind == DetectedKind.DOCX
    assert info.can_extract_standard is True


def test_inspect_text_pdf():
    info = inspect(FIXTURES / "sample.pdf")
    assert info.detected_kind == DetectedKind.PDF_TEXT
    assert info.can_extract_standard is True
    assert info.is_pdf is True
    assert info.pdf_text_page_ratio is not None


def test_inspect_scanned_pdf():
    info = inspect(FIXTURES / "scanned_sample.pdf")
    assert info.detected_kind == DetectedKind.PDF_SCANNED
    assert info.can_extract_standard is False
    assert info.is_pdf is True


def test_inspect_unknown_ext():
    info = inspect(FIXTURES / "sample.pdf.with_suffix")
    assert info.detected_kind == DetectedKind.UNKNOWN
    assert info.message
