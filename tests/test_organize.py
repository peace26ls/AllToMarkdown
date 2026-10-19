"""Phase 2：organize 层（分块策略 + LLM 增量指令整理）的单元测试。

所有测试都注入 **fake LLM 可调用对象**（`llm_call(system, user) -> dict`），
不依赖真实云端模型，从而验证分块、指令校验、应用与失败回退逻辑。
"""
from __future__ import annotations

import pytest

from app.core.organize import (
    chunk_document,
    organize_chunk,
    organize_document,
    parse_json_response,
)
from app.schema import Block, BlockType, Document


def _block(btype: BlockType, level: int | None = None, text: str = "", **data) -> Block:
    d = dict(data)
    if btype == BlockType.HEADING:
        d.setdefault("level", level or 1)
        d.setdefault("text", text or "标题")
    elif btype in (BlockType.PARAGRAPH, BlockType.CODE):
        d.setdefault("text", text)
    return Block(type=btype, data=d)


def _doc(*blocks: Block) -> Document:
    return Document(source_file="t.md", blocks=list(blocks))


def _chunk_of(doc: Document, index: int = 0):
    return chunk_document(doc)[index]


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------

def test_chunk_by_headings():
    doc = _doc(
        _block(BlockType.HEADING, level=1, text="第一章"),
        _block(BlockType.PARAGRAPH, text="段A"),
        _block(BlockType.HEADING, level=2, text="1.1"),
        _block(BlockType.PARAGRAPH, text="段B"),
        _block(BlockType.HEADING, level=1, text="第二章"),
        _block(BlockType.PARAGRAPH, text="段C"),
    )
    chunks = chunk_document(doc)
    assert len(chunks) == 3
    assert chunks[0].path == ["第一章"]
    assert [b.data.get("text") for b in chunks[0].blocks] == ["第一章", "段A"]
    assert chunks[1].path == ["第一章", "1.1"]
    assert chunks[2].path == ["第二章"]


def test_chunk_sibling_heading_closes_parent():
    """同级标题出现时，父标题的路径应弹出（1.1 之后 1.2 是同级）。"""
    doc = _doc(
        _block(BlockType.HEADING, level=1, text="章"),
        _block(BlockType.HEADING, level=2, text="1.1"),
        _block(BlockType.PARAGRAPH, text="A"),
        _block(BlockType.HEADING, level=2, text="1.2"),
        _block(BlockType.PARAGRAPH, text="B"),
    )
    chunks = chunk_document(doc)
    assert chunks[1].path == ["章", "1.1"]
    assert chunks[2].path == ["章", "1.2"]


def test_chunk_atomic_not_split_by_size():
    """即使 chunk 块数超限，表格（原子块）也绝不被截断。"""
    # 构造 1 个标题 + 3 个表格块
    doc = _doc(
        _block(BlockType.HEADING, level=1, text="章"),
        Block(type=BlockType.TABLE, data={"rows": [["a", "b"], ["1", "2"]]}),
        Block(type=BlockType.TABLE, data={"rows": [["c", "d"], ["3", "4"]]}),
        Block(type=BlockType.TABLE, data={"rows": [["e", "f"], ["5", "6"]]}),
    )
    # 每 chunk 最多 2 块，但表格块不可拆，所以 3 个表格必须留在同一 chunk
    chunks = chunk_document(doc, max_blocks_per_chunk=2)
    assert len(chunks) == 1
    table_blocks = [b for b in chunks[0].blocks if b.type == BlockType.TABLE]
    assert len(table_blocks) == 3


# ---------------------------------------------------------------------------
# JSON 解析
# ---------------------------------------------------------------------------

def test_parse_json_response_strips_fence():
    raw = '```json\n{"blocks":[],"flags":[]}\n```'
    assert parse_json_response(raw) == {"blocks": [], "flags": []}


def test_parse_json_response_invalid_raises():
    with pytest.raises(Exception):
        parse_json_response("not json at all")


# ---------------------------------------------------------------------------
# 指令校验
# ---------------------------------------------------------------------------

def test_validate_rejects_unknown_id():
    chunk = chunk_document(_doc(_block(BlockType.PARAGRAPH, text="A")))[0]
    parsed = {"blocks": [{"id": "nope", "action": "keep"}]}
    from app.core.organize import _validate_instructions

    errors = _validate_instructions(chunk, parsed)
    assert any("不存在的 id" in e for e in errors)


def test_organize_apply_drop():
    p1 = _block(BlockType.PARAGRAPH, text="保留")
    p2 = _block(BlockType.PARAGRAPH, text="删除")
    chunk = chunk_document(_doc(p1, p2))[0]

    def llm_call(system, user):
        return {"blocks": [{"id": p2.id, "action": "drop"}], "flags": []}

    result = organize_chunk(chunk, llm_call)
    assert [b.id for b in result] == [p1.id]


def test_organize_apply_keep_updates_data():
    p = _block(BlockType.PARAGRAPH, text="旧文本")
    chunk = chunk_document(_doc(p))[0]

    def llm_call(system, user):
        return {"blocks": [{"id": p.id, "action": "keep",
                            "data": {"text": "改过的标题"}}], "flags": []}

    result = organize_chunk(chunk, llm_call)
    assert result[0].data["text"] == "改过的标题"


def test_organize_merge_two_blocks():
    a = _block(BlockType.PARAGRAPH, text="前半")
    b = _block(BlockType.PARAGRAPH, text="后半")
    chunk = chunk_document(_doc(a, b))[0]

    def llm_call(system, user):
        return {"blocks": [{"id": a.id, "action": "merge", "with": b.id}], "flags": []}

    result = organize_chunk(chunk, llm_call)
    assert len(result) == 1
    assert result[0].data["text"] == "前半 后半"


def test_organize_fallback_on_retries_exhausted():
    """LLM 连续返回非法指令（引用不存在的 id），重试后仍失败 -> 保留原始块。"""
    p = _block(BlockType.PARAGRAPH, text="原文")
    chunk = chunk_document(_doc(p))[0]
    calls = {"n": 0}

    def llm_call(system, user):
        calls["n"] += 1
        return {"blocks": [{"id": "ghost", "action": "keep"}], "flags": []}

    result = organize_chunk(chunk, llm_call)
    assert [b.id for b in result] == [p.id]  # 回退到原始块
    assert calls["n"] >= 2  # 至少重试过一次


def test_organize_document_collects_fallback_meta():
    p1 = _block(BlockType.PARAGRAPH, text="A")

    def llm_call(system, user):
        raise RuntimeError("模拟网络错误")

    res = organize_document(_doc(p1), llm_call)
    assert res.blocks == [p1]          # 保留原始块
    assert res.chunks_unorganized == 1
    assert any(m.get("level") == "warn" for m in res.meta)


def test_organize_document_happy_path_no_meta():
    p = _block(BlockType.PARAGRAPH, text="A")

    def llm_call(system, user):
        return {"blocks": [], "flags": []}

    res = organize_document(_doc(p), llm_call)
    assert res.chunks_unorganized == 0
    assert res.blocks == [p]
