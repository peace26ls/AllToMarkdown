"""Phase 1 纯规则 MVP 的 CLI 入口。

用法:
    python -m app.cli <input.docx|.pdf> -o <out.md> --mode standard
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from app.core import extract_docx, extract_pdf
from app.core.ingest import DetectedKind, get_text_density_threshold, inspect


def _word_count(text: str) -> int:
    """粗略字数统计（用于日志展示）。"""
    return len(text)


def build_document(input_path: Path, mode: str):
    """根据 ingest 识别结果路由到对应提取器。

    Returns:
        (Document, 识别信息 DocInspect)
    Raises:
        RuntimeError: 文件类型不支持或扫描件（Phase 1 未实现 OCR）
    """
    info = inspect(input_path)

    if info.detected_kind == DetectedKind.DOCX:
        doc = extract_docx.extract(input_path, output_dir=input_path.parent)
    elif info.detected_kind == DetectedKind.PDF_TEXT:
        doc = extract_pdf.extract(input_path, output_dir=input_path.parent)
    elif info.detected_kind == DetectedKind.PDF_SCANNED:
        # Phase 1 无 OCR：给出清晰报错而非崩溃
        raise RuntimeError(
            "未检测到文字层，该 PDF 疑似为扫描件。"
            "Phase 1 尚不支持 OCR，可改用「高质量模式」或等待 Phase 4 的 OCR 链路。"
        )
    else:
        raise RuntimeError(f"不支持的文件类型或无法识别: {info.message}")

    # 记录运行模式与识别结论
    doc.mode = mode
    doc.detected_kind = info.detected_kind.value
    doc.notes.append(f"ingest: {info.message}")

    # 依据置信度阈值统一回填 needs_review（默认 0.7，可被环境变量覆盖）
    threshold = float(os.getenv("CONFIDENCE_THRESHOLD", "0.7"))
    doc.finalize(threshold)
    return doc, info


def organize_document_with_llm(doc):
    """在提取结果上运行 LLM 整理层（Phase 2）。

    返回整理后的 Document（直接修改 blocks）。LLM 整理失败会回退到
    规则提取的原始块，并把告警附加到 doc.notes，不中断流程。
    """
    from app.core.llm import OpenAIProvider, complete_json
    from app.core.organize import organize_document

    provider = OpenAIProvider()
    # 适配器：organize 层协议 llm_call(system, user) -> dict
    llm_call = lambda system, user: complete_json(provider, user, system=system)

    print(f"      调用 LLM 整理（{provider.name}）...")
    result = organize_document(doc, llm_call)
    doc.blocks = result.blocks
    if result.chunks_unorganized:
        doc.notes.append(
            f"organize: {result.chunks_unorganized} 个 chunk 整理失败，已保留规则提取的原始块"
        )
    for item in result.meta:
        doc.notes.append(f"organize[{item.get('chunk_id', '')}]: {item.get('message', '')}")
    return doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alltomd",
        description="把 Word(.docx) 与 PDF 文档提取整理为 Markdown 文件",
    )
    parser.add_argument("input", type=Path, help="输入的 .docx 或 .pdf 文件")
    parser.add_argument("-o", "--output", type=Path, default=None, help="输出 .md 文件路径")
    parser.add_argument(
        "-O", "--organize", action="store_true",
        help="调用 LLM 整理层（Phase 2），需配置 OPENAI_API_KEY",
    )
    parser.add_argument(
        "--mode",
        choices=["standard", "high_quality"],
        default="standard",
        help="运行模式（Phase 2 起 high_quality 有完整实现）",
    )
    args = parser.parse_args(argv)

    input_path: Path = args.input
    if not input_path.exists():
        print(f"错误: 输入文件不存在: {input_path}", file=sys.stderr)
        return 1

    output_path = args.output or input_path.with_suffix(".md")

    try:
        print(f"[1/3] 识别文件类型: {input_path.name}")
        doc, info = build_document(input_path, args.mode)
        print(f"      识别为 {doc.detected_kind}，共 {len(doc.blocks)} 个内容块")

        if args.organize:
            organize_document_with_llm(doc)
            print(f"      整理后 {len(doc.blocks)} 个内容块")

        print("[2/3] 渲染 Markdown ...")
        from app.core.render import render_document

        md_text = render_document(doc, base_dir=str(output_path.parent))

        print("[3/3] 写出文件 ...")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(md_text, encoding="utf-8")
        print(f"      已写出: {output_path}")
        print(f"      字数约 {_word_count(md_text)}")

        # 报告需人工复核的低置信度块
        review_blocks = doc.review_items(threshold=float(os.getenv("CONFIDENCE_THRESHOLD", "0.7")))
        if review_blocks:
            print(f"      提示: {len(review_blocks)} 个低置信度块需人工复核：")
            for b in review_blocks:
                loc = f"p{b.source.page}" if b.source and b.source.page else "-"
                print(
                    f"        - [{b.id}] {b.type.value} @{loc} "
                    f"(conf={b.confidence:.2f}, reason={b.data.get('note', '') or '低置信度'})"
                )
        return 0

    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except Exception:
        print("发生未预期的错误:", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
