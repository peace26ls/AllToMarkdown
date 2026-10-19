"""多模态「高质量模式」（Phase 4）。

适用场景（对应交接文档第 3 节的技术选型）：
- **复杂版式**：多栏排版、跨页表格、图文混排 —— 规则提取会串行错乱，
  模型直接"看"页面反而更准。
- **扫描件兜底**：本机没装 OCR 引擎时，可直接用视觉模型读图。

为什么不是默认模式：按页调用视觉模型，**成本与耗时远高于规则提取**，
所以只在用户显式选择「高质量」时启用，并在 notes / meta 中写明调用页数。

设计要点：
- 与 OCR 链路共用 ``ocr.render_pages``（PyMuPDF 渲染，无额外依赖）。
- 模型只要求返回**纯净 Markdown**，本模块再解析成统一 Block，
  因此下游 organize / render / 前端完全复用，不引入第二套契约。
- **逐页容错**：单页调用失败只跳过该页并记 note，不整份失败；
  只有全部页失败才抛错，避免一次抖动毁掉整份文档。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

from app.schema import Block, BlockType, Document, Source
from app.core.ocr import render_pages

# vision_call 协议：(system, user_text, images: list[bytes]) -> markdown 文本
VisionCall = Callable[[str, str, list[bytes]], str]

# 高质量模式渲染 DPI：110 足够视觉模型读清版式，兼顾图片体积与 token 成本
DEFAULT_DPI = 110

_SYSTEM_PROMPT = (
    "你是文档数字化助手，负责把页面图像忠实转换为 Markdown。\n"
    "严格要求：\n"
    "1. 只输出 Markdown 正文，不要任何解释、前言、结语或代码块围栏。\n"
    "2. 保留标题层级（# / ## / ###），保持原文措辞与顺序。\n"
    "3. 表格必须用 GFM 表格语法（| 列 | 列 |）完整还原，不要省略行列。\n"
    "4. 严禁臆造、补全或翻译页面上看不到的内容；识别不清的地方照抄可见部分。\n"
    "5. 公式用 LaTeX（行内 $...$，独立 $$...$$）；代码用带语言标注的代码围栏。"
)

_USER_TEMPLATE = "请把第 {page} 页转换为 Markdown 正文。"


def _parse_markdown(text: str) -> list[Block]:
    """把模型输出的 Markdown 解析成 Block 列表。

    复用 docx 链路里已验证过的 GFM 解析器（标题/段落/列表/表格/代码），
    避免为视觉链路再维护一套解析规则。视觉链路没有本地图片目录（整页已
    交给模型看图），故 images 传空、基准置信度给 1.0，由调用方按页统一打折。
    """
    from app.core.extract_docx import _parse_blocks

    return list(_parse_blocks(text or "", images=[], base_confidence=1.0))


def extract(
    path: str | Path,
    *,
    vision_call: VisionCall,
    mode: str = "high_quality",
    output_dir: Path | None = None,
    dpi: int = DEFAULT_DPI,
    max_pages: int | None = None,
    pages: Sequence[int] | None = None,
) -> Document:
    """用视觉模型把 PDF 页面转成统一 Document。

    Args:
        path: PDF 路径（文字版 / 扫描件均可）。
        vision_call: 多模态调用适配器，见 ``llm.make_vision_call``。
        mode: 运行模式，写入 Document.mode。
        dpi: 渲染分辨率。
        max_pages: 最多处理前 N 页（**按页计费**，建议长文档设上限）。
        pages: 指定页号（1-based），优先级高于 max_pages。

    Returns:
        Document（blocks 的 source.page 标注来源页）。

    Raises:
        RuntimeError: 所有页面都调用失败时抛出（提示检查 key / 模型是否支持视觉）。
    """
    targets = list(pages) if pages else (
        list(range(1, max_pages + 1)) if max_pages else None
    )
    rendered = render_pages(path, dpi=dpi, pages=targets)

    blocks: list[Block] = []
    notes: list[str] = []
    ok_pages = 0
    failed_pages: list[int] = []

    for pno, png in rendered:
        try:
            md = vision_call(_SYSTEM_PROMPT, _USER_TEMPLATE.format(page=pno), [png])
        except Exception as exc:  # noqa: BLE001 单页失败：记录并继续
            failed_pages.append(pno)
            notes.append(f"vision: 第{pno}页调用失败（{exc}），已跳过")
            continue

        page_blocks = _parse_markdown(md)
        if not page_blocks:
            failed_pages.append(pno)
            notes.append(f"vision: 第{pno}页未解析出内容，已跳过")
            continue

        for i, b in enumerate(page_blocks):
            # 重编号避免跨页 id 冲突；source.page 保留来源页便于复核定位
            b.id = f"v{pno}-{i}"
            b.source = Source(page=pno, description=f"第{pno}页 多模态识别")
            if b.confidence == 1.0:
                # 多模态并非逐字确定：给一个低于规则提取但高于 OCR 的默认分，
                # 便于低质量页自然进入复核清单。
                b.confidence = 0.85
        blocks.extend(page_blocks)
        ok_pages += 1

    if ok_pages == 0:
        raise RuntimeError(
            "多模态「高质量模式」全部页面调用失败："
            + ("；".join(notes) if notes else "无可用页面")
            + "。请检查 OPENAI_API_KEY / OPENAI_BASE_URL 是否可用，"
            "以及所选模型是否支持图片输入（vision）。"
        )

    notes.insert(0, f"vision: 成功 {ok_pages} 页，失败 {len(failed_pages)} 页（按页计费）")
    return Document(
        source_file=str(path),
        mode=mode,  # type: ignore[arg-type]
        detected_kind="pdf_text",
        blocks=blocks,
        notes=notes,
    )


def estimate_pages(path: str | Path) -> int:
    """返回 PDF 页数（供前端/CLI 估算高质量模式的按页成本）。"""
    import pymupdf

    doc = pymupdf.open(str(path))
    try:
        return doc.page_count
    finally:
        doc.close()
