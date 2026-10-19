"""FastAPI 路由层（Phase 3 前端契约，对应交接文档第 7 节）。

所有转换任务的唯一入口是 POST /api/tasks（multipart 上传），随后的
状态轮询 / 下载 / 预览 / 元数据都围绕返回的 task_id 进行。
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.responses import Response

from app import tasks
from app.core.ingest import inspect

router = APIRouter(prefix="/api")

# 上传文件名清洗：只保留安全字符，避免路径穿越
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_.\-]")


def _safe_filename(name: str) -> str:
    base = _SAFE_NAME_RE.sub("_", name or "document")
    return base[:120] or "document"


@router.post("/tasks")
async def create_task(
    file: UploadFile = File(...),
    mode: str = Form("standard"),
    organize: str = Form("false"),
) -> JSONResponse:
    """上传文档并创建转换任务。返回 {task_id}。

    ATTENTION：为支持前端轮询，这里不阻塞等结果，任务在后台线程运行。
    """
    if mode not in ("standard", "high_quality"):
        raise HTTPException(status_code=400, detail="mode 仅支持 standard / high_quality")

    # 先把上传文件保存到任务目录，保证任务执行时磁盘可读
    raw = await file.read()
    safe_name = _safe_filename(file.filename or "document")
    # 校验扩展名，early fail
    suffix = Path(safe_name).suffix.lower()
    if suffix not in (".docx", ".pdf"):
        raise HTTPException(status_code=400, detail="仅支持 .docx 与 .pdf 文件")

    tid = tasks.create_task(
        source_name=file.filename or safe_name,
        mode=mode,
        organize=(organize.lower() in ("true", "1", "yes", "on")),
        input_path=Path(safe_name),  # 占位，下面写入真实路径
    )
    task = tasks.get_task(tid)
    input_dir = task.dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / safe_name
    input_path.write_bytes(raw)

    tasks._set(task, input_path=input_path)  # noqa: SLF001 登记真实输入路径

    tasks.start_task(tid)
    return JSONResponse({"task_id": tid})


@router.get("/tasks")
async def list_tasks(limit: int = Query(20, ge=1, le=100)) -> JSONResponse:
    """最近任务列表（首页概览）。"""
    return JSONResponse({"tasks": tasks.list_tasks(limit=limit)})


@router.get("/tasks/{tid}")
async def get_task_status(tid: str) -> JSONResponse:
    """轮询任务状态。"""
    payload = tasks.status_payload(tid)
    if payload is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return JSONResponse(payload)


@router.get("/tasks/{tid}/markdown")
async def download_markdown(tid: str) -> Response:
    """下载 output.md（Content-Disposition 附件）。"""
    payload = tasks.status_payload(tid)
    if payload is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if payload["status"] != "done":
        raise HTTPException(status_code=409, detail="任务尚未完成")
    task = tasks.get_task(tid)
    out_md = task.dir / "output.md"
    if not out_md.exists():
        raise HTTPException(status_code=404, detail="未找到产物 output.md")
    return FileResponse(
        out_md,
        media_type="text/markdown; charset=utf-8",
        filename=f"{_safe_filename(task.source_name).rsplit('.', 1)[0]}.md",
    )


@router.get("/tasks/{tid}/preview")
async def preview_markdown(tid: str) -> PlainTextResponse:
    """返回原始 markdown，前端用 marked.js 渲染成 HTML 片段。"""
    payload = tasks.status_payload(tid)
    if payload is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if payload["status"] != "done":
        raise HTTPException(status_code=409, detail="任务尚未完成")
    out_md = tasks.get_task(tid).dir / "output.md"
    if not out_md.exists():
        raise HTTPException(status_code=404, detail="未找到产物 output.md")
    return PlainTextResponse(out_md.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


@router.get("/tasks/{tid}/archive")
async def download_archive(tid: str) -> Response:
    """下载 zip 压缩包：包含 output.md 与 images/ 全部图片。"""
    payload = tasks.status_payload(tid)
    if payload is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if payload["status"] != "done":
        raise HTTPException(status_code=409, detail="任务尚未完成")

    import io
    import zipfile

    task = tasks.get_task(tid)
    out_md = task.dir / "output.md"
    if not out_md.exists():
        raise HTTPException(status_code=404, detail="未找到产物 output.md")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(out_md, arcname="output.md")
        img_dir = task.dir / "images"
        if img_dir.is_dir():
            for img in sorted(img_dir.iterdir()):
                if img.is_file():
                    zf.write(img, arcname=f"images/{img.name}")
    buf.seek(0)

    stem = _safe_filename(task.source_name).rsplit(".", 1)[0]
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{stem}-alltomd.zip"'},
    )


@router.get("/tasks/{tid}/meta")
async def get_meta(tid: str) -> JSONResponse:
    """置信度、复核清单、耗时统计。"""
    payload = tasks.status_payload(tid)
    if payload is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    task = tasks.get_task(tid)
    meta_path = task.dir / "output.meta.json"
    if not meta_path.exists():
        if payload["status"] != "done":
            raise HTTPException(status_code=409, detail="任务尚未完成")
        raise HTTPException(status_code=404, detail="未找到 output.meta.json")
    import json as _json

    return JSONResponse(_json.loads(meta_path.read_text(encoding="utf-8")))


@router.get("/tasks/{tid}/images/{name}")
async def get_image(tid: str, name: str) -> FileResponse:
    """返回任务 images/ 下的图片文件。"""
    payload = tasks.status_payload(tid)
    if payload is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    safe = _SAFE_NAME_RE.sub("_", name or "")
    img = tasks.get_task(tid).dir / "images" / safe
    if not img.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(img)


@router.delete("/tasks/{tid}")
async def delete_task(tid: str) -> JSONResponse:
    """清理任务目录与状态。"""
    payload = tasks.status_payload(tid)
    if payload is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    tasks.cleanup_task(tid)
    return JSONResponse({"ok": True})


# 供 main.py 导入，便于健康检查复用
def inspect_doc(path: str):
    """对上传前的文件做快速格式识别（供调试/校验）。"""
    return inspect(path)
