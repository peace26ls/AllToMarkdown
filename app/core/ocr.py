"""扫描件 OCR 链路（Phase 4）。

把「没有文字层的 PDF」转成与文字版 PDF 同构的 Block 中间表示，
下游的 organize / render / 前端完全无感。

设计要点：
- **引擎抽象**：``OcrEngine`` 是协议类，PaddleOCR 只是当前唯一的真实实现。
  这样即便 paddle 装不上（体积大、Windows 易踩坑），其余代码仍可测试与运行，
  未安装时给出清晰降级提示而不是 ImportError 崩溃。
- **页面渲染走 PyMuPDF**：已有依赖即可把 PDF 页渲染成 PNG，
  不需要额外装 poppler / pdf2image / ghostscript。
- **几何聚合**：OCR 只给「文本框 + 文字 + 置信度」，没有字号/加粗。
  因此用**框高**代理字号做标题推断，用**垂直间距**做段落切分，
  并复用 extract_pdf 的 CJK 智能拼接，避免 "采 用" 这类凭空空格。
- **置信度如实下调**：OCR 天生不如文字层可靠，块级置信度取行内最低分并打折，
  低分块会自动进复核清单（meta.json / 前端校对列表）。
"""
from __future__ import annotations

import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import pymupdf  # PyMuPDF：页面渲染 + 打开 PDF

from app.schema import Block, BlockType, Document, Source
from app.core.extract_pdf import _smart_join  # 复用 CJK 断行拼接

# 渲染 DPI：150 足够 OCR 识别（更高只增耗时与内存），可用环境变量覆盖
DEFAULT_DPI = 150


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class OcrLine:
    """OCR 识别出的一行文本及其几何/质量信息。"""

    text: str
    score: float
    # 文本框（像素坐标）：(x0, y0, x1, y1)
    box: tuple[float, float, float, float]
    page: int


class OcrEngine(ABC):
    """OCR 引擎协议：输入一张图的 PNG 字节，输出若干行文本。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """引擎标识，写入 notes / meta 便于追溯。"""

    @abstractmethod
    def recognize(self, image_bytes: bytes) -> list[OcrLine]:
        """识别单张图片，返回行列表（无需 page，由调用方回填）。"""


# ---------------------------------------------------------------------------
# PaddleOCR 实现
# ---------------------------------------------------------------------------

class PaddleOcrEngine(OcrEngine):
    """PaddleOCR 封装（懒加载）。

    - 构造时不导入 paddle（导入耗时数百毫秒且会拉起模型配置），
      真正识别时才初始化，避免 Web 启动被拖慢。
    - 兼容 paddleocr 3.x（返回 dict，键为 rec_texts/rec_scores/rec_polys）
      与 2.x（返回 list[[poly, (text, score)]]）两种结果形态。
    """

    def __init__(self, lang: str = "ch", *, enable_orientation: bool = False):
        self.lang = lang
        self.enable_orientation = enable_orientation
        self._engine = None

    def _build(self):
        if self._engine is None:
            from paddleocr import PaddleOCR  # 延迟导入：未安装时在此抛出可捕获的异常

            # 3.x / 2.x 构造签名不同，逐级回退；每级都先尝试带 enable_mkldnn=False，
            # 因为 paddlepaddle 3.3.x 在 Windows CPU 上默认启用 oneDNN 会抛
            # NotImplementedError（PIR 属性未支持），必须显式关掉才能推理。
            attempts = (
                dict(
                    lang=self.lang,
                    enable_mkldnn=False,
                    use_doc_orientation_classify=self.enable_orientation,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                ),
                dict(
                    lang=self.lang,
                    enable_mkldnn=False,
                ),
                dict(use_angle_cls=self.enable_orientation, lang=self.lang),  # 2.x
            )
            last_err: Exception | None = None
            for kwargs in attempts:
                try:
                    self._engine = PaddleOCR(**kwargs)
                    break
                except TypeError as exc:  # 该版本不接受这些关键字 -> 换下一档
                    last_err = exc
                    continue
            if self._engine is None:
                raise RuntimeError(f"PaddleOCR 初始化失败（版本不兼容）: {last_err}")
        return self._engine

    @property
    def name(self) -> str:
        return f"paddleocr:{self.lang}"

    def recognize(self, image_bytes: bytes) -> list[OcrLine]:
        import numpy as np
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        arr = np.asarray(img)

        engine = self._build()
        # 3.x 推荐 predict，2.x 只有 ocr
        raw = engine.predict(arr) if hasattr(engine, "predict") else engine.ocr(arr)
        return _normalize_paddle_result(raw)


def _normalize_paddle_result(raw) -> list[OcrLine]:
    """把 paddleocr 各版本/形态的结果归一化成 OcrLine 列表。

    3.x predict 返回可迭代的 dict-like：{"rec_texts": [...], "rec_scores": [...],
    "rec_polys"/"rec_boxes": [...]}；2.x ocr 返回 [[poly, (text, score)], ...]，
    也可能是 [None]（该页无文字）。
    """
    lines: list[OcrLine] = []

    # 3.x：单个结果对象或结果列表
    items: list = []
    if isinstance(raw, dict) or hasattr(raw, "get"):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        # 2.x 形态：[[ [poly, (text, score)], ... ]]（外层按页）
        if raw and isinstance(raw[0], (list, tuple)) and raw[0] and isinstance(raw[0][0], (list, tuple)):
            items = list(raw[0]) if isinstance(raw[0][0], (list, tuple)) and len(raw[0][0]) == 2 else [raw]
        else:
            items = list(raw)

    for it in items:
        if it is None:
            continue
        if isinstance(it, dict) or hasattr(it, "get"):
            try:
                texts = list(it.get("rec_texts") or [])
                scores = list(it.get("rec_scores") or [])
                polys = list(it.get("rec_polys") or it.get("rec_boxes") or [])
            except Exception:  # noqa: BLE001 形态不符，跳过该结果
                continue
            for i, text in enumerate(texts):
                if not str(text).strip():
                    continue
                score = float(scores[i]) if i < len(scores) else 0.0
                box = _bbox_from_poly(polys[i]) if i < len(polys) else (0.0, 0.0, 0.0, 0.0)
                lines.append(OcrLine(text=str(text).strip(), score=score, box=box, page=0))
            continue

        # 2.x：元素形如 [poly, (text, score)]
        if isinstance(it, (list, tuple)) and len(it) >= 2:
            poly, rec = it[0], it[1]
            text, score = (rec[0], rec[1]) if isinstance(rec, (list, tuple)) else (str(rec), 0.0)
            if not str(text).strip():
                continue
            lines.append(OcrLine(
                text=str(text).strip(),
                score=float(score or 0.0),
                box=_bbox_from_poly(poly),
                page=0,
            ))
    return lines


def _bbox_from_poly(poly) -> tuple[float, float, float, float]:
    """多点多边形 / ndarray / 直写 bbox 统一成 (x0, y0, x1, y1)。"""
    try:
        pts = [[float(p[0]), float(p[1])] for p in poly]
    except Exception:  # noqa: BLE001 非点序列，尝试当成 [x0,y0,x1,y1]
        try:
            flat = [float(v) for v in poly]
        except Exception:  # noqa: BLE001
            return (0.0, 0.0, 0.0, 0.0)
        if len(flat) >= 4:
            return (flat[0], flat[1], flat[2], flat[3])
        return (0.0, 0.0, 0.0, 0.0)
    if not pts:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def ocr_available() -> tuple[bool, str]:
    """OCR 是否可用（供 API / UI 决定是否开放扫描件入口）。

    Returns:
        (是否可用, 说明文案)
    """
    try:
        import paddleocr  # noqa: F401
    except Exception as exc:  # noqa: BLE001 未安装或依赖残缺
        return False, f"未安装 paddleocr（{type(exc).__name__}）：pip install paddlepaddle paddleocr"
    return True, "paddleocr 可用"


# ---------------------------------------------------------------------------
# PDF 页面渲染
# ---------------------------------------------------------------------------

def render_pages(
    path: str | Path,
    *,
    dpi: int = DEFAULT_DPI,
    pages: Sequence[int] | None = None,
) -> list[tuple[int, bytes]]:
    """把 PDF 页面渲染成 PNG 字节。

    Args:
        path: PDF 路径。
        dpi: 渲染分辨率，默认 150。
        pages: 指定页号（1-based，None 表示全部）。

    Returns:
        [(页号, png_bytes), ...]
    """
    out: list[tuple[int, bytes]] = []
    doc = pymupdf.open(str(path))
    try:
        total = doc.page_count
        targets = list(pages) if pages else list(range(1, total + 1))
        for pno in targets:
            if pno < 1 or pno > total:
                continue
            pix = doc[pno - 1].get_pixmap(dpi=dpi)
            out.append((pno, pix.tobytes("png")))
    finally:
        doc.close()
    return out


# ---------------------------------------------------------------------------
# 几何聚合：行 -> 段落 / 标题
# ---------------------------------------------------------------------------

def _median_height(lines: list[OcrLine]) -> float:
    hs = [l.box[3] - l.box[1] for l in lines if l.box[3] > l.box[1]]
    return statistics.median(hs) if hs else 12.0


def _group_rows(lines: list[OcrLine], row_tol: float) -> list[list[OcrLine]]:
    """把 OCR 行按 y 方向聚类成「视觉行」（同一行的多个文本框会被合并）。"""
    if not lines:
        return []
    ordered = sorted(lines, key=lambda l: (l.box[1], l.box[0]))
    rows: list[list[OcrLine]] = []
    cur: list[OcrLine] = [ordered[0]]
    cur_y = ordered[0].box[1]
    for ln in ordered[1:]:
        if abs(ln.box[1] - cur_y) <= row_tol:
            cur.append(ln)
            cur_y = min(cur_y, ln.box[1])
        else:
            rows.append(cur)
            cur = [ln]
            cur_y = ln.box[1]
    rows.append(cur)
    for r in rows:
        r.sort(key=lambda l: l.box[0])
    return rows


def _looks_like_heading_row(row: list[OcrLine], body_h: float, page_width: float) -> int | None:
    """用「框高」代理字号判断标题级别。

    OCR 没有字号/加粗信息，可信的替代信号只有：
    - 框高显著大于正文行高（标题字更大）
    - 文本短
    - 不以句读结尾
    - 居中（左右留白近似）
    """
    text = " ".join(l.text for l in row).strip()
    if not text or len(text) > 120:
        return None
    if text.endswith(("。", ".", "，", ",", "；", ";", "：", ":")):
        return None

    h = max((l.box[3] - l.box[1]) for l in row)
    ratio = h / body_h if body_h else 0.0
    if ratio < 1.15:
        return None

    x0 = min(l.box[0] for l in row)
    x1 = max(l.box[2] for l in row)
    is_centered = abs(x0 - (page_width - x1)) < 40

    if ratio >= 1.5:
        return 1 if is_centered else 2
    if ratio >= 1.25:
        return 2 if is_centered else 3
    return 3 if len(text) <= 60 else None


def _rows_to_blocks(
    rows: list[list[OcrLine]],
    *,
    page: int,
    body_h: float,
    page_width: float,
    start_idx: int,
) -> list[Block]:
    """把一页的视觉行聚合成 Block（标题 / 段落）。"""
    blocks: list[Block] = []
    para_rows: list[list[OcrLine]] = []
    last_bottom: float | None = None

    def flush() -> None:
        nonlocal para_rows
        if not para_rows:
            return
        parts = [" ".join(l.text for l in r) for r in para_rows]
        text = _smart_join(parts)
        scores = [l.score for r in para_rows for l in r]
        # 保守取最低分并轻微打折：OCR 天然不如文字层可靠
        conf = max(0.0, (min(scores) if scores else 0.0) - 0.05)
        if text:
            blocks.append(Block(
                id=f"ocr-p{page}-{start_idx + len(blocks)}",
                type=BlockType.PARAGRAPH,
                data={"text": text},
                confidence=round(conf, 3),
                source=Source(page=page, description=f"第{page}页 OCR 段落"),
            ))
        para_rows = []

    for row in rows:
        level = _looks_like_heading_row(row, body_h, page_width)
        if level is not None:
            flush()
            last_bottom = None
            text = " ".join(l.text for l in row).strip()
            scores = [l.score for l in row]
            blocks.append(Block(
                id=f"ocr-h{page}-{start_idx + len(blocks)}",
                type=BlockType.HEADING,
                data={"level": level, "text": text},
                confidence=round(max(0.0, (min(scores) if scores else 0.0) - 0.05), 3),
                source=Source(page=page, description=f"第{page}页 OCR 标题"),
            ))
            continue

        top = min(l.box[1] for l in row)
        if para_rows and last_bottom is not None and top - last_bottom > body_h * 1.6:
            flush()
        para_rows.append(row)
        last_bottom = max(l.box[3] for l in row)

    flush()
    return blocks


def lines_to_document(
    lines: Iterable[OcrLine],
    *,
    source_file: str,
    mode: str = "ocr",
    engine_name: str = "",
    page_width: float = 1240.0,
) -> Document:
    """把 OCR 行聚合成 Document（跨页）。"""
    all_lines = list(lines)
    blocks: list[Block] = []
    pages = sorted({l.page for l in all_lines}) or [1]
    body_h = _median_height(all_lines)

    for page in pages:
        page_lines = [l for l in all_lines if l.page == page]
        rows = _group_rows(page_lines, row_tol=max(4.0, body_h * 0.6))
        blocks.extend(_rows_to_blocks(
            rows, page=page, body_h=body_h, page_width=page_width, start_idx=len(blocks),
        ))

    notes: list[str] = []
    if engine_name:
        notes.append(f"ocr: 引擎 {engine_name}，共 {len(pages)} 页")
    if not blocks:
        notes.append("ocr: 未识别到任何文本，请确认扫描件清晰度或提高 DPI")
    return Document(
        source_file=source_file,
        mode=mode,  # type: ignore[arg-type]
        detected_kind="pdf_scanned",
        blocks=blocks,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------

def extract(
    path: str | Path,
    *,
    mode: str = "ocr",
    output_dir: Path | None = None,
    engine: OcrEngine | None = None,
    dpi: int = DEFAULT_DPI,
    max_pages: int | None = None,
) -> Document:
    """对扫描件 PDF 做 OCR 提取，返回统一 Document。

    Args:
        path: PDF 路径（应已由 ingest 判定为 pdf_scanned）。
        mode: 运行模式，写入 Document.mode（"ocr" / "high_quality"）。
        output_dir: 预留（OCR 链路不落盘图片，保持与 docx/pdf 签名一致）。
        engine: 自定义 OCR 引擎，None 时用 PaddleOcrEngine（测试可注入假引擎）。
        dpi: 渲染分辨率。
        max_pages: 最多处理前 N 页（控制成本），None 为全部。

    Raises:
        RuntimeError: paddleocr 不可用时给出清晰提示。
    """
    eng = engine
    if eng is None:
        ok, reason = ocr_available()
        if not ok:
            raise RuntimeError(
                f"该 PDF 无文字层（扫描件），需要 OCR：{reason}。"
                "也可改用「高质量模式」（多模态 LLM）处理。"
            )
        eng = PaddleOcrEngine()

    pages = render_pages(path, dpi=dpi, pages=list(range(1, max_pages + 1)) if max_pages else None)

    lines: list[OcrLine] = []
    page_width = 1240.0
    doc = pymupdf.open(str(path))
    try:
        for pno, png in pages:
            for ln in eng.recognize(png):
                ln.page = pno
                lines.append(ln)
        if doc.page_count:
            # 用渲染宽度做居中判断基准（像素 = 页面宽 * dpi / 72）
            page_width = doc[0].rect.width * dpi / 72.0
    finally:
        doc.close()

    result = lines_to_document(
        lines, source_file=str(path), mode=mode, engine_name=eng.name, page_width=page_width,
    )
    result.notes.append(f"ocr: dpi={dpi}, 渲染 {len(pages)} 页")
    return result
