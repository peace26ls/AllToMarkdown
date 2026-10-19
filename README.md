# AllToMarkdown · 纸墨转换台

把 **Word(.docx)** 与 **PDF** 文档批量提取、整理为清晰 Markdown 文件的 AI 工具。支持双提取链路（docx / PDF）、可选 LLM 结构化整理、Web 界面与命令行双入口。

> 设计语言：Editorial Paper Workbench —— 暖纸白 + 墨黑 + 深青墨强调 + 琥珀点缀。

---

## 功能特性

- **docx 提取**：pandoc 优先，mammoth（HTML 解析）兜底，**表格 / 标题 / 列表 / 图片**高保真还原，图片自动落盘到 `images/` 并正确引用。
- **PDF 提取**：PyMuPDF 文本 + 版式信息，基于字号 / 加粗 / 居中**推断标题层级**；按行垂直间距智能聚合段落，修复中文断行产生的凭空空格。
- **扫描件预检**：按页文字密度自动识别扫描件，给出清晰提示而非崩溃。
- **LLM 结构化整理（Phase 2）**：按标题树分块（原子块不拆），让 LLM 输出**严格 JSON 增量指令**（keep / merge / split / drop），本地渲染；引用缺失 id 会被拦截，整理失败自动回退原始块并进入复核清单。
- **Web 界面（Phase 3）**：上传 → 进度轮询 → 预览（含校对清单）→ 下载 .md / 压缩包；最近任务可回看。
- **命令行**：`python -m app.cli` 单文件转换。

---

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.11+ |
| 后端 | FastAPI + uvicorn（BackgroundTasks / threading 单机状态机） |
| 提取 | pandoc（可选）· mammoth · PyMuPDF |
| 整理 | `openai` SDK（OpenAI 兼容，如 DeepSeek） |
| 前端 | 原生 HTML / CSS / JS（marked 渲染，无框架） |

---

## 快速开始

### 1. 安装依赖

```bash
# 推荐使用 venv
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -e .            # 或按需安装：
pip install pydantic pymupdf python-docx mammoth pillow fastapi "uvicorn[standard]" python-multipart openai httpx pytest
```

> **pandoc（可选）**：安装 [pandoc](https://pandoc.org/installing.html) 后，docx 走 pandoc 链路（还原度最高）；缺失时自动降级 mammoth。

### 2. 配置环境变量（仅 LLM 整理需要）

复制 `.env.example` 为 `.env`，或直接设置环境变量：

```bash
export OPENAI_API_KEY=sk-xxxx          # 必填（使用 LLM 整理时）
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_MODEL=gpt-4o-mini
export CONFIDENCE_THRESHOLD=0.7        # 低于此置信度进复核清单
export PDF_TEXT_DENSITY_THRESHOLD=80   # 扫描件判定：单页字数阈值
```

### 3. 命令行转换

```bash
# 纯规则提取
python -m app.cli tests/fixtures/sample.docx -o out.md
python -m app.cli tests/fixtures/sample.pdf -o out.md

# 附 LLM 整理层（需 API key）
python -m app.cli tests/fixtures/sample.docx -o out.md --organize
```

### 4. 启动 Web 界面

```bash
python -m uvicorn app.main:app --reload --port 8000
# 打开 http://127.0.0.1:8000
```

---

## Web API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` | multipart 上传（`file` + `mode` + `organize`），返回 `{task_id}` |
| GET | `/api/tasks` | 最近任务列表 |
| GET | `/api/tasks/{id}` | 轮询状态：`pending/extracting/organizing/rendering/done/failed` + 进度 |
| GET | `/api/tasks/{id}/markdown` | 下载 `output.md` |
| GET | `/api/tasks/{id}/preview` | 返回 markdown 原文（前端渲染） |
| GET | `/api/tasks/{id}/meta` | 置信度 / 复核清单 / 耗时统计 |
| GET | `/api/tasks/{id}/archive` | 下载 zip（`output.md` + `images/`） |
| GET | `/api/tasks/{id}/images/{name}` | 任务图片静态服务 |
| DELETE | `/api/tasks/{id}` | 清理任务 |

状态机：`pending → extracting → organizing(可选) → rendering → done / failed`。

---

## 项目结构

```
AllToMarkdown/
├── pyproject.toml / .env.example / README.md / HANDOFF.md
├── app/
│   ├── main.py          # FastAPI 入口，挂载 API 与静态前端
│   ├── api.py           # /api/tasks 路由
│   ├── tasks.py         # 后台任务状态机
│   ├── cli.py           # 命令行入口
│   ├── schema.py        # 统一中间表示（Block/Document/Source）—— 契约核心
│   └── core/
│       ├── ingest.py        # 格式识别 + 扫描件预检
│       ├── extract_docx.py  # pandoc 优先 + mammoth HTML 兜底
│       ├── extract_pdf.py   # PyMuPDF 版式标题推断 + 段落聚合
│       ├── llm.py           # OpenAI 兼容 provider 抽象 + JSON 解析重试
│       ├── organize.py      # 分块策略 + LLM 增量整理
│       └── render.py        # 中间表示 -> Markdown
├── static/               # Web 前端（index.html / style.css / app.js）
├── tasks/                # 运行时任务产物（.gitignore）
└── tests/                # 单测 + fixtures（docx / 文字PDF / 扫描PDF）
```

### 统一中间表示（schema）

所有提取器输出同一种 `Block` 结构，整理层与渲染层只认这份契约：

- `BlockType`：heading / paragraph / list / table / image / code / formula / rule
- **原子块**（表格 / 公式 / 列表 / 代码）在 schema 层标记 `atomic`，分块时绝不截断
- `confidence` 低于阈值自动标 `needs_review`，进入 meta 复核清单
- `Source` 记录页码 / 锚点，用于报错与人工定位

核心原则：**LLM 只输出增量 JSON 指令，最终 Markdown 始终由 render 层本地生成**，不接触模型全文。

---

## 测试

```bash
python -m pytest tests/ -v
```

覆盖：schema、ingest、render、docx 提取、PDF 提取、organize 分块/校验/回退、Web API 端到端。

---

## 路线图

- [x] **Phase 1** 纯规则 MVP：ingest + docx/PDF 提取 + render + CLI
- [x] **Phase 2** LLM 整理层：分块 + JSON 增量指令 + 回退
- [x] **Phase 3** Web 界面：FastAPI + 状态机 + 前端
- [ ] **Phase 4** 扫描件 OCR（PaddleOCR）+ 多模态「高质量模式」

---

## 已知限制

- **PDF 表格** 当前未识别（PDF 链路只做文本 + 版式标题推断），mammoth 路径的表格依赖 HTML 结构。
- **PDF 标题层级** 为启发式，复杂版式依赖 LLM 整理层优化。
- **docx 用 mammoth 兜底**时，复杂样式（页眉页脚、批注、嵌入对象）不处理。
- LLM 整理需联网与 API key；未配置时仅走纯规则提取。

---

## License

MIT © AllToMarkdown
