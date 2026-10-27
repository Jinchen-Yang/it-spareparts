"""纯函数逐格对比；身份来自令牌，不依赖 Excel 行号。"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json

from app.services.workbook.schema import json_value


def digest_plan(data: dict) -> str:
    body = json.dumps(json_value(data), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass
class Plan:
    schema_name: str
    export_id: str
    rows: list[dict]
    counts: dict[str, int]
    errors: list[dict]
    warnings: list[dict]
    schema_version: str = ""
    subject_type: str | None = None
    subject_id: int | None = None

    def to_dict(self) -> dict:
        return json_value({
            "schema_name": self.schema_name, "export_id": self.export_id,
            "schema_version": self.schema_version,
            "subject_type": self.subject_type, "subject_id": self.subject_id,
            "rows": self.rows, "counts": self.counts,
            "errors": self.errors, "warnings": self.warnings,
        })

    @property
    def digest(self) -> str:
        return digest_plan(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)


def _equal(left, right) -> bool:
    # JSON 持久化使用十进制文本；比较时仍保留金额的数值语义。
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except Exception:
            return False
    return json_value(left) == json_value(right)


def _issues(issues, row):
    return [item for item in issues if item.get("sheet") == row.sheet
            and item.get("row_number") == row.row_number]


def plan(parse_result, load_current) -> Plan:
    ids = defaultdict(list)
    for row in parse_result.rows:
        if row.kind != "added":
            ids[row.sheet].append(row.entity_id)
    current = {sheet: load_current(sheet, list(dict.fromkeys(keys))) for sheet, keys in ids.items()}
    rows = []
    for row in parse_result.rows:
        key = f"{row.sheet}:{hashlib.sha256(str(row.token).encode()).hexdigest()}" if row.token else f"{row.sheet}:new:{row.row_number}"
        errors = _issues(parse_result.errors, row)
        warnings = _issues(parse_result.warnings, row)
        values = {key: cell.value for key, cell in row.cells.items() if key in row.editable_keys}
        item = {
            "key": key, "sheet": row.sheet, "row_number": row.row_number,
            "entity_id": row.entity_id, "base_version": row.base_version,
            "status": "added", "changes": [], "values": values,
            "custom_fields": dict(row.custom_fields), "errors": errors, "warnings": warnings,
        }
        before = {}
        if row.kind != "added":
            found = current.get(row.sheet, {}).get(row.entity_id)
            if found is not None:
                before = found[1]
            if found is None or found[0] != row.base_version:
                item["status"] = "conflict"
                item["warnings"] = [*warnings, {
                    "sheet": row.sheet, "row_number": row.row_number, "column": None,
                    "message": "服务器记录已变更或不可访问，请重新导出后编辑",
                }]
            else:
                item["status"] = "deleted" if row.kind == "deleted" else "unchanged"
        if row.kind == "deleted":
            item["delete_key"] = key
            # 删除候选只携带导出可见的基线，避免回调额外返回的字段泄露。
            item["values"] = dict(row.baseline)
            item["changes"] = [
                {"key": column, "header": (row.cells[column].header or column) if column in row.cells else column,
                 "before": value, "after": None}
                for column, value in row.baseline.items() if value is not None
            ]
            item["changes"].extend(
                {"key": f"+{name}", "header": f"+{name}", "before": value, "after": None}
                for name, value in row.custom_fields.items() if value is not None
            )
        else:
            changes = []
            for column, value in values.items():
                old = before.get(column)
                if not _equal(old, value):
                    changes.append({"key": column, "header": row.cells[column].header or column,
                                    "before": old, "after": value})
            old_custom = before.get("custom_fields", {})
            for name, value in row.custom_fields.items():
                old = old_custom.get(name)
                if not _equal(old, value):
                    changes.append({"key": f"+{name}", "header": f"+{name}", "before": old, "after": value})
            item["changes"] = changes
            if changes and item["status"] == "unchanged":
                item["status"] = "modified"
        rows.append(item)
    counts = {key: 0 for key in ("added", "modified", "unchanged", "deleted", "conflict")}
    counts.update(Counter(row["status"] for row in rows))
    return Plan(parse_result.schema_name, parse_result.export_id, rows, counts,
                list(parse_result.errors), list(parse_result.warnings), str(parse_result.schema_version))
