"""FastAPI 应用入口（Phase 3 Web 界面）。

运行：
    cd D:/AIProject/AllToMarkdown
    python -m uvicorn app.main:app --reload --port 8000

前端静态页面挂在根路径，API 路由(/api/*)注册在静态挂载之前，
因此 /api/* 优先由路由层处理，其余路径交由静态文件服务。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router

# static/ 前端资源目录
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(
        title="AllToMarkdown",
        description="把 Word(.docx) 与 PDF 文档批量提取、整理为 Markdown 文件的 AI 工具",
        version="0.3.0",
    )

    # 开发期放宽跨域（前端与后端同源时可移除）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API 路由（注册先于静态挂载，保证 /api/* 优先）
    app.include_router(api_router)

    # 静态前端：根路径服务 static/index.html
    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


app = create_app()
