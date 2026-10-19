"""docx 提取链路测试：走 mammoth HTML 解析路径（pandoc 可缺失）。"""
from __future__ import annotations

from pathlib import Path

from app.core import extract_docx
from app.schema import BlockType

FIXTURE = Path(__file__).parent / "fixtures" / "sample.docx"


def test_pandoc_available_detection():
    # 不应抛异常（是否有 pandoc 由环境决定）
    assert isinstance(extract_docx.pandoc_available(), bool)


def test_mammoth_html_table_restored():
    """mammoth 的 markdown 转换不输出表格，但 HTML 路径应还原出表格块。"""
    if not FIXTURE.exists():
        return  # fixtures 未就绪时跳过，避免 CI 失败
    doc = extract_docx.extract(FIXTURE, output_dir=FIXTURE.parent, prefer_pandoc=False)
    tables = [b for b in doc.blocks if b.type == BlockType.TABLE]
    assert tables, "应至少解析出一个表格块"
    rows = tables[0].data["rows"]
    assert ["方案", "速度", "准确性"] in rows
    assert ["方案A", "快", "高"] in rows


def test_docx_has_headings_and_paragraphs():
    if not FIXTURE.exists():
        return
    doc = extract_docx.extract(FIXTURE, output_dir=FIXTURE.parent, prefer_pandoc=False)
    types = {b.type for b in doc.blocks}
    assert BlockType.HEADING in types
    assert BlockType.PARAGRAPH in types
    assert BlockType.LIST in types
