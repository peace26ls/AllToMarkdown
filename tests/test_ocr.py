"""OCR 链路测试（Phase 4）。

原则：**不依赖真实 paddle**。用 FakeEngine 验证「行 -> 块」的聚合逻辑，
真实 OCR 的端到端验证走 ``RUN_REAL_OCR=1 pytest tests/test_ocr.py -k real``，
避免 CI / 日常回归被数 GB 依赖与模型下载拖住。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core import ocr
from app.schema import BlockType

FIXTURES = Path(__file__).parent / "fixtures"


class FakeEngine(ocr.OcrEngine):
    """返回预设行的假引擎，用于验证聚合逻辑。"""

    def __init__(self, pages: dict[int, list[tuple[str, tuple[float, float, float, float]]]]):
        self.pages = pages
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake"

    def recognize(self, image_bytes: bytes) -> list[ocr.OcrLine]:
        self.calls += 1
        # 用调用顺序当页号：extract 按页顺序调用
        page = self.calls
        return [
            ocr.OcrLine(text=t, score=0.95, box=box, page=0)
            for t, box in self.pages.get(page, [])
        ]


def _line(text: str, y: float, h: float = 20.0, x0: float = 100.0, x1: float = 500.0,
          page: int = 1, score: float = 0.95) -> ocr.OcrLine:
    return ocr.OcrLine(text=text, score=score, box=(x0, y, x1, y + h), page=page)


# ---------------------------------------------------------------------------
# 几何聚合
# ---------------------------------------------------------------------------

def test_rows_grouped_by_y():
    """同 y 的多个文本框合并成一行，行内按 x 排序。"""
    lines = [
        _line("左半行", y=100, x0=100, x1=300),
        _line("右半行", y=100, x0=320, x1=520),
        _line("第二行", y=140),
    ]
    rows = ocr._group_rows(lines, row_tol=8.0)
    assert len(rows) == 2
    assert [l.text for l in rows[0]] == ["左半行", "右半行"]
    assert rows[1][0].text == "第二行"


def test_smart_join_no_spurious_space():
    """PDF/OCR 断行拼接不应产生 "采 用" 这类凭空空格。"""
    lines = [_line("本次实验采", y=100), _line("用万能材料试验机", y=124)]
    rows = ocr._group_rows(lines, row_tol=6.0)
    blocks = ocr._rows_to_blocks(rows, page=1, body_h=20.0, page_width=1240.0, start_idx=0)
    # 两行间距 24 > body_h * 1.6 = 32? 否（24 < 32）-> 应合并为一段
    assert len(blocks) == 1
    assert blocks[0].data["text"] == "本次实验采用万能材料试验机"


def test_paragraph_split_on_large_gap():
    """垂直间距显著大于行高时切分段落。"""
    lines = [_line("第一段内容", y=100), _line("第二段内容", y=200)]
    rows = ocr._group_rows(lines, row_tol=6.0)
    blocks = ocr._rows_to_blocks(rows, page=1, body_h=20.0, page_width=1240.0, start_idx=0)
    assert len(blocks) == 2


def test_heading_inferred_from_box_height():
    """框高显著大于正文行高且文本短 -> 标题。"""
    lines = [
        _line("实验报告", y=100, h=36.0),      # 1.8x -> 一级（居中）
        _line("正文段落内容比较长一些", y=180, h=20.0),
    ]
    doc = ocr.lines_to_document(lines, source_file="x.pdf", engine_name="fake")
    kinds = [b.type for b in doc.blocks]
    assert kinds[0] == BlockType.HEADING
    assert doc.blocks[0].data["level"] in (1, 2, 3)
    assert kinds[1] == BlockType.PARAGRAPH


def test_punctuation_tail_is_not_heading():
    """以句号结尾的大字行不是标题（更像正文/图注）。"""
    lines = [_line("这是一句说明文字。", y=100, h=36.0)]
    doc = ocr.lines_to_document(lines, source_file="x.pdf", engine_name="fake")
    assert all(b.type != BlockType.HEADING for b in doc.blocks)


def test_confidence_reflects_ocr_score():
    """块置信度取行内最低分并打折，OCR 天然低于文字层。"""
    lines = [
        _line("识别很清楚", y=100, score=0.98),
        _line("这一行很模糊", y=124, score=0.62),
    ]
    doc = ocr.lines_to_document(lines, source_file="x.pdf", engine_name="fake")
    assert doc.blocks[0].confidence < 0.62
    assert doc.blocks[0].confidence > 0.5


def test_empty_lines_gives_note():
    doc = ocr.lines_to_document([], source_file="x.pdf", engine_name="fake")
    assert doc.blocks == []
    assert any("未识别到任何文本" in n for n in doc.notes)


# ---------------------------------------------------------------------------
# paddle 结果归一化（兼容 3.x dict 与 2.x list 两种形态）
# ---------------------------------------------------------------------------

def test_normalize_paddle_v3_dict():
    raw = [{
        "rec_texts": ["标题文字", "正文内容"],
        "rec_scores": [0.97, 0.91],
        "rec_polys": [[[10, 10], [110, 10], [110, 40], [10, 40]],
                      [[10, 60], [210, 60], [210, 90], [10, 90]]],
    }]
    lines = ocr._normalize_paddle_result(raw)
    assert [l.text for l in lines] == ["标题文字", "正文内容"]
    assert lines[0].score == 0.97
    assert lines[0].box == (10.0, 10.0, 110.0, 40.0)


def test_normalize_paddle_v2_list():
    raw = [[
        [[[0, 0], [100, 0], [100, 30], [0, 30]], ("旧版文本", 0.88)],
        [[[0, 50], [100, 50], [100, 80], [0, 80]], ("第二行", 0.9)],
    ]]
    lines = ocr._normalize_paddle_result(raw)
    assert [l.text for l in lines] == ["旧版文本", "第二行"]
    assert lines[0].score == 0.88


def test_normalize_paddle_empty_page():
    """2.x 对无文字页返回 [None]，不应抛异常。"""
    assert ocr._normalize_paddle_result([None]) == []


# ---------------------------------------------------------------------------
# 渲染与主入口
# ---------------------------------------------------------------------------

def test_render_pages_png():
    pdf = FIXTURES / "sample.pdf"
    if not pdf.exists():
        pytest.skip("缺少夹具 sample.pdf")
    out = ocr.render_pages(pdf, dpi=72)
    assert out, "应至少渲染一页"
    for pno, png in out:
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "应为 PNG 字节"


def test_render_pages_respects_page_filter():
    pdf = FIXTURES / "sample.pdf"
    if not pdf.exists():
        pytest.skip("缺少夹具 sample.pdf")
    out = ocr.render_pages(pdf, dpi=72, pages=[1])
    assert len(out) == 1 and out[0][0] == 1


def test_extract_with_injected_engine():
    """注入假引擎跑通 extract 全链路（不触碰 paddle）。"""
    pdf = FIXTURES / "sample.pdf"
    if not pdf.exists():
        pytest.skip("缺少夹具 sample.pdf")
    engine = FakeEngine(pages={
        1: [("标题ABC", (100, 100, 400, 140)), ("正文一行", (100, 200, 400, 220))],
    })
    doc = ocr.extract(pdf, engine=engine, dpi=72)
    assert engine.calls >= 1
    assert doc.detected_kind == "pdf_scanned"
    assert doc.blocks, "应产出至少一个块"


def test_ocr_available_returns_tuple():
    ok, reason = ocr.ocr_available()
    assert isinstance(ok, bool)
    assert isinstance(reason, str) and reason


@pytest.mark.skipif(os.getenv("RUN_REAL_OCR") != "1", reason="真实 OCR 需 paddle + 模型下载，默认跳过")
def test_real_ocr_end_to_end():
    """真实 PaddleOCR 冒烟：RUN_REAL_OCR=1 时执行。"""
    from PIL import Image, ImageDraw, ImageFont

    font_path = next(
        (p for p in ("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/msyh.ttc")
         if os.path.exists(p)), None
    )
    if not font_path:
        pytest.skip("缺少中文字体")

    img = Image.new("RGB", (1240, 600), "white")
    ImageDraw.Draw(img).text((120, 120), "测试文档标题", font=ImageFont.truetype(font_path, 56), fill="black")
    ImageDraw.Draw(img).text((120, 260), "这是一段用于验证 OCR 的正文内容。",
                             font=ImageFont.truetype(font_path, 32), fill="black")
    pdf_path = Path(os.environ.get("TMP", ".")) / "alltomd_ocr_smoke.pdf"
    img.save(pdf_path, format="PDF")

    ok, reason = ocr.ocr_available()
    if not ok:
        pytest.skip(f"paddleocr 不可用: {reason}")

    doc = ocr.extract(pdf_path, dpi=150)
    text = " ".join(b.data.get("text", "") for b in doc.blocks)
    assert "测试文档标题" in text or "测试" in text, f"识别结果异常: {text}"
