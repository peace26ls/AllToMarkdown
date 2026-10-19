"""docx 提取链路：pandoc 优先，mammoth 兜底。

两种工具都产出 markdown 文本流，本模块统一把它们轻量解析成 Block 结构，
喂给统一的中间表示（schema.Document），与 PDF 链路共享相同契约。

设计要点（相对交接文档的优化点）：
- pandoc 优先（版式还原度更高）。pandoc 是独立二进制，运行时检测，缺失自动降级 mammoth。
- **图片真实落盘**：pandoc 用 `--extract-media` 抽取内嵌图片到临时 media 目录，
  再统一归一到 output_dir/images/ 下并重写引用；mammoth 路径用 convert_image 回调直接落盘。
  这样 IMAGE Block 里的 path 始终指向真实存在的文件（交接文档仅"规划"，这里落实到实现）。
- mammoth 兜底时对复杂样式还原弱，标题/表格置信度适当下调，走复核清单。
"""
from __future__ import annotations

import html
import html.parser
import re
import shutil
import subprocess
from pathlib import Path

from app.schema import Block, BlockType, Document, Source

# 常见 markdown 块级正则
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RE_HR = re.compile(r"^\s*([-*_])(\s*\1\s*){2,}\s*$")
_RE_FENCE_START = re.compile(r"^```")
_RE_OL = re.compile(r"^(\d+)\.\s+(.*)$")
_RE_UL = re.compile(r"^\s*[-*+]\s+(.*)$")
_RE_IMG = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_RE_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")


def pandoc_available() -> bool:
    """检测 pandoc 二进制是否可用。"""
    return shutil.which("pandoc") is not None


def _pandoc_to_md(file: Path, output_dir: Path) -> tuple[str, list[str]]:
    """调用 pandoc 转 markdown，并把内嵌图片抽取到 output_dir/images/。

    返回 (markdown 文本, 落盘图片相对路径列表)。
    pandoc 用 --extract-media 抽取，输出到 `<output_dir>/_media_` 下，
    之后被 _normalize_media 归一到 images/。
    """
    media_tmp = output_dir / "_media_"
    media_tmp.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["pandoc", str(file), "-t", "gfm", "--wrap=none",
             f"--extract-media={media_tmp}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pandoc 返回码 {proc.returncode}: {proc.stderr[:500]}")
        return proc.stdout, []
    except FileNotFoundError:
        raise RuntimeError("pandoc 未安装")
    except subprocess.TimeoutExpired:
        raise RuntimeError("pandoc 转换超时")


def _normalize_media(output_dir: Path, md: str) -> tuple[str, list[str]]:
    """把 pandoc 抽取到 `_media_/` 的图片归一到 `images/`，并重写 md 中的引用。

    返回 (修正后的 markdown, images/ 下的相对路径列表)。
    """
    img_dir = output_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    media_dir = output_dir / "_media_"
    images: list[str] = []

    if media_dir.exists():
        for f in sorted(media_dir.rglob("*")):
            if f.is_file():
                dest = img_dir / f.name
                shutil.move(str(f), str(dest))
                images.append(f"images/{f.name}")
        # 清掉空的临时目录
        shutil.rmtree(media_dir, ignore_errors=True)

    # 重写 md 中指向 media/ 的图片引用为 images/
    md = re.sub(r"\]\(media/([^)]+)\)", r"](images/\1)", md)
    return md, images


def _mammoth_to_md(file: Path, output_dir: Path) -> tuple[str, list[str]]:
    """调用 mammoth 转 markdown，把图片通过 convert_image 回调落到 images/。

    返回 (markdown 文本, 图片相对路径列表)。
    """
    import mammoth

    images: list[str] = []
    img_dir = output_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    def convert_image(image) -> dict:
        ext = (image.content_type or "").split("/")[-1] or "png"
        if ext not in ("png", "jpeg", "jpg", "gif", "bmp", "svg"):
            ext = "png"
        name = f"I{len(images) + 1}.{ext}"
        with image.open() as stream:
            data = stream.read()
        (img_dir / name).write_bytes(data)
        images.append(f"images/{name}")
        return {"src": f"images/{name}"}

    with open(file, "rb") as f:
        result = mammoth.convert_to_markdown(
            f, convert_image=mammoth.images.img_element(convert_image)
        )
    return result.value, images


def _mammoth_to_html(file: Path, output_dir: Path) -> tuple[str, list[str]]:
    """调用 mammoth 转 HTML，并把图片通过 convert_image 回调落到 images/。

    返回 (HTML 文本, 图片相对路径列表)。HTML 保留了表格/标题/列表等结构，
    用于 _html_to_blocks 做高保真的结构还原（mammoth 的 markdown 转换不输出表格）。
    """
    import mammoth

    images: list[str] = []
    img_dir = output_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    def convert_image(image) -> dict:
        ext = (image.content_type or "").split("/")[-1] or "png"
        if ext not in ("png", "jpeg", "jpg", "gif", "bmp", "svg"):
            ext = "png"
        name = f"I{len(images) + 1}.{ext}"
        with image.open() as stream:
            data = stream.read()
        (img_dir / name).write_bytes(data)
        images.append(f"images/{name}")
        return {"src": f"images/{name}"}

    with open(file, "rb") as f:
        result = mammoth.convert_to_html(
            f, convert_image=mammoth.images.img_element(convert_image)
        )
    return result.value, images


class _HtmlBlocksParser(html.parser.HTMLParser):
    """极简 HTML -> Block 解析器，专为 mammoth 输出设计。

    只关心本项目需要的元素：h1-h6 / p / ul+ol+li / table / img / pre。其余忽略。
    """

    # 块级容器：进入后开始累计文本
    _BLOCK_TAGS = {
        "h1": ("heading", 1), "h2": ("heading", 2), "h3": ("heading", 3),
        "h4": ("heading", 4), "h5": ("heading", 5), "h6": ("heading", 6),
        "p": ("paragraph", None), "li": ("list_item", None),
    }

    def __init__(self, base_confidence: float):
        super().__init__(convert_charrefs=True)
        self.base_confidence = base_confidence
        self.blocks: list[Block] = []
        self._counter = 0
        # 解析状态
        self._cur_kind: str | None = None
        self._cur_level: int | None = None
        self._buf: list[str] = []
        self._open_list: bool = False
        self._list_ordered: bool = False
        # 表格状态
        self._in_table = False
        self._table_rows: list[list[str]] = []
        self._cur_row: list[str] | None = None
        self._bound_text = ""

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _flush(self) -> None:
        """flush 当前累计的块级文本为 Block。"""
        if self._cur_kind is None:
            return
        text = self._clean("".join(self._buf)).strip()
        if text:
            if self._cur_kind == "heading":
                self.blocks.append(Block(
                    id=self._next_id("h"), type=BlockType.HEADING,
                    data={"level": self._cur_level, "text": text},
                    confidence=self.base_confidence,
                ))
            elif self._cur_kind == "paragraph":
                self.blocks.append(Block(
                    id=self._next_id("p"), type=BlockType.PARAGRAPH,
                    data={"text": text}, confidence=self.base_confidence,
                ))
            elif self._cur_kind == "list_item":
                # 列表项累积到当前打开的列表块
                if self.blocks and self.blocks[-1].type == BlockType.LIST:
                    self.blocks[-1].data.setdefault("items", []).append(text)
                else:
                    self.blocks.append(Block(
                        id=self._next_id("l"), type=BlockType.LIST,
                        data={"items": [text], "ordered": self._list_ordered},
                        confidence=self.base_confidence,
                    ))
        self._buf = []
        self._cur_kind = None
        self._cur_level = None

    @staticmethod
    def _clean(text: str) -> str:
        import re as _re
        # 去掉换行带来的多余空白，压缩连续空白
        return _re.sub(r"\s+", " ", text)

    def handle_starttag(self, tag, attrs):
        if tag in self._BLOCK_TAGS:
            self._flush()
            kind, level = self._BLOCK_TAGS[tag]
            self._cur_kind = kind
            self._cur_level = level
            if tag == "li":
                self._open_list = True
            return
        if tag == "ul":
            self._list_ordered = False
            return
        if tag == "ol":
            self._list_ordered = True
            return
        if tag == "table":
            self._flush()
            self._in_table = True
            self._table_rows = []
            return
        if tag == "tr":
            if self._in_table:
                self._cur_row = []
            return
        if tag == "td" or tag == "th":
            self._bound_text = ""
            return
        if tag == "img":
            src = dict(attrs).get("src", "")
            if src:
                # 独立图片块；若处于表格单元格内则作为单元格文本
                if self._in_table and self._cur_row is not None:
                    self._bound_text += f"![图片]({src})"
                else:
                    self._flush()
                    self.blocks.append(Block(
                        id=self._next_id("img"), type=BlockType.IMAGE,
                        data={"path": src, "alt": "image"},
                        confidence=self.base_confidence,
                    ))
            return
        if tag == "br":
            if self._cur_kind:
                self._buf.append(" ")
            return

    def handle_endtag(self, tag):
        if tag in self._BLOCK_TAGS:
            self._flush()
            return
        if tag == "ul" or tag == "ol":
            self._open_list = False
            return
        if tag == "td" or tag == "th":
            if self._in_table and self._cur_row is not None:
                self._cur_row.append(self._clean(self._bound_text).strip())
                self._bound_text = ""
            return
        if tag == "tr":
            if self._in_table and self._cur_row is not None:
                self._table_rows.append(self._cur_row)
                self._cur_row = None
            return
        if tag == "table":
            self._flush()
            if self._table_rows:
                self.blocks.append(Block(
                    id=self._next_id("t"), type=BlockType.TABLE,
                    data={"rows": self._table_rows, "caption": ""},
                    confidence=self.base_confidence,
                ))
            self._in_table = False
            self._table_rows = []
            return

    def handle_data(self, data):
        if self._in_table and self._cur_row is not None:
            self._bound_text += data
        elif self._cur_kind:
            self._buf.append(data)


def _html_to_blocks(html: str, base_confidence: float) -> list[Block]:
    """把 mammoth 生成的 HTML 解析为 Block 列表（高保真还原表格/列表）。"""
    parser = _HtmlBlocksParser(base_confidence)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # 任何解析异常都不致命：退回把实体去除后的纯文本段落
        return []
    return parser.blocks


def _parse_blocks(md: str, images: list[str], base_confidence: float) -> list[Block]:
    """把 markdown 文本流轻量解析成 Block 列表（对齐 schema 契约）。

    只处理本项目关心的块类型：heading / paragraph / list / code / image / table / rule。
    """
    blocks: list[Block] = []
    lines = md.split("\n")
    i = 0
    n = len(lines)
    counter = 0

    def next_id(prefix: str) -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}{counter}"

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # 代码围栏
        if _RE_FENCE_START.match(stripped):
            code_lines = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过收尾 ```
            blocks.append(Block(
                id=next_id("c"),
                type=BlockType.CODE,
                data={"text": "\n".join(code_lines).rstrip(), "language": ""},
                confidence=base_confidence,
            ))
            continue

        # 表格：以 | 开头，且下一行是分隔行
        if stripped.startswith("|") and i + 1 < n and re.match(
            r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1].strip()
        ):
            table_block, i_new = _consume_table(lines, i, next_id, base_confidence)
            blocks.extend(table_block)
            i = i_new
            continue

        # 水平线
        if _RE_HR.match(stripped):
            blocks.append(Block(
                id=next_id("hr"), type=BlockType.RULE, data={},
                confidence=base_confidence,
            ))
            i += 1
            continue

        # 标题
        m = _RE_HEADING.match(stripped)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            blocks.append(Block(
                id=next_id("h"),
                type=BlockType.HEADING,
                data={"level": level, "text": _inline_to_plain(text)},
                confidence=base_confidence,
            ))
            i += 1
            continue

        # 独立图片行
        if _RE_IMG.match(stripped) and not _RE_UL.match(stripped):
            m = _RE_IMG.match(stripped)
            blocks.append(Block(
                id=next_id("img"),
                type=BlockType.IMAGE,
                data={"path": m.group(2).replace("\\", "/"), "alt": m.group(1)},
                confidence=base_confidence,
            ))
            i += 1
            continue

        # 列表：收集连续列表项
        if _RE_UL.match(stripped) or _RE_OL.match(stripped):
            list_block, i_new = _consume_list(lines, i, next_id, base_confidence)
            blocks.extend(list_block)
            i = i_new
            continue

        # 普通段落（可能多行直到空行）
        para_lines = [stripped]
        i += 1
        while i < n and lines[i].strip():
            para_lines.append(lines[i].strip())
            i += 1
        blocks.append(Block(
            id=next_id("p"),
            type=BlockType.PARAGRAPH,
            data={"text": " ".join(para_lines)},
            confidence=base_confidence,
        ))

    return blocks


def _consume_table(lines, i, next_id, base_confidence):
    """读取 GFM 表格，返回 ([Table block], 新的行索引)。"""
    header_line = lines[i].strip().strip("|")
    header = [c.strip() for c in header_line.split("|")]
    j = i + 2  # 跳过分隔行
    rows: list[list[str]] = [header]
    n = len(lines)
    while j < n and lines[j].strip().startswith("|"):
        row = [c.strip() for c in lines[j].strip().strip("|").split("|")]
        rows.append(row)
        j += 1
    block = Block(
        id=next_id("t"),
        type=BlockType.TABLE,
        data={"rows": rows, "caption": ""},
        confidence=base_confidence,
    )
    return [block], j


def _consume_list(lines, i, next_id, base_confidence):
    """收集连续列表项为一个 LIST 块。"""
    kinds: list[str] = []
    items: list[str] = []
    n = len(lines)
    j = i
    while j < n:
        stripped = lines[j].strip()
        mu = _RE_UL.match(stripped)
        mo = _RE_OL.match(stripped)
        if mu:
            kinds.append("ul")
            items.append(_inline_to_plain(mu.group(1).strip()))
            j += 1
        elif mo:
            kinds.append("ol")
            items.append(_inline_to_plain(mo.group(1).strip()))
            j += 1
        else:
            break
    block = Block(
        id=next_id("l"),
        type=BlockType.LIST,
        data={"items": items, "ordered": any(k == "ol" for k in kinds)},
        confidence=base_confidence,
    )
    return [block], j


def _inline_to_plain(text: str) -> str:
    """去掉行内 markdown 标记（粗体/斜体/链接），保留纯文本。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = _RE_LINK.sub(r"\1", text)
    return text.strip()


def extract(file: Path, output_dir: Path, *, prefer_pandoc: bool = True) -> Document:
    """从 docx 提取中间表示（统一入口，与 PDF 链路签名一致）。

    Args:
        file: 输入的 .docx 路径
        output_dir: 输出目录（图片等资源落盘于此，Block 存相对路径）
        prefer_pandoc: 是否优先使用 pandoc；False 强制用 mammoth（便于测试）

    Returns:
        Document，detected_kind="docx"，mode 由调用方回填。
    """
    use_pandoc = prefer_pandoc and pandoc_available()

    if use_pandoc:
        try:
            md, _tmp = _pandoc_to_md(file, output_dir)
            md, images = _normalize_media(output_dir, md)
            blocks = _parse_blocks(md, images, 0.95)
            tool = "pandoc"
        except RuntimeError:
            # pandoc 存在但转换失败，降级 mammoth（HTML 解析，保表格）
            html, images = _mammoth_to_html(file, output_dir)
            blocks = _html_to_blocks(html, 0.85)
            tool = "mammoth(fallback)"
    else:
        # mammoth HTML 解析：比 markdown 转换保真度高（表格/结构可还原）
        html, images = _mammoth_to_html(file, output_dir)
        blocks = _html_to_blocks(html, 0.85)
        tool = "mammoth"

    # 全局默认来源标注（docx 无页码；段落锚点后续可按需填充）
    for b in blocks:
        b.source = Source(description=str(file.name))

    return Document(
        id=file.stem,
        source_file=str(file),
        mode="standard",
        detected_kind="docx",
        blocks=blocks,
        notes=[f"提取工具: {tool}"],
    )
