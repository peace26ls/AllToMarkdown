"""后台任务状态机（Phase 3 单机版）。

职责：
- 管理一次「上传 -> 提取 -> 整理 -> 渲染 -> 下载」任务的完整生命周期。
- 每个任务有独立目录 `tasks/{task_id}/`，其下产出 output.md / output.meta.json / images/。
- registry 用线程安全字典实现，配合 status 接口轮询（前端不上 WebSocket）。

状态机：pending -> extracting -> organizing(可选) -> rendering -> done / failed。
进度 progress 为 0-100 浮点，供前端进度条。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from app.core.ingest import DetectedKind, inspect

# tasks/ 运行时产物根目录（.gitignore 已忽略其内容）
TASKS_ROOT = Path(__file__).resolve().parent.parent / "tasks"

# 置信度阈值，低于此值的 block 标 needs_review 进 meta 复核清单
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.7"))


class TaskStatus(str, Enum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    # Phase 4：扫描件 OCR（本地 PaddleOCR）
    OCR = "ocr"
    # Phase 4：多模态「高质量模式」（按页调用视觉模型）
    VISION = "vision"
    ORGANIZING = "organizing"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Task:
    """一个转换任务的运行时状态。"""

    id: str
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    error: str | None = None
    source_name: str = ""
    mode: str = "standard"
    organize: bool = False
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    elapsed: float = 0.0
    blocks: int = 0
    detected_kind: str = ""
    # 任务目录 tasks/{id}/
    dir: Path | None = None
    # 输入文件保存路径
    input_path: Path | None = None
    # 人类可读进度说明（用于前端状态文案）
    step_label: str = "排队中"
    notes: list[str] = field(default_factory=list)


_registry: dict[str, Task] = {}
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def create_task(*, source_name: str, mode: str, organize: bool, input_path: Path) -> str:
    """登记一个任务并返回 task_id。任务目录与状态在此建立。"""
    tid = uuid.uuid4().hex[:12]
    task_dir = TASKS_ROOT / tid
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "images").mkdir(exist_ok=True)

    task = Task(
        id=tid,
        source_name=source_name,
        mode=mode,
        organize=organize,
        dir=task_dir,
        input_path=input_path,
        step_label="排队中",
    )
    with _lock:
        _registry[tid] = task
    return tid


def get_task(tid: str) -> Task | None:
    with _lock:
        return _registry.get(tid)


def _set(task: Task, **kwargs: Any) -> None:
    """线程安全地更新任务字段。"""
    with _lock:
        for k, v in kwargs.items():
            setattr(task, k, v)


def status_payload(tid: str) -> dict | None:
    """构造给前端轮询的 JSON 状态。任务不存在返回 None。"""
    task = get_task(tid)
    if task is None:
        return None
    with _lock:
        return {
            "id": task.id,
            "status": task.status.value,
            "progress": round(task.progress, 1),
            "step_label": task.step_label,
            "error": task.error,
            "blocks": task.blocks,
            "detected_kind": task.detected_kind,
            "elapsed": round(task.elapsed, 1),
            "organize": task.organize,
            "mode": task.mode,
            "source_name": task.source_name,
        }


def list_tasks(limit: int = 20) -> list[dict]:
    """按创建时间倒序列出最近任务（首页概览用）。"""
    with _lock:
        items = sorted(_registry.values(), key=lambda t: t.created_at, reverse=True)
        payloads = []
        for t in items[:limit]:
            payloads.append({
                "id": t.id,
                "source_name": t.source_name,
                "status": t.status.value,
                "progress": round(t.progress, 1),
                "created_at": t.created_at,
                "mode": t.mode,
                "organize": t.organize,
            })
        return payloads


# ---------------------------------------------------------------------------
# 主执行流程（在独立线程中运行）
# ---------------------------------------------------------------------------

def _vision_extract(input_path: Path, detected_kind: str, mode: str):
    """多模态「高质量模式」：逐页渲染成图送视觉模型。"""
    from app.core import vision
    from app.core.llm import OpenAIProvider, make_vision_call

    provider = OpenAIProvider()
    doc = vision.extract(
        input_path,
        vision_call=make_vision_call(provider),
        mode=mode,
        max_pages=int(os.getenv("VISION_MAX_PAGES", "0")) or None,
    )
    # detected_kind 以 ingest 结论为准（扫描件/文字版由预检决定）
    doc.detected_kind = detected_kind
    doc.notes.append(f"vision: provider={provider.name}")
    return doc


def _ocr_extract(input_path: Path, mode: str):
    """扫描件 OCR：本地 PaddleOCR（未安装时 ocr.extract 会给出清晰报错）。"""
    from app.core import ocr

    return ocr.extract(input_path, mode=mode, dpi=int(os.getenv("OCR_DPI", "150")))


def _extract_doc(input_path: Path, output_dir: Path, mode: str):
    from app.core import extract_docx, extract_pdf

    info = inspect(input_path)

    # 高质量模式对文字版与扫描件都适用（模型直接"看"页面）
    if mode == "high_quality":
        return _vision_extract(input_path, info.detected_kind.value, mode), info

    if mode == "ocr" or info.detected_kind == DetectedKind.PDF_SCANNED:
        return _ocr_extract(input_path, mode), info

    if info.detected_kind == DetectedKind.DOCX:
        doc = extract_docx.extract(input_path, output_dir=output_dir)
    elif info.detected_kind == DetectedKind.PDF_TEXT:
        doc = extract_pdf.extract(input_path, output_dir=output_dir)
    else:
        raise RuntimeError(f"不支持的文件类型或无法识别: {info.message}")
    doc.mode = mode
    doc.detected_kind = info.detected_kind.value
    doc.finalize(CONFIDENCE_THRESHOLD)
    return doc, info


def _organize_doc(doc) -> None:
    from app.core.llm import OpenAIProvider, complete_json
    from app.core.organize import organize_document

    provider = OpenAIProvider()
    llm_call = lambda system, user: complete_json(provider, user, system=system)
    result = organize_document(doc, llm_call)
    doc.blocks = result.blocks
    if result.chunks_unorganized:
        doc.notes.append(
            f"organize: {result.chunks_unorganized} 个 chunk 整理失败，已保留原始块"
        )
    for item in result.meta:
        doc.notes.append(f"organize[{item.get('chunk_id', '')}]: {item.get('message', '')}")


def _write_outputs(task: Task, doc, base_dir: str) -> dict:
    """渲染并写出 output.md 与 output.meta.json，返回路径字典。"""
    from app.core.render import render_document

    md_text = render_document(doc, base_dir=base_dir)

    out_md = task.dir / "output.md"
    out_md.write_text(md_text, encoding="utf-8")

    # 收集复核项
    review_items = []
    for b in doc.blocks:
        if b.needs_review or b.confidence < CONFIDENCE_THRESHOLD:
            review_items.append({
                "id": b.id,
                "type": b.type.value,
                "page": b.source.page if b.source else None,
                "confidence": round(b.confidence, 3),
                "description": b.source.description if b.source else None,
                "reason": b.data.get("note", "低置信度") if isinstance(b.data, dict) else "低置信度",
            })

    meta = {
        "task_id": task.id,
        "source": task.source_name,
        "detected_kind": doc.detected_kind,
        "mode": task.mode,
        "organize": task.organize,
        "blocks": len(doc.blocks),
        "review_items": review_items,
        "notes": doc.notes,
        "elapsed": round(task.elapsed, 2),
        "created_at": task.created_at,
    }
    meta_path = task.dir / "output.meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "md": str(out_md),
        "meta": str(meta_path),
        "images_dir": str(task.dir / "images"),
    }


def run_task(tid: str) -> None:
    """处理任务（在线程中执行）。异常被捕获写入任务状态，绝不外抛。"""
    task = get_task(tid)
    if task is None:
        return
    start = _now()
    try:
        if task.mode == "high_quality":
            step = "多模态识别中（按页计费）…"
            phase = TaskStatus.VISION
        elif task.mode == "ocr":
            step = "OCR 识别扫描件…"
            phase = TaskStatus.OCR
        else:
            step = "正在提取内容…"
            phase = TaskStatus.EXTRACTING
        _set(task, status=phase, progress=15, step_label=step)

        doc, info = _extract_doc(task.input_path, task.dir, task.mode)
        # OCR / 多模态链路内部不做阈值回填，这里统一处理（幂等）
        doc.finalize(CONFIDENCE_THRESHOLD)
        _set(task, status=phase, progress=55, blocks=len(doc.blocks),
             detected_kind=doc.detected_kind, step_label="提取完成，正在等待整理/渲染…")
        task.notes.extend(doc.notes)

        if task.organize:
            _set(task, status=TaskStatus.ORGANIZING, progress=70, step_label="LLM 结构化整理中…")
            _organize_doc(doc)

        _set(task, status=TaskStatus.RENDERING, progress=85, step_label="渲染 Markdown…")
        _write_outputs(task, doc, base_dir=str(task.dir))

        elapsed = _now() - start
        _set(task, status=TaskStatus.DONE, progress=100, step_label="完成",
             elapsed=elapsed, finished_at=_now(),
             blocks=len(doc.blocks))
    except Exception as exc:  # noqa: BLE001
        elapsed = _now() - start
        _set(task, status=TaskStatus.FAILED, error=str(exc), step_label="失败",
             elapsed=elapsed, finished_at=_now())


def start_task(tid: str) -> None:
    """启动任务：在独立线程中执行，不阻塞请求。"""
    t = threading.Thread(target=run_task, args=(tid,), daemon=True)
    t.start()


def cleanup_task(tid: str) -> None:
    """删除任务目录及其状态（供手动清理/取消接口）。"""
    task = get_task(tid)
    if task is not None:
        with _lock:
            _registry.pop(tid, None)
        if task.dir and task.dir.exists():
            shutil.rmtree(task.dir, ignore_errors=True)
