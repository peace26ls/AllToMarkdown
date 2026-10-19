"""统一中间表示 (Unified Intermediate Representation)

所有提取器（docx / pdf / ocr / 多模态 LLM）输出同一种结构，
整理层与渲染层只认这一份契约。这是整个项目的骨架。

设计要点：
- Block 是所有内容的统一点，各 type 的自有数据放在 ``data: dict`` 中。
- ``atomic`` 原子块：表格 / 公式 / 列表（及代码块）这类一旦被截断就失去
  语义的内容，在 schema 层就标记为原子，后续分块（organize）逻辑必须遵守，
  而不是靠组织层自己的判断 —— 这是对交接文档 6.1 分块策略的显式强化。
- ``confidence`` 越低说明提取越不可靠，低于阈值进 meta.json 复核清单。
- ``source`` 记录来源页/锚点，供报错与定位。
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class BlockType(str, Enum):
    """块类型。所有提取器产出的最小内容单元。"""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    IMAGE = "image"
    CODE = "code"
    FORMULA = "formula"
    # 分隔符/横线等装饰性内容，渲染时可忽略
    RULE = "rule"


# 这些类型的块必须整块传输，绝不允许在分块（organize）时被拆开。
# 拆开意味着表格丢列、公式断行、列表顺序错乱。
ATOMIC_TYPES: frozenset[BlockType] = frozenset(
    {BlockType.TABLE, BlockType.FORMULA, BlockType.LIST, BlockType.CODE}
)


class Source(BaseModel):
    """块来源定位信息，用于报错与人工复核定位。"""

    # 页码（PDF，1-based）；docx 无页概念可留空
    page: int | None = None
    # 段落锚点或行号（docx 常用段落索引）
    para: int | None = None
    # 人类可读位置描述，例如 "第 2 页 表格 3" / "正文第 40 段"
    description: str | None = None


class Block(BaseModel):
    """统一内容块。

    data 按 type 约定取值：
      HEADING   -> {"level": int(1-6), "text": str}
      PARAGRAPH -> {"text": str}
      LIST      -> {"items": list[str], "ordered": bool, "depth": int}
      TABLE     -> {"rows": list[list[str]], "caption": str | None}
      IMAGE     -> {"path": str(相对 tasks/{id}/ 的路径), "alt": str | None,
                    "width": int | None, "height": int | None}
      CODE      -> {"text": str, "language": str | None}
      FORMULA   -> {"latex": str, "inline": bool}
      RULE      -> {}
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    type: BlockType
    data: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_review: bool = False
    atomic: bool = Field(default=False)
    source: Source = Field(default_factory=Source)

    @model_validator(mode="after")
    def _auto_atomic(self) -> "Block":
        """若 atomic 未显式赋值，按 type 自动推断是否为原子块。

        pydantic v2 的 field_validator(mode="before") 无法可靠读取并列字段
        type 的值（字段处理顺序不确定），因此改用 model_validator(after)，
        通过 model_fields_set 判断 atomic 是否被显式设置过。
        """
        if "atomic" not in self.model_fields_set and self.type in ATOMIC_TYPES:
            self.atomic = True
        return self


class Document(BaseModel):
    """一份输入文档提取后的完整中间表示。"""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    source_file: str
    mode: Literal["standard", "high_quality"] = "standard"
    detected_kind: Literal["docx", "pdf_text", "pdf_scanned", "unknown"] = "unknown"
    blocks: list[Block] = Field(default_factory=list)
    # 提取器能力元信息：用了哪些链路、有无降级，供 meta.json 与 UI 展示
    notes: list[str] = Field(default_factory=list)

    def review_items(self, threshold: float = 0.7) -> list[Block]:
        """返回低于置信度阈值或显式标记的复核块。"""
        return [
            b
            for b in self.blocks
            if b.needs_review or b.confidence < threshold
        ]

    def finalize(self, threshold: float = 0.7) -> None:
        """在渲染前统一回填 needs_review（依据阈值），避免各层重复判断。"""
        for b in self.blocks:
            if b.confidence < threshold:
                b.needs_review = True
