"""多模态「高质量模式」测试（Phase 4）。

全部用例用 FakeVisionCall 注入，**不访问网络**：
验证「模型输出的 Markdown -> 统一 Block」的解析、分页归因、逐页容错与失败语义。
"""
from __future__ import annotations

import pytest

from app.core import vision
from app.schema import BlockType

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


def _fake(mapping: dict[int, str] | None = None, default: str = "# 默认标题\n\n正文内容。"):
    """构造 vision_call：按页号返回预设 Markdown。"""
    def call(system: str, user: str, images: list[bytes]) -> str:
        # 从 user 文案里取页号，模拟真实调用
        page = 1
        for tok in user.replace("第", " ").replace("页", " ").split():
            if tok.isdigit():
                page = int(tok)
                break
        assert system, "应传入 system 提示词"
        assert images and images[0][:4] == b"\x89PNG", "应传入 PNG 图片字节"
        if mapping is not None:
            if page in mapping:
                return mapping[page]
            raise RuntimeError("模型调用超时（模拟）")
        return default
    return call


def test_vision_parses_markdown_into_blocks():
    md = "# 实验报告\n\n这是正文段落。\n\n| 组 | 强度 |\n| --- | --- |\n| A | 235 |\n"
    doc = vision.extract(
        FIXTURES / "sample.pdf",
        vision_call=_fake(default=md),
        dpi=72,
        pages=[1],
    )
    kinds = [b.type for b in doc.blocks]
    assert BlockType.HEADING in kinds
    assert BlockType.PARAGRAPH in kinds
    assert BlockType.TABLE in kinds, f"表格应被解析: {kinds}"


def test_vision_assigns_page_and_unique_ids():
    doc = vision.extract(
        FIXTURES / "sample.pdf",
        vision_call=_fake(),
        dpi=72,
        pages=[1, 2],
    )
    assert doc.blocks, "应产出块"
    ids = [b.id for b in doc.blocks]
    assert len(ids) == len(set(ids)), f"块 id 必须跨页唯一: {ids}"
    assert all(b.source and b.source.page in (1, 2) for b in doc.blocks)
    assert all(b.id.startswith("v") for b in doc.blocks)


def test_vision_confidence_below_one():
    """多模态结果给 0.85 而非满分，便于低质页进入复核清单。"""
    doc = vision.extract(
        FIXTURES / "sample.pdf",
        vision_call=_fake(),
        dpi=72,
        pages=[1],
    )
    assert all(b.confidence <= 0.85 for b in doc.blocks)


def test_vision_skips_failed_page_but_keeps_others():
    """单页失败只跳过该页，其余页照常产出。"""
    doc = vision.extract(
        FIXTURES / "sample.pdf",
        vision_call=_fake(mapping={2: "# 第二页标题\n\n内容。"}),  # 第 1 页失败
        dpi=72,
        pages=[1, 2],
    )
    assert doc.blocks, "第 2 页应成功"
    assert all(b.source.page == 2 for b in doc.blocks)
    assert any("第1页" in n and "失败" in n for n in doc.notes), doc.notes
    assert any("成功 1 页" in n for n in doc.notes), doc.notes


def test_vision_all_pages_failed_raises():
    with pytest.raises(RuntimeError) as ei:
        vision.extract(
            FIXTURES / "sample.pdf",
            vision_call=_fake(mapping={}),  # 全部失败
            dpi=72,
            pages=[1],
        )
    assert "高质量模式" in str(ei.value)


def test_vision_max_pages_limits_cost():
    """max_pages 生效，避免长文档按页烧钱。"""
    doc = vision.extract(
        FIXTURES / "sample.pdf",
        vision_call=_fake(),
        dpi=72,
        max_pages=1,
    )
    assert all(b.source.page == 1 for b in doc.blocks)


def test_estimate_pages():
    assert vision.estimate_pages(FIXTURES / "sample.pdf") >= 1


def test_atomic_table_from_vision():
    """模型还原的表格必须是原子块（不被 organize 拆开）。"""
    md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    doc = vision.extract(
        FIXTURES / "sample.pdf",
        vision_call=_fake(default=md),
        dpi=72,
        pages=[1],
    )
    table = next(b for b in doc.blocks if b.type == BlockType.TABLE)
    assert table.atomic is True
