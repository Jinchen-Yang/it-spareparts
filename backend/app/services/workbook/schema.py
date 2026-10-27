"""工作簿声明：导出、解析与页面共用一份列契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from app.services.excel_text_safety import has_illegal_excel_upload_text


class WorkbookError(ValueError):
    status_code = 422


@dataclass(frozen=True)
class WorkbookContext:
    field_groups: set[str] = field(default_factory=set)
    actor: str | None = None


@dataclass(frozen=True)
class ColumnSpec:
    key: str
    header: str
    type: Literal["text", "code", "int", "decimal", "date", "enum", "bool"]
    edit: Literal["locked", "required", "optional", "reference"]
    hint: str
    choices: dict[str, str] | list[str] | None = None
    width: float | None = None
    max_len: int | None = None
    decimals: int | None = None
    field_group: str | None = None

    def __post_init__(self):
        if not self.key or not self.header or self.header.startswith("+"):
            raise ValueError("列键和表头不能为空，普通表头不能以 + 开头")
        if self.header != self.header.strip() or any(c in self.header for c in "\u200b\u200c\u200d\u2060\ufeff\u00a0") or has_illegal_excel_upload_text(self.header):
            raise ValueError("表头不能包含首尾空白、不可见字符或控制字符")
        if self.key in {"_row_token", "entity_id", "base_version"} or self.header == "_row_token":
            raise ValueError("列名不能占用系统身份字段")
        if self.type not in {"text", "code", "int", "decimal", "date", "enum", "bool"}:
            raise ValueError("不支持的工作簿列类型")
        if self.edit not in {"locked", "required", "optional", "reference"}:
            raise ValueError("不支持的工作簿编辑规则")
        if self.decimals is not None and self.decimals < 0:
            raise ValueError("decimals 不能为负数")
        if self.max_len is not None and not 0 < self.max_len <= 32767:
            raise ValueError("max_len 必须在 1–32767 之间")
        if self.type == "enum" and not self.choices:
            raise ValueError("enum 必须声明 choices（代码到中文标签的映射或列表）")

    @property
    def editable(self) -> bool:
        return self.edit in {"required", "optional"}

    @property
    def mandatory(self) -> bool:
        return self.type == "code" and self.edit == "locked"


@dataclass(frozen=True)
class SheetSpec:
    key: str
    title: str
    columns: list[ColumnSpec]
    allow_insert: bool = False
    allow_delete: bool = False
    custom_columns: bool = False

    def __post_init__(self):
        if not self.key or not self.title or len(self.title) > 31 or any(c in self.title for c in r"\/*?:[]"):
            raise ValueError("工作表名称不合法")
        if self.title.casefold() in {"填写说明", "_meta"}:
            raise ValueError("工作表名称与系统工作表冲突")
        if len({c.key for c in self.columns}) != len(self.columns) or len({c.header for c in self.columns}) != len(self.columns):
            raise ValueError("同一工作表的列键、表头不能重复")


@dataclass(frozen=True)
class WorkbookSchema:
    name: str
    version: str | int
    sheets: list[SheetSpec]
    instructions: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.name or len(self.name) > 64 or not str(self.version) or not self.sheets:
            raise ValueError("工作簿必须声明名称、版本与工作表")
        if len({s.key for s in self.sheets}) != len(self.sheets) or len({s.title.casefold() for s in self.sheets}) != len(self.sheets):
            raise ValueError("工作表键和名称不能重复")

    def to_dict(self, *, ctx=None) -> dict:
        result = asdict(self)
        for sheet, spec in zip(result["sheets"], self.sheets):
            sheet["columns"] = [
                {**asdict(c), "mandatory": c.mandatory}
                for c in spec.columns if column_allowed(c, ctx)
            ]
        return result


def column_allowed(column: ColumnSpec, ctx) -> bool:
    if not column.field_group:
        return True
    if isinstance(ctx, WorkbookContext):
        return column.field_group in ctx.field_groups
    if ctx is None:
        return False
    from app import permissions
    perms = ctx.permissions
    if perms is None:
        perms = permissions.effective(ctx.role, None)
    return bool(perms.get(column.field_group, False))


def json_value(value: Any) -> Any:
    """金额保持十进制文本，日期使用 ISO；不经过浮点数中转。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(v) for v in value]
    return value
