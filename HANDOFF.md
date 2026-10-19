# AllToMarkdown 项目交接文档

> 文档生成日期：2026-10-18
> 本文档供接手该项目的 Agent 从零阅读，包含全部已确认决策与实现规格。

---

## 1. 项目目标

构建一个 **把 Word(.docx) 与 PDF 文档批量提取、整理为 Markdown 文件** 的 AI 工具。用户上传文档，工具完成内容提取、结构整理，输出单个 Markdown 文件（图片落盘到 `images/` 旁路引用）。

---

## 2. 已确认的需求决策（不可推翻，除非用户明确要求）

以下三点由用户在 2026-10-17/18 的需求澄清中亲自选定：

| 决策项 | 选择 | 影响 |
|---|---|---|
| 文档类型 | **混合场景**（学术+办公均需覆盖） | 必须做双提取链路 + 自动模式选择 |
| 产品形态 | **带 Web 界面** | FastAPI + 前端页面，不是纯 CLI |
| LLM 运行方式 | **云端 API** | 不需要本地模型，无需打包推理环境 |
| LLM 提供商 | **OpenAI / 兼容接口**（openai SDK） | provider 抽象设计，但先实现 OpenAI 一个能跑的 |

---

## 3. 技术选型（已定）

- **Python 3.11+**
- **后端**：FastAPI + uvicorn；后台任务用 `BackgroundTasks`（单机够用，**先不上 Celery**）
- **前端**：原生 HTML/CSS/JS 静态页，**不用 React/Vue**。工具类项目，上传→进度→预览→下载的交互不需要框架
- **提取链路**：
  | 环节 | 选择 | 说明 |
  |---|---|---|
  | docx | **pandoc 优先，mammoth 兜底** | pandoc 是独立二进制，pip 装不了；启动时检测，缺失自动降级 mammoth 并提示 |
  | PDF 文字版 | **PyMuPDF** | 提取文本 + 版式信息（字号/位置/字体），用于标题层级推断 |
  | PDF 扫描件 | PaddleOCR | 按页面文字密度预检自动判断 |
  | 复杂版式兜底 | 页面渲染成图 → 多模态 LLM | 用户在 UI 上手动选「高质量模式」触发，按页计费 |
- **LLM**：`openai` SDK，模型由环境变量配置。整理层只在「结构整理」时介入。

### 明确排除的技术（避免接手者重复踩坑）

- **不用 Marker / marker-pdf**：它的版式识别强，但依赖 torch，Windows 安动辄数 GB，部署成本过高。复杂版式交给多模态 LLM。
- **不让 LLM 直接输出全文 Markdown**：不可控、长文档成本不可预测、无法复核。改为：LLM 输出**严格 JSON 增量指令**，本地渲染 md。
- **不做静默纠错**：低置信度区块（坏表格、可疑公式）**打标输出 + 在 meta.json 中列出**，而非悄悄输出错误内容。

---

## 4. 项目结构（规划）

```
AllToMarkdown/
├── pyproject.toml
├── .env.example                  # OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
├── HANDOFF.md                    # 本文档
├── app/
│   ├── main.py                   # FastAPI 入口、生命周期、pandoc 检测
│   ├── api.py                    # upload / status / download / preview 路由
│   ├── schema.py                 # 统一中间表示（核心，见第 5 节）
│   ├── core/
│   │   ├── ingest.py             # 格式识别、扫描件预检（文字密度）
│   │   ├── extract_docx.py       # pandoc -> markdown，mammoth 兜底
│   │   ├── extract_pdf.py        # PyMuPDF 文本+版式提取
│   │   ├── ocr.py                # PaddleOCR 封装（Phase 4）
│   │   ├── llm.py                # provider 抽象，先实现 OpenAI
│   │   ├── organize.py           # 分块策略 + LLM 结构化整理
│   │   └── render.py             # 中间表示 -> 最终 markdown
│   └── tasks.py                  # BackgroundTask 状态机
├── static/                       # Web UI（index.html / style.css / app.js）
├── tasks/                        # 运行时任务产物，按 task_id 分目录
│   └── {task_id}/
│       ├── output.md
│       ├── images/
│       └── meta.json             # 置信度、需人工复核项、耗时、模式
└── tests/
    └── fixtures/                 # 放真实测试文档（docx + 文字版 PDF + 扫描件）
```

---

## 5. 核心设计：统一中间表示

所有提取器输出同一种结构，整理层和渲染层只认它。这是整个项目的骨架，**先定 schema 再写提取器**。

```python
# app/schema.py（示意，实现时按 pydantic 定义）
class BlockType(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    IMAGE = "image"
    CODE = "code"
    FORMULA = "formula"

class Block(BaseModel):
    id: str
    type: BlockType
    # 各 type 自身数据：heading 存 level+text，table 存 rows+cation，
    # image 存 path（相对 tasks/{id}/ 的路径），formula 存 LaTeX...
    data: dict
    confidence: float = 1.0        # 提取置信度，< 阈值进 meta.json 复核清单
    needs_review: bool = False
    source: BlockSource            # 页码 / 段落锚点，用于报错定位

class Document(BaseModel):
    id: str
    source_file: str
    mode: Literal["standard", "high_quality"]
    detected_kind: Literal["docx", "pdf_text", "pdf_scanned", "unknown"]
    blocks: list[Block]
```

约定：
- 表格一律存 `rows: list[list[str]]` + `caption`，**渲染时才转成 markdown 语法**。
- 图片提取后存 `tasks/{id}/images/xxx.png`，Block 中只存相对路径。
- `confidence` 最低阈值的 block 标 `needs_review=True`，写进 `meta.json`。

---

## 6. LLM 整理层设计（项目的技术核心）

### 6.1 分块策略（organize.py）

- 按**标题树**分块，同标题下的段落不拆开。
- **表格、公式、列表作为原子块，绝不被截断**。
- 每块附带其所在的标题路径（`第一章 > 1.2 小节`）作为上下文。
- 超长章节按段落边界二次切分，块与块之间用标题路径保持衔接。

### 6.2 Prompt 契约

给 LLM 的输入：一个 chunk 的 Block 列表（序列化为紧凑 JSON）+ 标题路径上下文。
要求输出**严格 JSON**，例如：

```json
{
  "blocks": [
    { "id": "<原 block id>", "action": "keep", "data": { ...整理后的数据... } },
    { "id": "...", "action": "merge", "with": "<另一个 id>" },
    { "id": "...", "action": "split", "into": [ { ... } ] }
  ],
  "flags": [
    { "id": "...", "reason": "表格疑似缺列", "severity": "high" }
  ]
}
```

要点：
- **JSON schema 强校验**，解析失败重试一次，再失败则保留规则提取的原始块。
- LLM 只能引用输入中存在的 block id，不允许新造内容（防止幻觉注入原文）。
- 渲染层（render.py）拿到 JSON 后在本地生成 markdown，LLM 不接触最终 md 文本。

---

## 7. Web API 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` | multipart 上传文件，参数 `mode=standard\|high_quality`；返回 `{ task_id }` |
| GET | `/api/tasks/{id}` | 状态：`pending / extracting / organizing / rendering / done / failed`，含进度百分比与错误信息 |
| GET | `/api/tasks/{id}/markdown` | 下载 `output.md`（Content-Disposition） |
| GET | `/api/tasks/{id}/preview` | 预览用：md 渲染成 HTML 片段 |
| GET | `/api/tasks/{id}/meta` | 置信度、复核清单、耗时统计 |
| GET | `/api/tasks/{id}/images/{name}` | 图片静态服务 |

前端用**轮询**（不上 WebSocket，工具项目不值得）拿 `GET /api/tasks/{id}` 的状态。

---

## 8. 分阶段路线与验收标准

### Phase 1 —— 纯规则 MVP（先做这个）

- 范围：`ingest` + `extract_docx` + `extract_pdf`（仅文字版）+ `render` + **CLI 入口**
- CLI 形态：`python -m app.cli <input.docx|.pdf> -o <out.md> --mode standard`
- **验收标准**：
  1. 拿用户提供的 3 份真实文档（1 个办公 docx、1 个学术 PDF、1 个含表格的 PDF）跑通
  2. 标题层级、段落、表格、列表正确还原，图片落盘到 `images/` 并正确引用
  3. 扫描件给出清晰报错（「未检测到文字层，请用 OCR 模式」）而非崩溃
- **不包含**：LLM、Web 界面、OCR

### Phase 2 —— LLM 整理层

- 范围：`llm.py` + `organize.py` + 分块策略 + JSON 契约
- 验收：分块不截断表格/列表；JSON 校验有重试；整理后的标题层级明显优于规则提取；低置信区块进 meta.json

### Phase 3 —— Web 界面

- 范围：FastAPI + 静态 UI + BackgroundTasks 状态机
- 验收：上传→进度轮询→预览（含复核标黄）→下载 md 和 images 压缩包

### Phase 4 —— 难点兜底

- 范围：PaddleOCR 扫描件链路 + 多模态「高质量模式」
- 验收：扫描件可转；高质量模式对多栏/跨页表格有明显改善

---

## 9. 环境与开发须知

- **操作系统 Windows 11**，Claude Code 使用 bash shell（路径用正斜杠，重定向用 `/dev/null`）。
- **项目目前还没有 git 仓库**，建议接手第一步先 `git init` 并提交 Phase 1 初始代码。
- 项目目录：`D:\AIProject\AllToMarkdown`；当前**只有本交接文档，尚未写任何代码**。
- 依赖（Phase 1 最小集）：`fastapi uvicorn python-multipart python-docx mammoth pymupdf pillow pydantic`；Phase 2 起：`openai`；Phase 4：`paddleocr`（安装重，放最后）。
- pandoc 需单独安装 Windows 二进制（https://pandoc.org/installing.html），代码中必须做存在性检测。
- 用户的之前的项目记忆存于 `C:\Users\Lsssss\.claude\projects\D--AIProject-AllToMarkdown\memory\project_alltomarkdown.md`，内容与本文档第 2-3 节一致。

---

## 10. 立即开始的建议

1. 先 `git init`，建好 `pyproject.toml` 和第 4 节的目录骨架
2. 实现 `schema.py`（第 5 节），这是其余所有模块的契约
3. 向用户要 3 份真实测试文档放进 `tests/fixtures/`（文档质量决定 Phase 1 验收是否真实有效）
4. 跑 Phase 1，用 CLI 验收，再进入 Phase 2

---

## 11. Phase 1 实现进度（2026-10-19 更新 ✅）

> 本节由接手 Agent 在完成 Phase 1 后追加，记录已落地/偏离原设计的地方，供下一阶段参考。

### 已完成（对应第 8 节 Phase 1 全部验收标准）

- 代码骨架与 `app/schema.py` / `app/core/*` / `app/cli.py` 全部实现，`git init` 并已提交首版。
- `python -m app.cli <docx|pdf> -o out.md --mode standard` 可运行。
- 测试：`tests/` 下 28 个用例全部通过（schema / ingest / render / docx / pdf 全覆盖），fixtures 含 `sample.docx`、`sample.pdf`（文字版）、`scanned_sample.pdf`（扫描件）。

### 对交接文档的实现强化（设计决策落地为代码）

1. **原子块在 schema 层显式标注**：`ATOMIC_TYPES` + `Block.atomic`（表格/公式/列表/代码），由 `model_validator` 按 type 自动推断，组织(organize)层必须遵守——而非仅靠 organize.py 内部逻辑判断（对应第 5 节、6.1 节）。
2. **mammoth 兜底改用 HTML 解析**：mammoth 的 markdown 转换**不输出表格**（会把每个单元格打成独立段落），且会对图片路径做 `\.` 转义。因此 mammoth 路径走 `convert_to_html` + 极简 `html.parser` 解析（`_HtmlBlocksParser`），高保真还原表格/列表/标题（对应第 3 节“pandoc 优先，mammoth 兜底”）。
3. **pandoc 图片真实落盘**：pandoc 用 `--extract-media` 抽取内嵌图片到临时目录，再归一为 `images/` 并重写 md 引用；mammoth 用 `convert_image` 回调直接落盘（对应第 5 节“图片落盘 images/”）。
4. **PDF 段落智能聚合 + 中文断行修复**：按行垂直间距判断段落边界（避免同页多段被合并）；段落内用 CJK 边界规则拼接（避免“采 用 / 结 果”之类凭空空格）；标题层级按字号/加粗/居中推断（第 5 节版式标题推断落地）。
5. **扫描件预检阈值可配置**：`PDF_TEXT_DENSITY_THRESHOLD`（默认 80 字符/页），按“文字页占比 ≥60%”判定，避免整份 PDF 因个别空洞页误判（对应第 3 节扫描件预检）。

### 已知限制（日后迭代方向）

- **PDF 表格尚未识别**：当前 PDF 链路只做文本+版式推断（标题/段落），**不含表格提取**。mammoth 路径的表格提取依赖 HTML 结构，PDF 需待后续版本（用列坐标聚类或 Phase 4 高质量模式）。
- **PDF 标题层级为启发式**：级别推断（H1-H6）对居中/加粗判定的文档质量依赖较高，复杂版式靠 Phase 2 LLM 整理层优化。
- **docx 用 mammoth 兜底时**见上方强化 2：表格能还原，但复杂样式（页眉页脚、嵌入对象、批注）不处理。
- pandoc 未强制安装：代码运行时检测，缺失自动降级 mammoth 并给出提示（对应第 3 节）。

### 运行方式

```bash
# 安装依赖（Phase 1 最小集）
pip install pydantic pymupdf python-docx mammoth pillow pytest
# 跑测试
python -m pytest tests/ -v
# 命令行提取
python -m app.cli tests/fixtures/sample.docx -o out.md
python -m app.cli tests/fixtures/sample.pdf -o out.md
```

### 下一步（接 Phase 2）

- `llm.py` + `organize.py`：分块策略（标题树 + 原子块不拆）+ JSON 增量指令契约（第 6 节）。
- `tasks.py` + FastAPI：Web 界面 + BackgroundTasks 状态机（第 7 节、Phase 3）。
- 用真实用户文档增强 fixtures，验证 Phase 1 验收标准在真实文档上的表现。

---

## 12. Phase 2 / Phase 3 实现进度（2026-10-19 更新 ✅）

### Phase 2 —— LLM 整理层（已完成，12 个独立测试全过）

- `app/core/llm.py`：`LLMProvider` 抽象 + `OpenAIProvider`（openai SDK，兼容 DeepSeek 等 `/chat/completions` 端点）；`parse_json_response` 剥离 ```json 围栏并严格解析；`complete_json` 内置失败重试。client 延迟实例化，无 API key 时仍可 import（便于测试/注入 FakeProvider）。
- `app/core/organize.py`：
  - **分块（chunk_document）**：按标题树分块，同一标题路径归并；原子块（表格/公式/列表/代码）**绝不被截断**——切分只发生在标题边界或非原子段落之间；超长 chunk 在非原子块处二次切分并共享标题路径。
  - **LLM 整理（organize_document / organize_chunk）**：把 chunk 序列化为紧凑 JSON 喂给 LLM，要求输出**严格 JSON 增量指令**（keep / merge / split / drop + flags），最后 md 始终由 render 层本地生成（LLM 不接触最终文本）。
  - **强约束**：指令只能引用输入中已有的 block id（`_validate_instructions` 校验，防止幻觉注入原文）；JSON 解析失败或校验失败**重试 1 次仍失败则保留规则提取的原始块**并写入 meta，不静默输出错误内容。
  - `_apply_instructions` 支持 keep（更新 data）/ merge（文本/列表项拼接）/ split（替换为 into 列表）/ drop。
  - Chunk 附标题路径上下文（`第一章 > 1.2 小节`）喂给 LLM。
- CLI 新增 `-O / --organize` 开关调用整理层。

### Phase 3 —— Web 界面（已完成，后端 API 全链路验证通过）

- `app/tasks.py`：单机任务状态机。状态 `pending -> extracting -> organizing(可选) -> rendering -> done/failed`；进度 0-100；线程安全 registry（dict + lock）；任务目录 `tasks/{id}/` 产出 `output.md / output.meta.json / images/`；`start_task` 在独立线程执行不阻塞请求。
- `app/api.py`：按第 7 节契约实现全部端点（见下方「Web API」）。上传做扩展名校验、文件名清洗（防路径穿越）；预览返回原始 markdown，前端用 marked.js 渲染。
- `app/main.py`：FastAPI 应用 + CORS + 静态挂载（根路径服务 static/index.html）。
- `static/`：`index.html` / `style.css` / `app.js`。「纸页·工作台（Editorial Paper Workbench）」设计语言——暖纸白 + 墨黑 + 深青墨强调 + 琥珀点缀，衬线标题（Noto Serif SC）配无衬线正文（Outfit）；上传→进度轮询→预览（含校对清单标黄）→下载 md 与 zip。
- 前端已修复两个类名不匹配 bug：`[data-reveal]` 依赖的 `.is-in` 应为 `.is-revealed`（交互类名统一）；dropzone 拖拽类 `is-drag` 应为 `is-dragover`。

### 全量测试

`python -m pytest tests/ -q` → **40 passed**（Phase1 的 28 + Phase2 的 12），覆盖 schema / ingest / render / docx / pdf / organize。

### Web API 契约（已实现）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` | multipart 上传 `file + mode + organize`，返回 `{task_id}` |
| GET | `/api/tasks` | 最近任务列表（首页概览） |
| GET | `/api/tasks/{id}` | 状态 + 进度 + 步骤标签 + 错误 |
| GET | `/api/tasks/{id}/markdown` | 下载 output.md |
| GET | `/api/tasks/{id}/preview` | 返回原始 markdown（前端 marked 渲染） |
| GET | `/api/tasks/{id}/meta` | 置信度/复核清单/耗时/模式 |
| GET | `/api/tasks/{id}/archive` | 打包 output.md + images/ 为 zip |
| GET | `/api/tasks/{id}/images/{name}` | 图片静态服务 |
| DELETE | `/api/tasks/{id}` | 清理任务目录与状态 |

### 启动 Web 服务

```bash
pip install fastapi "uvicorn[standard]" python-multipart httpx
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 浏览器打开 http://127.0.0.1:8000/
```

### 已知限制（接 Phase 4 / 后续）

- **PDF 表格尚未识别**：PDF 链路只做文本+版式（标题/段落），表格提取待后续（列坐标聚类或 Phase 4 高质量模式）。
- **LLM 整理需 OPENAI_API_KEY**：未配置时 organize 会走到失败回退（保留原始块），不影响 standard 提取。
- **扫描件（PDF 无文字层）**：Phase 1-3 给出清晰报错，OCR 链路留待 Phase 4（PaddleOCR）。
- **「高质量模式」**：目前 UI 已展示但 disabled，需 Phase 4 多模态 LLM 落地。
