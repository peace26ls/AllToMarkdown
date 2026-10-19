# AllToMarkdown · 纸墨转换台

把 **Word(.docx)** 与 **PDF** 文档批量提取、整理为清晰 Markdown 文件的 AI 工具。支持双提取链路（docx / PDF）、可选 LLM 结构化整理、Web 界面与命令行双入口。

> 设计语言：Editorial Paper Workbench —— 暖纸白 + 墨黑 + 深青墨强调 + 琥珀点缀。

---

## 功能特性

- **docx 提取**：pandoc 优先，mammoth（HTML 解析）兜底，**表格 / 标题 / 列表 / 图片**高保真还原，图片自动落盘到 `images/` 并正确引用。
- **PDF 提取**：PyMuPDF 文本 + 版式信息，基于字号 / 加粗 / 居中**推断标题层级**；按行垂直间距智能聚合段落，修复中文断行产生的凭空空格。
- **扫描件预检**：按页文字密度自动识别扫描件，给出清晰提示而非崩溃。
- **扫描件 OCR（Phase 4）**：本地 PaddleOCR 识别无文字层 PDF，用「文本框高度」代理字号推断标题、按垂直间距切分段落，输出与文字版同构的中间表示。
- **多模态「高质量模式」（Phase 4）**：把页面渲染成图逐页送视觉模型，能还原多栏排版与跨页表格；**按页计费**，仅在用户显式选择时启用，单页失败只跳过该页。
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
| 扫描件 OCR | PaddleOCR（paddlepaddle 3.3 + paddleocr 3.7，CPU） |
| 整理 | `openai` SDK（OpenAI 兼容，如 DeepSeek）；高质量模式走同一端点的 vision 能力 |
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

> **PaddleOCR（可选，仅扫描件 OCR 需要，约 1–2GB）**：
> ```bash
> pip install paddlepaddle paddleocr
> ```
> 未安装时其余功能不受影响：前端「扫描件 OCR」按钮会置灰，并在 `/api/capabilities` 中给出安装提示。
> **Windows + paddlepaddle 3.3.x 已知坑**：默认启用的 oneDNN 会抛
> `NotImplementedError: ConvertPirAttribute2RuntimeAttribute ... onednn_instruction.cc`，
> 本项目已在 `ocr.py` 中通过 `enable_mkldnn=False` 规避。

### 2. 配置环境变量（仅 LLM 整理需要）

复制 `.env.example` 为 `.env`，或直接设置环境变量：

```bash
export OPENAI_API_KEY=sk-xxxx          # 必填（使用 LLM 整理时）
export OPENAI_BASE_URL=https://api.openai.com/v1
export OPENAI_MODEL=gpt-4o-mini
export CONFIDENCE_THRESHOLD=0.7        # 低于此置信度进复核清单
export PDF_TEXT_DENSITY_THRESHOLD=80   # 扫描件判定：单页字数阈值
export OCR_DPI=150                     # Phase 4：扫描件渲染分辨率（越高越准越慢）
export VISION_MAX_PAGES=               # Phase 4：高质量模式单次最多处理页数（留空=不限，按页计费！）
```

### 3. 命令行转换

```bash
# 纯规则提取
python -m app.cli tests/fixtures/sample.docx -o out.md
python -m app.cli tests/fixtures/sample.pdf -o out.md

# 附 LLM 整理层（需 API key）
python -m app.cli tests/fixtures/sample.docx -o out.md --organize

# Phase 4：扫描件 OCR（需 paddleocr，CPU 上较慢）
python -m app.cli scan.pdf -o out.md --mode ocr

# Phase 4：多模态高质量模式（按页计费，适合复杂版式/扫描件）
python -m app.cli complex.pdf -o out.md --mode high_quality
```

> 三种模式的选择：**standard** 最快且免费；**ocr** 本地跑，无网络成本但 CPU 慢；
> **high_quality** 质量最好（能还原表格与多栏），但每张页面都要调用一次视觉模型。

### 4. 启动 Web 界面

```bash
python -m uvicorn app.main:app --reload --port 8000
# 打开 http://127.0.0.1:8000
```

---

## Web API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/capabilities` | 服务端能力：`ocr`（PaddleOCR 是否可用）/ `vision`（是否配置 key） |
| POST | `/api/tasks` | multipart 上传（`file` + `mode` + `organize`），返回 `{task_id}`。`mode`：`standard` / `high_quality` / `ocr` |
| GET | `/api/tasks` | 最近任务列表 |
| GET | `/api/tasks/{id}` | 轮询状态：`pending/extracting/ocr/vision/organizing/rendering/done/failed` + 进度 |
| GET | `/api/tasks/{id}/markdown` | 下载 `output.md` |
| GET | `/api/tasks/{id}/preview` | 返回 markdown 原文（前端渲染） |
| GET | `/api/tasks/{id}/meta` | 置信度 / 复核清单 / 耗时统计 |
| GET | `/api/tasks/{id}/archive` | 下载 zip（`output.md` + `images/`） |
| GET | `/api/tasks/{id}/images/{name}` | 任务图片静态服务 |
| DELETE | `/api/tasks/{id}` | 清理任务 |

状态机：`pending → extracting → organizing(可选) → rendering → done / failed`；
选择 `ocr` 时提取阶段状态为 `ocr`，选择 `high_quality` 时为 `vision`（前端据此显示「OCR 识别中 / 多模态识别中」）。

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
│       ├── llm.py           # OpenAI 兼容 provider 抽象 + JSON 解析重试 + vision 补全
│       ├── organize.py      # 分块策略 + LLM 增量整理
│       ├── ocr.py           # Phase 4：OCR 引擎抽象 + PaddleOCR + 页面渲染 + 几何聚合
│       ├── vision.py        # Phase 4：多模态逐页转换（按页计费、逐页容错）
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

覆盖：schema、ingest、render、docx 提取、PDF 提取、organize 分块/校验/回退、
Web API 端到端、OCR 聚合逻辑、多模态解析与容错（共 62 个用例）。

OCR / 多模态的单元测试**不依赖 paddle 与网络**：用假引擎、假 provider 注入验证逻辑；
真实 PaddleOCR 的端到端冒烟用（需本机已装 paddle）：

```bash
RUN_REAL_OCR=1 python -m pytest tests/test_ocr.py -k real -v
```

---

## 路线图

- [x] **Phase 1** 纯规则 MVP：ingest + docx/PDF 提取 + render + CLI
- [x] **Phase 2** LLM 整理层：分块 + JSON 增量指令 + 回退
- [x] **Phase 3** Web 界面：FastAPI + 状态机 + 前端
- [x] **Phase 4** 扫描件 OCR（PaddleOCR）+ 多模态「高质量模式」

---

## 已知限制

- **PDF 表格** 规则链路未识别（只做文本 + 版式标题推断）；需要表格时用 `--mode high_quality`，或等后续列坐标聚类方案。
- **PDF 标题层级** 为启发式，复杂版式依赖 LLM 整理层优化。
- **docx 用 mammoth 兜底**时，复杂样式（页眉页脚、批注、嵌入对象）不处理。
- LLM 整理需联网与 API key；未配置时仅走纯规则提取。
- **OCR 慢**：CPU 上单页约数十秒（含模型加载，首次还会下载 PP-OCRv6 模型），长扫描件建议先抽页。
- **高质量模式贵**：按页调用视觉模型，务必设 `VISION_MAX_PAGES` 或直接只在关键页使用。
- **OCR 不还原表格**：扫描件走行级聚合，表格内容会变成连续段落；表格还原请用高质量模式。

---

## License

MIT © AllToMarkdown
