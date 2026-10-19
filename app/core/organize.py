"""LLM 结构化整理层（Phase 2 技术核心）。

职责（对应交接文档第 6 节）：
1. **分块（chunk_document）**：按标题树把 Document 的 blocks 切成若干 chunk，
   每个 chunk 附标题路径作为上下文；表格/公式/列表/代码等**原子块**绝不被截断。
2. **LLM 整理（organize_document）**：对每个 chunk，把 blocks 序列化为紧凑 JSON
   交给 LLM，要求输出**严格 JSON 增量指令**（keep / merge / split / drop），
   在本地按指令重排/修正，避免让 LLM 直接输出全文 Markdown。

设计原则（这些是让"整理"可控、可复核、不产生幻觉的关键）：
- **只允许引用输入中已存在的 block id**：LLM 不得发明新 id / 新内容，防止幻觉注入原文。
- **严格 JSON schema 校验 + 重试**：解析失败或校验失败重试一次；仍失败则保留规则提取的原始块，
  并把该 chunk 记录到需人工复核清单（不静默输出错误内容）。
- **增量而非全文**：LLM 产出的是"操作指令"，最终 md 始终由 render 层本地生成。

关于 LLM 调用协议：
- 本模块不直接依赖任何具体 provider，只接收一个 ``llm_call`` 可调用对象。
- 协议：``llm_call(system: str, user: str) -> dict``，由调用方负责把 LLM 输出解析为 dict
  （可通过 app.core.llm 的 OpenAIProvider + complete_json 构造），失败应在内部完成重试后抛异常。
- 这样既满足"provider 抽象"，也便于在测试中注入 stub。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app.schema import ATOMIC_TYPES, Block, BlockType, Document

logger = logging.getLogger(__name__)

# LLM 每条增量指令允许的动作
_ACTIONS = frozenset({"keep", "merge", "split", "drop"})

# "文本整理"适用的块类型：其 data 是文本可被 LLM 改写；其余（表格/图片/代码/公式）
# 是结构化形态，不要求 LLM 改写文本，避免破坏结构
_TEXT_TYPES = frozenset({BlockType.HEADING, BlockType.PARAGRAPH, BlockType.LIST})

# llm_call 可调用签名：(system, user) -> dict
LLMCall = Callable[[str, str], dict]


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    """组织层专用的一个内容块。"""

    id: str
    # 标题路径，如 ["第一章 引言", "1.2 小节"]，作为上下文喂给 LLM
    path: list[str]
    blocks: list[Block]
    # 该 chunk 首个 block 在原 doc.blocks 中的下标，用于重排回写
    start_idx: int
    # 整理失败时的备注（仅供 meta 记录，非核心字段）
    _fallback_note: str | None = field(default=None, repr=False)


@dataclass
class OrganizeResult:
    """组织层的最终产出。"""

    blocks: list[Block]                     # 整理后的全部 blocks（按原顺序）
    meta: list[dict] = field(default_factory=list)  # flags / 保留回退项 / 低置信
    chunks_unorganized: int = 0             # 因失败而保留原始块的 chunk 数


def _path_str(path: list[str]) -> str:
    return " > ".join(path) if path else "（文档根）"


def _serialize_block(b: Block) -> dict:
    """把 block 序列化成喂给 LLM 的紧凑 dict（剔除内部定位/审计字段）。"""
    d: dict[str, Any] = {"id": b.id, "type": b.type.value}
    if b.type == BlockType.TABLE:
        # 表格只给行列数与 caption，不塞全量数据（避免超长），整理层不要求改写表格
        data = b.data or {}
        rows = data.get("rows") or []
        d["data"] = {
            "n_rows": len(rows),
            "n_cols": max((len(r) for r in rows), default=0),
            "caption": data.get("caption") or "",
        }
    else:
        d["data"] = b.data
    return d


def chunk_document(doc: Document, *, max_blocks_per_chunk: int = 40) -> list[Chunk]:
    """按标题树把 Document 切成若干 chunk。

    规则（对应交接文档 6.1）：
    - 标题块开启新 chunk（同时更新标题路径上下文）；同路径下的段落/表格/列表/图片归入该 chunk。
    - 原子块（表格/公式/列表/代码）绝不从中间截断——切分只发生在标题边界或非原子块之间。
    - chunk 超过 max_blocks_per_chunk 时，在非原子块处断开并共享同一标题路径。
    """
    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []  # (level, text)

    cur_blocks: list[Block] = []
    cur_path: list[str] = []
    cur_start = 0

    def take() -> None:
        """收尾当前 chunk。"""
        nonlocal cur_blocks, cur_start
        if cur_blocks:
            chunks.append(Chunk(
                id=uuid.uuid4().hex[:8],
                path=list(cur_path),
                blocks=cur_blocks,
                start_idx=cur_start,
            ))
        cur_blocks = []
        # cur_start 会在下一次 append 首块时重新赋值

    for idx, b in enumerate(doc.blocks):
        if b.type == BlockType.HEADING:
            level = int(b.data.get("level", 1))
            text = str(b.data.get("text", "")).strip()
            # 先收尾上一个 chunk（沿用旧的标题路径），再切换到新标题路径
            take()
            # 维护标题栈：弹出所有 >= 当前级别的标题（同级/上级标题关闭当前小节）
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
            cur_path = [t for _, t in heading_stack]
            cur_start = idx
            cur_blocks.append(b)
            continue

        # 非标题块
        if len(cur_blocks) >= max_blocks_per_chunk and b.type not in ATOMIC_TYPES:
            take()
        if not cur_blocks:
            cur_start = idx
        cur_blocks.append(b)

    take()
    return chunks


# ---------------------------------------------------------------------------
# JSON 解析与指令校验（可独立测试）
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json(text: str) -> str:
    """从 LLM 回复中提取最可能的 JSON 文本（容忍 ```json 包裹）。"""
    if not text:
        return ""
    t = text.strip()
    m = _JSON_FENCE_RE.search(t)
    if m:
        return m.group(1).strip()
    a = t.find("{")
    b = t.rfind("}")
    if a != -1 and b != -1 and b > a:
        return t[a : b + 1]
    return t


def parse_json_response(text: str) -> dict:
    """解析 LLM 的 JSON 回复。失败抛 json.JSONDecodeError。"""
    cleaned = _extract_json(text)
    if not cleaned:
        raise json.JSONDecodeError("空回复", text, 0)
    obj = json.loads(cleaned)
    if not isinstance(obj, dict):
        raise json.JSONDecodeError("顶层必须是对象", cleaned, 0)
    return obj


def _validate_instructions(chunk: Chunk, parsed: dict) -> list[str]:
    """校验指令，返回错误列表（空 = 通过）。

    强约束：指令只能引用 chunk 中已存在的 block id；不得发明新 id。
    """
    errors: list[str] = []
    existing = {b.id for b in chunk.blocks}
    blocks_spec = parsed.get("blocks")

    if blocks_spec is None:
        return ["缺少 blocks 字段"]
    if not isinstance(blocks_spec, list):
        return ["blocks 必须是数组"]

    for i, ins in enumerate(blocks_spec):
        if not isinstance(ins, dict):
            errors.append(f"blocks[{i}] 不是对象")
            continue
        bid = ins.get("id")
        if not isinstance(bid, str):
            errors.append(f"blocks[{i}] 缺少 id")
            continue
        if bid not in existing:
            errors.append(f"blocks[{i}] 引用了不存在的 id: {bid}")
            continue
        action = ins.get("action")
        if action not in _ACTIONS:
            errors.append(f"blocks[{i}] action 非法: {action}")
            continue
        if action == "merge":
            with_id = ins.get("with")
            if with_id not in existing:
                errors.append(f"blocks[{i}] merge.with 非法: {with_id}")
        if action == "split":
            into = ins.get("into")
            if not isinstance(into, list) or not into:
                errors.append(f"blocks[{i}] split.into 必须是非空数组")

    flags = parsed.get("flags")
    if flags is not None:
        if not isinstance(flags, list):
            errors.append("flags 必须是数组")
        else:
            for j, f in enumerate(flags):
                if not isinstance(f, dict):
                    errors.append(f"flags[{j}] 不是对象")
                    continue
                fid = f.get("id")
                if fid is not None and fid not in existing:
                    errors.append(f"flags[{j}] 指向不存在的 id: {fid}")
    return errors


def _apply_instructions(chunk: Chunk, parsed: dict) -> list[Block]:
    """把增量指令应用到 chunk 的 blocks，返回整理后的 blocks（保持原相对顺序）。"""
    by_id = {b.id: b for b in chunk.blocks}

    new_by_id: dict[str, Block] = {}
    killed: set[str] = set()
    split_by_id: dict[str, list[Block]] = {}

    def gen_id() -> str:
        return uuid.uuid4().hex[:10]

    for ins in parsed.get("blocks") or []:
        bid = ins["id"]
        action = ins.get("action")

        if action == "keep":
            blk = by_id[bid]
            data = ins.get("data")
            new_by_id[bid] = blk.model_copy(update={"data": data}) if isinstance(data, dict) else blk

        elif action == "merge":
            with_id = ins.get("with")
            target = by_id[bid]
            src = by_id[with_id]
            t_data = dict(target.data)
            s_data = dict(src.data)

            def _join(a, b):
                return (str(a.get("text", "")) + " " + str(b.get("text", ""))).strip()

            if target.type in (BlockType.HEADING, BlockType.PARAGRAPH):
                t_data["text"] = _join(t_data, s_data)
            elif target.type == BlockType.LIST:
                t_data["items"] = list(t_data.get("items", [])) + list(s_data.get("items", []))
            new_by_id[bid] = target.model_copy(update={"data": t_data})
            killed.add(with_id)

        elif action == "split":
            base = by_id[bid]
            generated: list[Block] = []
            for item in ins.get("into", []):
                if isinstance(item, dict):
                    generated.append(base.model_copy(
                        update={"id": gen_id(), "data": item.get("data", {})}))
            if generated:
                split_by_id[bid] = generated
                killed.add(bid)

        elif action == "drop":
            killed.add(bid)

    # 按原顺序重建，保序
    result: list[Block] = []
    for b in chunk.blocks:
        if b.id in killed:
            continue
        gen = split_by_id.get(b.id)
        if gen is not None:
            result.extend(gen)
            continue
        result.append(new_by_id.get(b.id, b))
    return result


# ---------------------------------------------------------------------------
# LLM 调用与整理主流程
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "你是一个中文文档结构整理助手。用户会给你一个 JSON，其中 blocks 字段是一个数组，"
    "每个元素代表文档中的一个内容块（有 id、type、data）。你的任务是修正明显的结构问题，"
    "并输出增量整理指令。\n"
    "重要约束：\n"
    "1. 只能引用输入中已经存在的 id，绝对不能凭空发明新 id 或捏造原文档没有的内容。\n"
    "2. 不要改写表格/图片/代码/公式块的结构化 data；对它们只需保留（action 用 keep，data 可省略）。\n"
    "3. 允许的 action：keep（保留，可附修正后的 data）、merge（合并，需 with 指向另一现存 id）、\n"
    "   split（拆分，into 为非空数组）、drop（删除无关/重复内容）。\n"
    "4. 必须输出**纯 JSON**，不要任何多余文字，格式如下：\n"
    '{"blocks":[{"id":"<现存id>","action":"keep","data":{...}},'
    '{"id":"<现存id>","action":"merge","with":"<另一现存id>"},'
    '{"id":"<现存id>","action":"split","into":[{"data":{...}}]},'
    '{"id":"<现存id>","action":"drop"}],"flags":[{"id":"<现存id>","reason":"问题说明","severity":"low|high"}]}\n'
    "5. 若没有问题需要处理，返回 {\"blocks\":[],\"flags\":[]}。"
)


def _chunk_to_json(chunk: Chunk) -> str:
    """把 chunk 序列化为喂给 LLM 的紧凑 JSON 字符串。"""
    payload = {
        "context_path": chunk.path,
        "blocks": [_serialize_block(b) for b in chunk.blocks],
    }
    return json.dumps(payload, ensure_ascii=False)


def organize_chunk(chunk: Chunk, llm_call: LLMCall) -> list[Block]:
    """用 LLM 整理单个 chunk，返回整理后的 blocks。

    Args:
        chunk: 待整理的 chunk
        llm_call: (system, user) -> dict；内部负责调 LLM 并解析成 JSON，失败应自行重试后抛异常

    Returns:
        整理后的 blocks；若 LLM 结果校验失败（重试后仍失败）则返回原始 blocks，
        并在 chunk._fallback_note 记录原因。
    """
    user = (
        f"请整理下面这个文档片段。标题路径：{_path_str(chunk.path)}\n"
        f"片段内容（JSON）：\n{_chunk_to_json(chunk)}"
    )
    max_attempts = 2  # 首次 + 一次重试
    for attempt in range(max_attempts):
        try:
            parsed = llm_call(_SYSTEM_PROMPT, user)
            errors = _validate_instructions(chunk, parsed)
            if errors:
                logger.warning(
                    "chunk %s 指令校验失败(尝试%d): %s", chunk.id, attempt + 1, errors
                )
                if attempt < max_attempts - 1:
                    user += f"\n\n上次回复校验失败：{errors}\n请修正后重试。"
                    continue
                chunk._fallback_note = f"指令校验失败: {'; '.join(errors)}"
                return chunk.blocks
            return _apply_instructions(chunk, parsed)
        except Exception as exc:  # noqa: BLE001 解析失败 / 网络失败
            logger.warning("chunk %s LLM 调用失败(尝试%d): %s", chunk.id, attempt + 1, exc)
            if attempt < max_attempts - 1:
                continue
            chunk._fallback_note = f"调用/解析失败: {exc}"
            return chunk.blocks

    return chunk.blocks


def organize_document(doc: Document, llm_call: LLMCall, *, max_blocks_per_chunk: int = 40) -> OrganizeResult:
    """整理整个 Document，返回 OrganizeResult（blocks + meta）。

    Args:
        doc: 规则提取后的 Document
        llm_call: (system, user) -> dict，见 organize_chunk
    """
    chunks = chunk_document(doc, max_blocks_per_chunk=max_blocks_per_chunk)
    if not chunks:
        return OrganizeResult(blocks=doc.blocks, meta=[])

    all_blocks: list[Block] = []
    meta: list[dict] = []
    chunks_unorganized = 0

    for chunk in chunks:
        organized = organize_chunk(chunk, llm_call)
        if chunk._fallback_note:
            chunks_unorganized += 1
            meta.append({
                "level": "warn",
                "chunk_id": chunk.id,
                "path": chunk.path,
                "message": chunk._fallback_note,
                "n_blocks": len(chunk.blocks),
            })
            all_blocks.extend(chunk.blocks)  # 保留原始块
        else:
            all_blocks.extend(organized)

    return OrganizeResult(
        blocks=all_blocks,
        meta=meta,
        chunks_unorganized=chunks_unorganized,
    )
