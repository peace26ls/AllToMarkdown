"""schema.py 单元测试：统一中间表示的契约与行为。"""

import pytest

from app.schema import (
    ATOMIC_TYPES,
    Block,
    BlockType,
    Document,
    Source,
)


def test_block_default_id_generated():
    b = Block(type=BlockType.PARAGRAPH, data={"text": "hi"})
    assert b.id
    assert len(b.id) == 10


def test_atomic_auto_inferred_from_type():
    """表格/公式/列表/代码默认即原子块，无需显式指定 atomic。"""
    for t in ATOMIC_TYPES:
        b = Block(type=t, data={})
        assert b.atomic is True, f"{t} 应为原子块"


def test_non_atomic_types():
    for t in (BlockType.PARAGRAPH, BlockType.HEADING, BlockType.IMAGE, BlockType.RULE):
        b = Block(type=t, data={})
        assert b.atomic is False


def test_confidence_in_range():
    with pytest.raises(Exception):
        Block(type=BlockType.PARAGRAPH, data={}, confidence=1.5)
    with pytest.raises(Exception):
        Block(type=BlockType.PARAGRAPH, data={}, confidence=-0.1)


def test_review_items_low_confidence():
    doc = Document(
        source_file="x.docx",
        blocks=[
            Block(type=BlockType.PARAGRAPH, data={"text": "ok"}, confidence=0.9),
            Block(type=BlockType.TABLE, data={"rows": []}, confidence=0.5),
        ],
    )
    items = doc.review_items(threshold=0.7)
    assert len(items) == 1
    assert items[0].id == doc.blocks[1].id


def test_finalize_marks_needs_review_by_threshold():
    doc = Document(
        source_file="x.docx",
        blocks=[
            Block(type=BlockType.PARAGRAPH, data={"text": "ok"}, confidence=0.9),
            Block(type=BlockType.TABLE, data={"rows": []}, confidence=0.6),
        ],
    )
    doc.finalize(threshold=0.7)
    assert not doc.blocks[0].needs_review
    assert doc.blocks[1].needs_review


def test_source_defaults():
    b = Block(type=BlockType.PARAGRAPH, data={"text": "t"})
    assert b.source == Source()


def test_document_kind_constraint():
    with pytest.raises(Exception):
        Document(source_file="x", detected_kind="not-a-kind")
