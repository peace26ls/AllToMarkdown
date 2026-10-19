"""渲染层测试：Block -> Markdown。"""
from __future__ import annotations

from app.core.render import render_document
from app.schema import Block, BlockType, Document


def _doc(blocks: list[Block]) -> Document:
    return Document(source_file="t", blocks=blocks)


def test_heading_levels_and_text():
    doc = _doc([
        Block(type=BlockType.HEADING, data={"level": 1, "text": "标题一"}),
        Block(type=BlockType.HEADING, data={"level": 3, "text": "三级标题"}),
    ])
    out = render_document(doc)
    assert "# 标题一" in out
    assert "### 三级标题" in out


def test_clamps_heading_level():
    doc = _doc([Block(type=BlockType.HEADING, data={"level": 9, "text": "x"})])
    assert "###### x" in render_document(doc)


def test_table_with_header_row():
    doc = _doc([Block(type=BlockType.TABLE, data={
        "rows": [["方案", "速度"], ["A", "快"]],
        "caption": "",
    })])
    out = render_document(doc)
    lines = out.splitlines()
    assert lines[0] == "| 方案 | 速度 |"
    assert "| --- |" in out
    assert "| A | 快 |" in out


def test_table_cell_escaping():
    doc = _doc([Block(type=BlockType.TABLE, data={
        "rows": [["a|b", "x\ny"]],
        "caption": "",
    })])
    out = render_document(doc)
    assert r"a\|b" in out
    assert "<br>" in out


def test_list_ordered_and_plain():
    doc = _doc([Block(type=BlockType.LIST, data={"items": ["一", "二"], "ordered": True})])
    out = render_document(doc)
    assert "1. 一" in out and "2. 二" in out
    doc2 = _doc([Block(type=BlockType.LIST, data={"items": ["a", "b"], "ordered": False})])
    assert "- a" in render_document(doc2)


def test_code_block():
    doc = _doc([Block(type=BlockType.CODE, data={"text": "print(1)", "language": "python"})])
    assert "```python\nprint(1)\n```" in render_document(doc)


def test_rule_and_paragraph():
    doc = _doc([
        Block(type=BlockType.PARAGRAPH, data={"text": "正文"}),
        Block(type=BlockType.RULE, data={}),
    ])
    out = render_document(doc)
    assert "正文" in out and "---" in out
