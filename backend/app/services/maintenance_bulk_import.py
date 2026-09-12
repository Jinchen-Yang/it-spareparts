"""Registry-driven, all-project maintenance business-form import core.

This is deliberately separate from the existing round-trip workbooks.  Those
workbooks are signed, project-scoped editing protocols; this module accepts raw
business-system exports and projects only unambiguous rows onto canonical
maintenance facts.

Protocol::

    xlsx -> detect adapter/header -> immutable preview plan + commit token
         -> lock/recheck -> one transaction -> canonical services + audit

Preview persists evidence in ``sys_import_batch.report_json`` but performs no
domain writes.  Apply is idempotent by batch and by ``(adapter, file_hash)``.
Adapters are registered objects, so adding another form does not change the
parser, token, persistence, pagination, or apply transaction machinery.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import secrets
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Protocol
from uuid import uuid4

from openpyxl import load_workbook
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app import config
from app.business_time import business_today
from app.etl import mapping, pipeline
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject, MaintenanceProjectContract
from app.models.maintenance_project_operations import (
    MaintenanceCollectionReceipt,
    MaintenanceCollectionSnapshot,
    MaintenanceProjectOperationAudit,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.sales import FSalesOrder
from app.models.system import SysAuditLog, SysImportBatch, SysImportError, SysRawFile
from app.services import maintenance_project_catalog as catalog
from app.services import maintenance_project_identity
from app.services import maintenance_project_operations as operations

PROTOCOL_VERSION = "maintenance-bulk-import-v1"
MAX_PREVIEW_BYTES = 16 * 1024 * 1024
MAX_PREVIEW_ROWS = 50_000
HEADER_SCAN_ROWS = 20
MONEY_LIMIT = Decimal("1000000000000")
SUPPORTED_BATCH_TYPES = ("maint_contract", "maint_receipt")
TRANSFER_BATCH_TYPE = "maint_bulk"
TRANSFER_SCHEMA_VERSION = "maintenance-project-batch-transfer-v1"
MAX_TRANSFER_FILES = 20
MAX_TRANSFER_TOTAL_BYTES = 64 * 1024 * 1024
TRANSFER_TOKEN_TTL = timedelta(minutes=30)


class BulkImportError(ValueError):
    """Base domain error."""


class BulkImportInvalid(BulkImportError):
    def __init__(self, message: str, *, issues: list[dict] | None = None):
        super().__init__(message)
        self.issues = issues or []


class BulkImportConflict(BulkImportError):
    """应用期冲突。``code`` 给 HTTP 层区分「预览后台账/快照已变化」等可重试形态。"""

    def __init__(self, message: str, *, code: str = "apply_conflict"):
        super().__init__(message)
        self.code = code


STALE_PREVIEW_MESSAGE = "预览后台账/快照已变化，请重新预览"
REAL_OPERATOR_MESSAGE = "经营事实写入必须使用实名系统账号"


class BulkImportNotFound(BulkImportError):
    pass


class BulkImportScopeDenied(BulkImportError):
    pass


@dataclass(frozen=True)
class DetectedSheet:
    name: str
    header_row: int
    header_rows: tuple[int, ...]
    headers: tuple[str, ...]
    system_headers: tuple[str, ...]
    field_indexes: dict[str, int]
    field_matches: dict[str, dict]
    rows: tuple[tuple[int, tuple[Any, ...]], ...]
    # 数据区被铺开的合并单元格数（0 = 文件本身没有合并区，读法与改动前完全一致）。
    merged_cells_expanded: int = 0


@dataclass(frozen=True)
class PreviewArtifact:
    adapter_key: str
    file_type: str
    file_hash: str
    filename: str
    plan: dict


class FormAdapter(Protocol):
    key: str
    file_type: str
    label: str
    aliases: dict[str, tuple[str, ...]]
    system_aliases: dict[str, tuple[str, ...]]
    required_fields: frozenset[str]

    def recognize(
        self,
        headers: tuple[str, ...],
        system_headers: tuple[str, ...] | None = None,
    ) -> tuple[int, dict[str, int], dict[str, dict]] | None:
        ...

    def build_plan(self, db: Session, sheet: DetectedSheet) -> dict:
        ...

    def apply_plan(
        self,
        db: Session,
        plan: dict,
        *,
        operated_by: str,
        audit_reason: str,
        provenance: dict | None = None,
    ) -> dict:
        """``provenance``：``{"batch_id": sys_import_batch.id, "source_sha256": 原件 sha256}``，
        让写出的事实行直接指向批次与原件，而不只靠审计 reason 文本。"""
        ...


_ADAPTERS: dict[str, FormAdapter] = {}


def register_adapter(adapter: FormAdapter) -> FormAdapter:
    if adapter.key in _ADAPTERS:
        raise RuntimeError(f"duplicate maintenance bulk adapter: {adapter.key}")
    if adapter.file_type not in SUPPORTED_BATCH_TYPES:
        raise RuntimeError(f"unsupported sys_import_batch namespace: {adapter.file_type}")
    _ADAPTERS[adapter.key] = adapter
    return adapter


def registered_forms() -> list[dict]:
    return [
        {
            "form_type": adapter.key,
            "label": adapter.label,
            "required_fields": sorted(adapter.required_fields),
            "accepted_headers": {
                key: list(values) for key, values in adapter.aliases.items()
            },
            "stable_source_fields": {
                key: list(values)
                for key, values in getattr(adapter, "system_aliases", {}).items()
            },
        }
        for adapter in _ADAPTERS.values()
    ]


def _header(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"\((?:必填|不可修改)\)$", "", text, flags=re.I)
    return text.casefold()


def _system_header(value: Any) -> str:
    """Normalize the machine field id from the first export header row.

    Tritium exports sometimes prefix a child-table id (``D...F0000013``).
    The final segment is stable inside that document type and remains usable
    when users reorder columns.  It is deliberately kept separate from the
    Chinese-caption namespace, preventing the historic cross-document alias
    collision on fields such as ``税率(必填)``.
    """

    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    return value.rsplit(".", 1)[-1].casefold()


def _text(value: Any) -> str:
    return unicodedata.normalize(
        "NFKC", "" if value is None else str(value)
    ).strip()


def normalize_order_no(value: Any) -> str:
    value = re.sub(r"\s+", "", _text(value)).upper()
    return value[5:] if value.startswith("XSDD-") else value


def _decimal(value: Any, *, label: str, allow_zero: bool = True) -> Decimal:
    raw = _text(value).replace(",", "").replace("￥", "").replace("¥", "")
    if raw == "":
        raise BulkImportInvalid(f"{label}为空")
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise BulkImportInvalid(f"{label}不是合法数字：{raw!r}") from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise BulkImportInvalid(f"{label}必须是非负有限数字")
    if parsed >= MONEY_LIMIT:
        raise BulkImportInvalid(f"{label}超出系统金额上限")
    return parsed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _optional_decimal(value: Any, *, label: str) -> Decimal | None:
    return None if _text(value) == "" else _decimal(value, label=label)


def _tax_rate(value: Any) -> Decimal | None:
    raw = _text(value).replace("％", "%")
    if not raw:
        return None
    pct = raw.endswith("%")
    if pct:
        raw = raw[:-1]
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise BulkImportInvalid(f"税率不是合法数字：{value!r}") from exc
    if pct or parsed > 1:
        parsed /= Decimal("100")
    if parsed < 0 or parsed > 1:
        raise BulkImportInvalid("税率必须在 0–100% 之间")
    return parsed.quantize(Decimal("0.000001"))


def _date(value: Any, *, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _text(value)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise BulkImportInvalid(f"{label}不是合法日期：{raw!r}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _token_hash(batch_id: int, token: str) -> str:
    return hashlib.sha256(f"{batch_id}:{token}".encode("utf-8")).hexdigest()


def _operation_hash(operated_by: str, operation_key: str) -> str:
    return hashlib.sha256(
        f"{operated_by}\0{operation_key.strip()}".encode("utf-8")
    ).hexdigest()


def _advisory_lock(db: Session, key: str) -> None:
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


class HeaderAdapter:
    aliases: dict[str, tuple[str, ...]]
    system_aliases: dict[str, tuple[str, ...]] = {}
    required_fields: frozenset[str]
    required_alternatives: tuple[frozenset[str], ...] = ()
    # 合并单元格铺满整块时**绝不能**铺的字段：逐行金额一旦被铺，一笔钱会被复制成
    # 多笔。现网导出从不合并这些列（见 _expand_merged_cells），真出现了就显式拒绝。
    merge_guarded_fields: frozenset[str] = frozenset()

    def recognize(
        self,
        headers: tuple[str, ...],
        system_headers: tuple[str, ...] | None = None,
    ) -> tuple[int, dict[str, int], dict[str, dict]] | None:
        normalized_headers = tuple(_header(value) for value in headers)
        normalized_system = tuple(
            _system_header(value) for value in (system_headers or ())
        )
        indexes: dict[str, int] = {}
        matches: dict[str, dict] = {}
        stable_hits = 0
        for field, aliases in self.aliases.items():
            wanted = {_header(alias) for alias in aliases}
            label_candidates = [
                idx for idx, header in enumerate(normalized_headers) if header in wanted
            ]
            system_wanted = {
                _system_header(alias)
                for alias in self.system_aliases.get(field, ())
            }
            system_candidates = [
                idx for idx, header in enumerate(normalized_system)
                if header and header in system_wanted
            ]

            # A two-row business-system export is accepted only when the
            # stable field id and its human caption agree at the same column.
            # This resolves duplicate captions without relying on column
            # letters and fails closed if the upstream schema repurposes an id.
            if len(system_candidates) == 1:
                idx = system_candidates[0]
                if idx < len(normalized_headers) and normalized_headers[idx] in wanted:
                    indexes[field] = idx
                    stable_hits += 1
                    matches[field] = {
                        "source_column": headers[idx],
                        "system_field": (system_headers or ())[idx],
                        "confidence": "exact",
                    }
                    continue
            # A duplicated caption without a stable id is ambiguous, never
            # silently resolved to the first occurrence.
            if len(label_candidates) == 1:
                idx = label_candidates[0]
                indexes[field] = idx
                matches[field] = {
                    "source_column": headers[idx],
                    "system_field": None,
                    "confidence": "alias",
                }

        alternatives = self.required_alternatives or (self.required_fields,)
        if not any(required.issubset(indexes) for required in alternatives):
            return None
        return stable_hits * 100 + len(indexes), indexes, matches


def _detect(data: bytes) -> tuple[FormAdapter, DetectedSheet]:
    try:
        # Some Tritium exports have malformed worksheet dimensions/cell order:
        # openpyxl's streaming reader then exposes only column A although the
        # workbook visibly contains fields through EX.  Upload size and row
        # limits are enforced before/while parsing, so use the normal reader to
        # obtain the real sparse cell map deterministically.
        workbook = load_workbook(
            io.BytesIO(data),
            read_only=False,
            data_only=True,
            keep_links=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise BulkImportInvalid(f"无法读取 .xlsx：{type(exc).__name__}") from exc

    candidates: list[
        tuple[
            int,
            str,
            int,
            FormAdapter,
            tuple[str, ...],
            tuple[str, ...],
            dict[str, int],
            dict[str, dict],
        ]
    ] = []
    for sheet in workbook.worksheets:
        if sheet.sheet_state != "visible" or sheet.title.lower().startswith("hidden_"):
            continue
        header_band = [
            tuple(_text(value) for value in values)
            for values in sheet.iter_rows(
                min_row=1,
                max_row=min(HEADER_SCAN_ROWS, sheet.max_row),
                values_only=True,
            )
        ]
        for row_no, headers in enumerate(header_band, start=1):
            system_headers = header_band[row_no - 2] if row_no > 1 else ()
            for adapter in _ADAPTERS.values():
                match = adapter.recognize(headers, system_headers or None)
                if match is not None:
                    score, indexes, matches = match
                    candidates.append(
                        (
                            score,
                            sheet.title,
                            row_no,
                            adapter,
                            headers,
                            system_headers,
                            indexes,
                            matches,
                        )
                    )
    if not candidates:
        raise BulkImportInvalid(
            "无法识别表单：未找到销售合同额或收款单所需表头",
            issues=[{"code": "unknown_form", "registered_forms": list(_ADAPTERS)}],
        )
    candidates.sort(key=lambda item: (-item[0], item[1], item[2], item[3].key))
    best_score = candidates[0][0]
    best = [item for item in candidates if item[0] == best_score]
    identities = {(item[1], item[2], item[3].key) for item in best}
    if len(identities) != 1:
        raise BulkImportInvalid(
            "工作簿中存在多个同等匹配的业务表，无法自动选择",
            issues=[
                {"sheet": s, "header_row": r, "form_type": a.key}
                for _score, s, r, a, _h, _sh, _i, _m in best[:20]
            ],
        )
    (
        _score,
        sheet_name,
        header_row,
        adapter,
        headers,
        system_headers,
        indexes,
        matches,
    ) = best[0]
    sheet = workbook[sheet_name]
    merged_fill = _expand_merged_cells(
        sheet, header_row=header_row, adapter=adapter, indexes=indexes
    )
    rows: list[tuple[int, tuple[Any, ...]]] = []
    for row_no, values in enumerate(
        sheet.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1
    ):
        values = tuple(values)
        # 空行判定看**原始**单元格：先铺再判会把合并区盖住的空行当成数据行，凭空造钱。
        if all(_text(value) == "" for value in values):
            continue
        if merged_fill:
            values = tuple(
                merged_fill.get((row_no, index), value)
                for index, value in enumerate(values)
            )
        rows.append((row_no, values))
        if len(rows) > MAX_PREVIEW_ROWS:
            raise BulkImportInvalid(f"数据行超过安全上限 {MAX_PREVIEW_ROWS}")
    if not rows:
        raise BulkImportInvalid("识别到表头，但没有数据行")
    return adapter, DetectedSheet(
        name=sheet_name,
        header_row=header_row,
        header_rows=((header_row - 1, header_row) if system_headers else (header_row,)),
        headers=headers,
        system_headers=system_headers,
        field_indexes=indexes,
        field_matches=matches,
        rows=tuple(rows),
        merged_cells_expanded=len(merged_fill),
    )


def _expand_merged_cells(
    sheet: Any,
    *,
    header_row: int,
    adapter: FormAdapter,
    indexes: dict[str, int],
) -> dict[tuple[int, int], Any]:
    """把数据区的纵向合并区铺满整块，返回 {(行号, 0基列号): 值}。

    氚云「收款单主表 + 明细展开」导出（2026-09-08 生产实拍）把 收款单号 / 收款日期 /
    数据状态 等 38 个主表列做成跨整张单的合并区，只有块首那格有值。openpyxl 读非
    左上角单元格一律 None，于是同一张单的第 2..n 条明细全被判成「收款单号为空」——
    实拍 298 行文件 232 行落空，149.9 万只认了 37.8 万。合并区是 xlsx 自身对
    「这几行共用这个值」的表达，铺开就是按它的语义读，不是猜。

    三份生产导出实测：合并区全是单列纵向、不跨表头、从不涉及「收款明细.*」子表列
    （4 行文件 39 个 / 298 行文件 1287 个 / 8-28 子表导出 0 个）。没有合并区的文件
    这里返回空字典，读法与改动前逐字节一致。

    金额列被合并则拒绝（``merge_guarded_fields``）：铺开会把一笔钱复制成多笔，
    这是钱，不猜。
    """

    guarded = {
        index: field
        for field, index in indexes.items()
        if field in getattr(adapter, "merge_guarded_fields", frozenset())
    }
    fill: dict[tuple[int, int], Any] = {}
    for cell_range in sheet.merged_cells.ranges:
        if cell_range.max_row <= header_row:
            continue  # 表头区的合并不动，识别逻辑另有其事
        if cell_range.max_row == cell_range.min_row and cell_range.max_col == cell_range.min_col:
            continue
        for column in range(cell_range.min_col, cell_range.max_col + 1):
            field = guarded.get(column - 1)
            if field is None:
                continue
            raise BulkImportInvalid(
                f"第 {cell_range.min_row}-{cell_range.max_row} 行的"
                f"「{_text(sheet.cell(header_row, column).value)}」是合并单元格；"
                "逐行金额列不接受合并（铺开会把一笔钱算成多笔），请取消合并后重新导出",
                issues=[{
                    "code": "merged_amount_column",
                    "cell_range": str(cell_range),
                    "canonical_field": field,
                }],
            )
        anchor = sheet.cell(cell_range.min_row, cell_range.min_col).value
        if anchor is None:
            continue
        for row_no in range(cell_range.min_row, cell_range.max_row + 1):
            for column in range(cell_range.min_col, cell_range.max_col + 1):
                if row_no == cell_range.min_row and column == cell_range.min_col:
                    continue
                fill[(row_no, column - 1)] = anchor
    return fill


def _value(sheet: DetectedSheet, values: tuple[Any, ...], field: str) -> Any:
    idx = sheet.field_indexes.get(field)
    return values[idx] if idx is not None and idx < len(values) else None


def _contract_maps(db: Session) -> tuple[
    dict[str, list[MaintenanceProjectContract]],
    dict[str, MaintenanceProjectContract],
]:
    today = business_today()
    rows = list(db.scalars(
        select(MaintenanceProjectContract)
        .join(MaintenanceProject, MaintenanceProject.project_id == MaintenanceProjectContract.project_id)
        .where(
            MaintenanceProject.is_active.is_(True),
            MaintenanceProjectContract.effective_from <= today,
            or_(
                MaintenanceProjectContract.effective_to.is_(None),
                MaintenanceProjectContract.effective_to > today,
            ),
        )
        .order_by(
            MaintenanceProjectContract.contract_no,
            MaintenanceProjectContract.project_id,
            MaintenanceProjectContract.project_contract_id,
        )
    ))
    all_current: dict[str, list[MaintenanceProjectContract]] = defaultdict(list)
    for row in rows:
        all_current[normalize_order_no(row.contract_no)].append(row)
    safe = {
        key: matches[0]
        for key, matches in all_current.items()
        if len(matches) == 1
        and matches[0].status_mapping_state == "mapped"
        and matches[0].included_in_total
    }
    return dict(all_current), safe


def _row_issue(row_no: int, code: str, message: str, *, severity: str = "error") -> dict:
    return {"row_no": row_no, "code": code, "message": message, "severity": severity}


def _all_contracts_by_order(
    db: Session,
    order_variants: set[str],
) -> dict[str, list[MaintenanceProjectContract]]:
    if not order_variants:
        return {}
    rows = list(
        db.scalars(
            select(MaintenanceProjectContract)
            .where(MaintenanceProjectContract.contract_no.in_(sorted(order_variants)))
            .order_by(
                MaintenanceProjectContract.contract_no,
                MaintenanceProjectContract.effective_from,
                MaintenanceProjectContract.project_contract_id,
            )
        )
    )
    result: dict[str, list[MaintenanceProjectContract]] = defaultdict(list)
    for row in rows:
        result[normalize_order_no(row.contract_no)].append(row)
    return dict(result)


def _assignment_evidence(
    db: Session,
    order_variants: set[str],
) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """Return current-safe and historical XSDD ownership evidence.

    Current ownership is intentionally stricter than mere assignment presence:
    the WBDD, assignment and project must all still be active.  Historical rows
    remain visible to the pre-delivery auto-create guard so an old ownership
    decision can never be erased by creating a fresh project with the same
    sales order.
    """

    if not order_variants:
        return {}, {}
    rows = db.execute(
        select(
            FMaintenanceOrder.linked_sales_order_no,
            FMaintenanceOrder.raw_order_id,
            FMaintenanceOrder.data_status,
            MaintenanceSourceOrderAssignment.assignment_id,
            MaintenanceSourceOrderAssignment.project_id,
            MaintenanceSourceOrderAssignment.is_active,
            MaintenanceSourceOrderAssignment.version,
            MaintenanceProject.is_active,
            MaintenanceProject.version,
            MaintenanceProject.display_name,
        )
        .select_from(FMaintenanceOrder)
        .join(
            MaintenanceSourceOrderAssignment,
            MaintenanceSourceOrderAssignment.source_order_id
            == FMaintenanceOrder.raw_order_id,
        )
        .join(
            MaintenanceProject,
            MaintenanceProject.project_id
            == MaintenanceSourceOrderAssignment.project_id,
        )
        .where(FMaintenanceOrder.linked_sales_order_no.in_(sorted(order_variants)))
        .order_by(
            FMaintenanceOrder.linked_sales_order_no,
            MaintenanceSourceOrderAssignment.assignment_id,
        )
    ).all()
    current: dict[str, list[dict]] = defaultdict(list)
    historical: dict[str, list[dict]] = defaultdict(list)
    for (
        order_no,
        source_order_id,
        source_status,
        assignment_id,
        project_id,
        assignment_active,
        assignment_version,
        project_active,
        project_version,
        project_name,
    ) in rows:
        norm = normalize_order_no(order_no)
        item = {
            "source_order_id": source_order_id,
            "source_status": source_status,
            "assignment_id": assignment_id,
            "assignment_version": assignment_version,
            "project_id": project_id,
            "project_version": project_version,
            "project_name": project_name,
            "assignment_active": bool(assignment_active),
            "project_active": bool(project_active),
        }
        historical[norm].append(item)
        if (
            assignment_active
            and project_active
            and source_status == config.ACTIVE_STATUS
        ):
            current[norm].append(item)
    return dict(current), dict(historical)


def _assignment_fingerprint(rows: list[dict]) -> list[dict]:
    return sorted(
        [
            {
                "source_order_id": row["source_order_id"],
                "assignment_id": row["assignment_id"],
                "assignment_version": row["assignment_version"],
                "project_id": row["project_id"],
                "project_version": row["project_version"],
            }
            for row in rows
        ],
        key=lambda row: (row["project_id"], row["assignment_id"]),
    )


def _sales_fingerprint(rows: list[FSalesOrder]) -> list[dict]:
    return sorted(
        [
            {
                "id": row.id,
                "raw_order_id": row.raw_order_id,
                "order_no": row.order_no,
                "data_status": row.data_status,
                "amount_ex_tax": _jsonable(row.amount_ex_tax),
                "tax_rate": _jsonable(row.tax_rate),
            }
            for row in {row.id: row for row in rows}.values()
        ],
        key=lambda row: row["id"],
    )


def _is_yes(value: Any) -> bool:
    return _text(value).casefold() in {"是", "含税", "true", "yes", "1", "y"}


def _is_maintenance_business_type(value: Any) -> bool:
    business_type = _text(value)
    return any(word in business_type for word in ("维保", "运维", "维修"))


def _is_explicit_maintenance_row(
    sheet: DetectedSheet,
    values: tuple[Any, ...],
) -> bool:
    """只认源表显式的「维保业务=是」（2026-09-03 负责人拍板）。

    此前是「维保业务=是 或 业务类型含 维保/运维/维修」的或逻辑，会让明确标了
    「维保业务=否」的单次维修（业务类型里正好带「维修」）也被自动建项——与源头
    上人给出的否定标记直接矛盾。业务类型是分类，维保业务才是「这一行是不是维保
    业务」的权威事实；只信后者，列缺失或留空一律不建项（fail-closed）。
    """

    return _is_yes(_value(sheet, values, "maintenance_business"))


def _explicit_maintenance_period(
    sheet: DetectedSheet,
    values: tuple[Any, ...],
) -> tuple[date | None, date | None]:
    raw_from = _value(sheet, values, "period_from")
    raw_to = _value(sheet, values, "period_to")
    period_from = _date(raw_from, label="维保起始日期") if _text(raw_from) else None
    period_to = _date(raw_to, label="维保终止日期") if _text(raw_to) else None
    if period_from is not None and period_to is not None and period_to < period_from:
        raise BulkImportInvalid("维保终止日期不能早于起始日期")
    return period_from, period_to


_MANAGER_SEPARATORS = re.compile(r"[;；,，、/／|｜\s]+")


def _split_project_managers(raw: Any) -> tuple[str | None, str | None]:
    """销售订单「项目经理(必填)」是多值（实测「廖晓娟;司珂梓」，且不同行顺序相反）。

    2026-09-03 拍板：建项时负责人取第一个，完整原值保留（写进建项审计），
    不丢信息也不猜主责。返回 (首位, 原值归一)。
    """
    text = _text(raw)
    if not text:
        return None, None
    parts = [part for part in _MANAGER_SEPARATORS.split(text) if part]
    if not parts:
        return None, None
    return parts[0][:64], "、".join(parts)[:256]


def _maintenance_project_metadata(
    sheet: DetectedSheet,
    values: tuple[Any, ...],
    *,
    row_no: int,
    norm: str,
) -> dict:
    raw_name = _text(_value(sheet, values, "project_name"))
    if not raw_name:
        raise BulkImportInvalid("维保销售订单自动建项必须提供项目名称")
    if not _is_explicit_maintenance_row(sheet, values):
        raise BulkImportInvalid("自动建项只接受销售订单中明确的维保业务事实")
    # Preserve the exact sales-order name.  A pre-delivery name and a later
    # formal name for the same XSDD are peer display facts, not a hierarchy.
    display_name = raw_name.strip()

    direct_from, direct_to = _explicit_maintenance_period(sheet, values)
    period_from = direct_from
    period_to = direct_to
    # 期限允许缺失或单侧：D-05「销售订单上没有期限的，留给人工在项目信息编辑里填」。
    # 已知值照实保存、缺侧保持 NULL；双侧倒置仍整批拒绝。单侧期限会按 2026-09-03
    # 拍板口径显示为「期限缺失」，正好把待补项目筛出来提醒人工补齐。
    if period_from is not None and period_to is not None and period_to < period_from:
        raise BulkImportInvalid("维保终止日期不能早于起始日期")
    business_type = _text(_value(sheet, values, "business_type"))
    manager_primary, manager_raw = _split_project_managers(
        _value(sheet, values, "project_manager"))
    return {
        "row_no": row_no,
        "project_code": f"XSDD-{norm}"[:64],
        "display_name": display_name[:256],
        "period_from": period_from.isoformat() if period_from else None,
        "period_to": period_to.isoformat() if period_to else None,
        "business_type": business_type or None,
        "project_manager": manager_primary,
        "project_manager_raw": manager_raw,
    }


def _apply_sales_project_period(
    db: Session,
    *,
    item: dict,
    project_id: str,
    audit_reason: str,
    operated_by: str,
) -> None:
    raw_from = item.get("source_period_from")
    raw_to = item.get("source_period_to")
    from_present = bool(item.get("source_period_from_present"))
    to_present = bool(item.get("source_period_to_present"))
    if not from_present and not to_present:
        return
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        raise BulkImportConflict("项目在同步维保期限时消失")
    desired_from = (
        date.fromisoformat(raw_from)
        if raw_from is not None
        else (None if from_present else project.period_from)
    )
    desired_to = (
        date.fromisoformat(raw_to)
        if raw_to is not None
        else (None if to_present else project.period_to)
    )
    if project.period_from == desired_from and project.period_to == desired_to:
        return
    if project.version != item.get("expected_project_version"):
        raise BulkImportConflict("预览后的项目维保期限已变化")
    updates = {"period_from": desired_from, "period_to": desired_to}
    try:
        updated = catalog.update_project(
            db,
            project_id=project_id,
            version=project.version,
            updates=updates,
            reason=f"销售订单权威维保期限同步：{audit_reason}",
            operated_by=operated_by,
        )
    except (catalog.MaintenanceProjectCatalogError, catalog.MaintenanceProjectCatalogConflict) as exc:
        raise BulkImportConflict(str(exc)) from exc
    if updated is None:
        raise BulkImportConflict("项目在同步维保期限时消失")


class SalesContractAmountAdapter(HeaderAdapter):
    key = "sales_contract_amount"
    file_type = "maint_contract"
    label = "销售订单合同含税额"
    aliases = {
        "order_no": ("订单编号(必填)", "订单编号", "销售订单", "销售单号", "合同编号"),
        "raw_order_id": ("数据ID(不可修改)", "订单数据ID", "销售订单数据ID"),
        "amount_inc_tax": ("含税金额", "合同总额(含税)", "合同总额（含税）"),
        "order_amount": ("订单金额", "合同金额", "合同总额"),
        "tax_flag": ("是否含税(必填)", "是否含税", "含税标记"),
        "tax_rate": ("税率(必填)", "税率"),
        "tax_amount": ("税金", "税额"),
        "amount_ex_tax": ("不含税金额", "未税金额", "合同金额(未税)"),
        "data_status": ("数据状态", "订单状态"),
        "maintenance_business": ("维保业务", "是否维保业务"),
        "business_type": ("业务类型#", "业务类型"),
        "project_name": ("项目名称(必填)", "项目名称"),
        "project_manager": ("项目经理(必填)", "项目经理"),
        "period_from": ("维保起始日期(必填)", "维保起始日期", "维保起始时间"),
        "period_to": ("维保终止日期(必填)", "维保终止日期", "维保终止时间"),
    }
    # First-row ids from the sales-order export.  Matching is by id+caption at
    # the same column; these are not physical Excel positions.
    system_aliases = {
        "order_no": ("SeqNo",),
        "raw_order_id": ("ObjectId",),
        "order_amount": ("F0000021",),
        "tax_flag": ("F0000053",),
        "tax_rate": ("F0000054",),
        "tax_amount": ("F0000055",),
        "amount_ex_tax": ("F0000056",),
        "data_status": ("Status",),
        "maintenance_business": ("F0000118",),
        # The source contains both F0000060/业务类型 and
        # F0000059/业务类型#; the latter is the project-facing field.
        "business_type": ("F0000059",),
        "project_name": ("F0000119",),
        "project_manager": ("F0000134",),
        "period_from": ("F0000131",),
        "period_to": ("F0000132",),
    }
    required_fields = frozenset(
        {
            "order_no",
            "order_amount",
            "tax_flag",
            "tax_rate",
            "tax_amount",
            "amount_ex_tax",
        }
    )
    required_alternatives = (
        required_fields,
        frozenset({"order_no", "amount_inc_tax", "tax_rate", "amount_ex_tax"}),
        frozenset({"order_no", "amount_ex_tax", "tax_amount"}),
        frozenset({"order_no", "amount_inc_tax", "amount_ex_tax"}),
        frozenset({"order_no", "order_amount", "tax_flag", "tax_rate"}),
        frozenset(
            {
                "order_no",
                "order_amount",
                "tax_flag",
                "amount_ex_tax",
                "tax_amount",
            }
        ),
    )

    @staticmethod
    def _amounts(
        sheet: DetectedSheet,
        values: tuple[Any, ...],
    ) -> tuple[Decimal, Decimal, Decimal]:
        explicit_inc = _optional_decimal(
            _value(sheet, values, "amount_inc_tax"), label="含税金额"
        )
        order_amount = _optional_decimal(
            _value(sheet, values, "order_amount"), label="订单金额"
        )
        amount_ex = _optional_decimal(
            _value(sheet, values, "amount_ex_tax"), label="不含税金额"
        )
        tax_amount = _optional_decimal(
            _value(sheet, values, "tax_amount"), label="税金"
        )
        rate = _tax_rate(_value(sheet, values, "tax_rate"))
        flag = _text(_value(sheet, values, "tax_flag"))

        # First identify an authoritative gross value.  DK is gross only when
        # DL explicitly says so; DO+DN is an independent gross proof.
        inc: Decimal | None = None
        if explicit_inc is not None:
            inc = explicit_inc
        elif order_amount is not None and flag in {"含税", "是", "含税价"}:
            # Official sales export: DK/订单金额 is the authoritative gross
            # amount when DL says 含税.  DO is the ex-tax fact and DN is tax.
            inc = order_amount
        if order_amount is not None and flag in {"不含税", "否", "未税"}:
            amount_ex = amount_ex or order_amount

        # DO+DN can prove both gross and, when DO>0, the rate even if DM is
        # absent.  That is evidence-derived 0% when DN is actually zero, not a
        # NULL-to-zero fallback.  If a gross value was independently supplied,
        # it must agree with this sum.
        if amount_ex is not None and tax_amount is not None:
            sum_inc = (amount_ex + tax_amount).quantize(Decimal("0.01"))
            if inc is not None and abs(inc - sum_inc) > Decimal("0.02"):
                raise BulkImportInvalid("订单金额与不含税金额+税金不一致")
            inc = inc or sum_inc
            if rate is None:
                if amount_ex <= 0:
                    raise BulkImportInvalid("不含税金额为 0 时无法从税金反推税率")
                derived_rate = tax_amount / amount_ex
                if derived_rate < 0 or derived_rate > 1:
                    raise BulkImportInvalid("由税金反推的税率不在 0–100% 之间")
                rate = derived_rate.quantize(Decimal("0.000001"))

        # A known gross plus ex-tax amount can also prove the omitted tax and
        # rate.  A lone ex-tax/order amount still requires explicit rate.
        if rate is None and inc is not None and amount_ex is not None:
            derived_tax = inc - amount_ex
            if amount_ex <= 0 or derived_tax < 0:
                raise BulkImportInvalid("税率为空，且含税/未税金额无法反推税率")
            derived_rate = derived_tax / amount_ex
            if derived_rate < 0 or derived_rate > 1:
                raise BulkImportInvalid("由含税/未税金额反推的税率不在 0–100% 之间")
            rate = derived_rate.quantize(Decimal("0.000001"))

        if inc is None and amount_ex is not None:
            if rate is None:
                raise BulkImportInvalid("税率为空，不能从单一未税金额推导含税合同额")
            inc = (amount_ex * (Decimal("1") + rate)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        if inc is None:
            raise BulkImportInvalid("无法从订单金额/含税标记/税金推导含税合同额")

        if amount_ex is None:
            if tax_amount is not None:
                amount_ex = (inc - tax_amount).quantize(Decimal("0.01"))
            elif rate is not None:
                amount_ex = (inc / (Decimal("1") + rate)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            else:
                raise BulkImportInvalid("税率为空，且没有税金/未税金额可供交叉校验")
        if amount_ex < 0:
            raise BulkImportInvalid("不含税金额不能为负")
        if rate is None:
            raise BulkImportInvalid("税率缺失且无法从完整金额证据反推")
        expected_tax = (amount_ex * rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if tax_amount is not None and abs(expected_tax - tax_amount) > Decimal("0.02"):
            raise BulkImportInvalid("税金与不含税金额×税率不一致")
        if tax_amount is not None and abs((amount_ex + tax_amount) - inc) > Decimal("0.02"):
            raise BulkImportInvalid("订单金额与不含税金额+税金不一致")
        return inc, amount_ex, rate

    def build_plan(self, db: Session, sheet: DetectedSheet) -> dict:
        all_contracts, safe_contracts = _contract_maps(db)
        parsed: list[dict] = []
        source_rows: list[dict] = []
        hard_issues: list[dict] = []
        seen: dict[str, tuple[Any, ...]] = {}

        raw_ids: set[str] = set()
        order_variants: set[str] = set()
        for row_no, values in sheet.rows:
            raw_order_id = _text(_value(sheet, values, "raw_order_id"))
            order_no_raw = _text(_value(sheet, values, "order_no"))
            norm = normalize_order_no(order_no_raw)
            if raw_order_id:
                raw_ids.add(raw_order_id)
            if norm:
                order_variants.update({order_no_raw, norm, f"XSDD-{norm}"})

        historical_contracts = _all_contracts_by_order(db, order_variants)
        active_assignments, assignment_history = _assignment_evidence(
            db, order_variants
        )

        sales_rows = list(db.scalars(select(FSalesOrder).where(or_(
            FSalesOrder.raw_order_id.in_(sorted(raw_ids or {""})),
            FSalesOrder.order_no.in_(sorted(order_variants or {""})),
        ))))
        sales_by_raw = {row.raw_order_id: row for row in sales_rows}
        sales_by_order: dict[str, list[FSalesOrder]] = defaultdict(list)
        for row in sales_rows:
            sales_by_order[normalize_order_no(row.order_no)].append(row)

        all_projects = list(
            db.scalars(
                select(MaintenanceProject).order_by(MaintenanceProject.project_id)
            )
        )
        projects_by_id = {project.project_id: project for project in all_projects}
        projects_by_code: dict[str, list[MaintenanceProject]] = defaultdict(list)
        for project in all_projects:
            projects_by_code[project.project_code.casefold()].append(project)

        for row_no, values in sheet.rows:
            order_no_raw = _text(_value(sheet, values, "order_no"))
            norm = normalize_order_no(order_no_raw)
            raw_order_id = _text(_value(sheet, values, "raw_order_id"))
            base = {
                "row_no": row_no,
                "business_key": order_no_raw,
                "normalized_order_no": norm,
                "issues": [],
            }
            if not norm:
                issue = _row_issue(row_no, "missing_order_no", "销售订单号为空")
                base.update(action="error", issues=[issue])
                source_rows.append(base)
                hard_issues.append(issue)
                continue

            source_status = _text(_value(sheet, values, "data_status"))
            if "data_status" in sheet.field_indexes and not source_status:
                issue = _row_issue(row_no, "missing_source_status", "销售订单数据状态为空")
                base.update(action="error", issues=[issue])
                source_rows.append(base)
                hard_issues.append(issue)
                continue
            if source_status and source_status != config.ACTIVE_STATUS:
                base.update(
                    action="skip",
                    issues=[
                        _row_issue(
                            row_no,
                            "inactive_sales_order",
                            f"销售订单状态为“{source_status}”，不覆盖项目合同额",
                            severity="warning",
                        )
                    ],
                )
                source_rows.append(base)
                continue
            try:
                inc, amount_ex, rate = self._amounts(sheet, values)
            except BulkImportInvalid as exc:
                issue = _row_issue(row_no, "invalid_amount", str(exc))
                base.update(action="error", issues=[issue])
                source_rows.append(base)
                hard_issues.append(issue)
                continue

            project_name_raw = _text(_value(sheet, values, "project_name"))
            period_from_raw = _text(_value(sheet, values, "period_from"))
            period_to_raw = _text(_value(sheet, values, "period_to"))
            source_period_from: date | None = None
            source_period_to: date | None = None
            inverted_period_warning: dict | None = None
            try:
                explicit_maintenance = _is_explicit_maintenance_row(sheet, values)
            except BulkImportInvalid as exc:
                issue = _row_issue(
                    row_no, "maintenance_source_conflict", str(exc)
                )
                base.update(action="error", issues=[issue])
                source_rows.append(base)
                hard_issues.append(issue)
                continue
            if explicit_maintenance:
                if not project_name_raw:
                    issue = _row_issue(
                        row_no,
                        "invalid_maintenance_project",
                        "维保销售订单自动建项必须提供项目名称",
                    )
                    base.update(action="error", issues=[issue])
                    source_rows.append(base)
                    hard_issues.append(issue)
                    continue
                try:
                    source_period_from, source_period_to = _explicit_maintenance_period(
                        sheet, values
                    )
                except BulkImportInvalid as exc:
                    if str(exc) == "维保终止日期不能早于起始日期":
                        inverted_period_warning = _row_issue(
                            row_no,
                            "inverted_maintenance_period_preserved",
                            "销售源维保期限倒置；已有项目将保留原期限，"
                            "本次仍同步 XSDD 身份与合同金额",
                            severity="warning",
                        )
                    else:
                        issue = _row_issue(
                            row_no, "invalid_maintenance_period", str(exc)
                        )
                        base.update(action="error", issues=[issue])
                        source_rows.append(base)
                        hard_issues.append(issue)
                        continue
            duplicate = seen.get(norm)
            signature = (
                inc,
                amount_ex,
                rate,
                source_status,
                project_name_raw,
                period_from_raw,
                period_to_raw,
            )
            if duplicate is not None:
                if duplicate[:-1] != signature:
                    issue = _row_issue(
                        row_no,
                        "duplicate_conflict",
                        f"同一销售订单与第 {duplicate[-1]} 行的金额或项目元数据不一致",
                    )
                    base.update(action="error", issues=[issue])
                    hard_issues.append(issue)
                else:
                    base.update(
                        action="duplicate",
                        issues=[_row_issue(
                            row_no,
                            "duplicate_same",
                            f"与第 {duplicate[-1]} 行相同，应用时只处理一次",
                            severity="warning",
                        )],
                    )
                source_rows.append(base)
                continue
            seen[norm] = (*signature, row_no)

            sales = sales_by_raw.get(raw_order_id) if raw_order_id else None
            if sales is not None and normalize_order_no(sales.order_no) != norm:
                issue = _row_issue(row_no, "sales_identity_mismatch", "数据ID与销售订单号不一致")
                base.update(action="error", issues=[issue])
                source_rows.append(base)
                hard_issues.append(issue)
                continue
            if sales is None:
                matches = sales_by_order.get(norm, [])
                if len(matches) == 1:
                    sales = matches[0]
                else:
                    base["issues"].append(_row_issue(
                        row_no,
                        "sales_fact_ambiguous" if matches else "sales_fact_missing",
                        (
                            "系统销售事实存在多个候选；合同仍可按源文件处理，"
                            "但本次不反写 f_sales_order"
                            if matches
                            else (
                                "系统尚无该销售事实；合同仍可按源文件处理，"
                                "且不会制造缺少明细行的 f_sales_order 头"
                            )
                        ),
                        severity="warning",
                    ))
            sync_sales = bool(
                sales is not None and sales.data_status == config.ACTIVE_STATUS
            )
            if sales is not None and not sync_sales:
                base["issues"].append(_row_issue(
                    row_no,
                    "sales_fact_inactive",
                    "系统销售事实不是已生效状态；合同仍按源文件处理，但不反写该事实",
                    severity="warning",
                ))

            sales_amount_before = sales.amount_ex_tax if sales is not None else None
            sales_rate_before = sales.tax_rate if sales is not None else None
            contract_no = sales.order_no if sales is not None else order_no_raw
            contract_id = (
                raw_order_id
                or (sales.raw_order_id if sales is not None else "")
                or f"XSDD-{norm}"
            )
            sales_candidates = list(sales_by_order.get(norm, []))
            if raw_order_id and sales_by_raw.get(raw_order_id) is not None:
                sales_candidates.append(sales_by_raw[raw_order_id])

            operation = {
                "row_no": row_no,
                "normalized_order_no": norm,
                "source_project_name": project_name_raw[:256] or None,
                "source_period_from": (
                    source_period_from.isoformat() if source_period_from else None
                ),
                "source_period_to": (
                    source_period_to.isoformat() if source_period_to else None
                ),
                "source_period_from_present": bool(
                    explicit_maintenance
                    and inverted_period_warning is None
                    and "period_from" in sheet.field_indexes
                ),
                "source_period_to_present": bool(
                    explicit_maintenance
                    and inverted_period_warning is None
                    and "period_to" in sheet.field_indexes
                ),
                "sales_order_id": sales.id if sync_sales else None,
                "sales_raw_order_id": sales.raw_order_id if sync_sales else None,
                "sales_match_state": (
                    "unique_active" if sync_sales else (
                        "unique_inactive" if sales is not None else (
                            "ambiguous" if sales_by_order.get(norm) else "missing"
                        )
                    )
                ),
                "expected_sales_candidates": _sales_fingerprint(
                    sales_candidates
                ),
                "contract_id": contract_id,
                "sales_order_no": contract_no,
                "contract_status": source_status or config.ACTIVE_STATUS,
                "expected_sales_amount_ex_tax": _jsonable(sales_amount_before),
                "expected_sales_tax_rate": _jsonable(sales_rate_before),
                "expected_sales_data_status": (
                    sales.data_status if sales is not None else None
                ),
                "new_contract_amount_inc_tax": _jsonable(inc),
                "new_sales_amount_ex_tax": _jsonable(amount_ex),
                "new_sales_tax_rate": _jsonable(rate),
                "contract_effective_from": min(
                    (
                        sales.order_date
                        if sales is not None and sales.order_date is not None
                        else business_today()
                    ),
                    business_today(),
                ).isoformat(),
            }
            current_relations = all_contracts.get(norm, [])
            contract = safe_contracts.get(norm)
            if contract is not None:
                before = {
                    "contract_amount_inc_tax": contract.amount_inc_tax,
                    "contract_version": contract.version,
                    "sales_amount_ex_tax": sales_amount_before,
                    "sales_tax_rate": sales_rate_before,
                }
                after = {
                    "contract_amount_inc_tax": inc,
                    "sales_amount_ex_tax": amount_ex,
                    "sales_tax_rate": rate,
                }
                action = "noop" if (
                    contract.amount_inc_tax == inc
                    and (
                        not sync_sales
                        or (
                            sales_amount_before == amount_ex
                            and sales_rate_before == rate
                        )
                    )
                ) else "update_contract"
                contract_project = projects_by_id.get(contract.project_id)
                operation.update(
                    action=action,
                    project_id=contract.project_id,
                    project_contract_id=contract.project_contract_id,
                    expected_contract_version=contract.version,
                    expected_contract_amount_inc_tax=_jsonable(
                        contract.amount_inc_tax
                    ),
                    # 生产 FK 保证项目存在；这里用 get 取不到时留 None，既让
                    # 「只有合同、项目未预载」的计划期假数据可用，也不影响正确性
                    # ——apply 阶段仍会重新解析并锁定真实项目后才写入。
                    expected_project_version=(
                        contract_project.version if contract_project else None
                    ),
                )
            elif current_relations:
                issue = _row_issue(
                    row_no,
                    "contract_ambiguous",
                    "销售订单的当前项目合同关系共享、未映射或未计入总额",
                )
                base.update(action="error", issues=[issue])
                source_rows.append(base)
                hard_issues.append(issue)
                continue
            elif historical_contracts.get(norm):
                issue = _row_issue(
                    row_no,
                    "historical_contract_conflict",
                    "销售订单存在历史合同关系，不能自动新建当前关系",
                )
                base.update(action="error", issues=[issue])
                source_rows.append(base)
                hard_issues.append(issue)
                continue
            else:
                active = active_assignments.get(norm, [])
                active_project_ids = {row["project_id"] for row in active}
                historical_project_ids = {
                    row["project_id"] for row in assignment_history.get(norm, [])
                }
                if len(active_project_ids) > 1:
                    issue = _row_issue(
                        row_no,
                        "project_assignment_ambiguous",
                        "同一销售订单当前挂靠多个维保项目，拒绝自动建合同",
                    )
                    base.update(action="error", issues=[issue])
                    source_rows.append(base)
                    hard_issues.append(issue)
                    continue
                if len(active_project_ids) == 1:
                    project_id = next(iter(active_project_ids))
                    if historical_project_ids - {project_id}:
                        issue = _row_issue(
                            row_no,
                            "project_assignment_history_conflict",
                            "销售订单历史上归属过其他项目，拒绝自动建合同",
                        )
                        base.update(action="error", issues=[issue])
                        source_rows.append(base)
                        hard_issues.append(issue)
                        continue
                    evidence = [row for row in active if row["project_id"] == project_id]
                    operation.update(
                        action="create_contract",
                        project_id=project_id,
                        project_contract_id=None,
                        expected_project_version=evidence[0]["project_version"],
                        expected_assignment_fingerprint=_assignment_fingerprint(evidence),
                    )
                    before = {
                        "contract_amount_inc_tax": None,
                        "sales_amount_ex_tax": sales_amount_before,
                        "sales_tax_rate": sales_rate_before,
                    }
                    after = {
                        "contract_amount_inc_tax": inc,
                        "sales_amount_ex_tax": amount_ex,
                        "sales_tax_rate": rate,
                    }
                    action = "create_contract"
                else:
                    # Never infer a project from customer/name text.  Only an
                    # explicit maintenance source fact may create one.
                    if not _is_explicit_maintenance_row(sheet, values):
                        base.update(
                            action="unmatched",
                            issues=[
                                _row_issue(
                                    row_no,
                                    "project_not_found",
                                    "未命中已有维保项目，且源行未明确标记维保业务，本批跳过",
                                    severity="warning",
                                )
                            ],
                        )
                        source_rows.append(base)
                        continue
                    if assignment_history.get(norm):
                        issue = _row_issue(
                            row_no,
                            "historical_assignment_conflict",
                            "销售订单存在历史项目归属，拒绝另建项目",
                        )
                        base.update(action="error", issues=[issue])
                        source_rows.append(base)
                        hard_issues.append(issue)
                        continue
                    if inverted_period_warning is not None:
                        issue = _row_issue(
                            row_no,
                            "invalid_maintenance_period",
                            "维保终止日期不能早于起始日期；新项目无法建立可信期限",
                        )
                        base.update(action="error", issues=[issue])
                        source_rows.append(base)
                        hard_issues.append(issue)
                        continue
                    try:
                        metadata = _maintenance_project_metadata(
                            sheet, values, row_no=row_no, norm=norm
                        )
                    except BulkImportInvalid as exc:
                        issue = _row_issue(
                            row_no, "invalid_maintenance_project", str(exc)
                        )
                        base.update(action="error", issues=[issue])
                        source_rows.append(base)
                        hard_issues.append(issue)
                        continue
                    existing_codes = projects_by_code.get(
                        metadata["project_code"].casefold(), []
                    )
                    if existing_codes:
                        issue = _row_issue(
                            row_no,
                            "maintenance_project_identity_conflict",
                            "XSDD 稳定项目编号已存在，拒绝自动创建",
                        )
                        base.update(action="error", issues=[issue])
                        source_rows.append(base)
                        hard_issues.append(issue)
                        continue
                    operation.update(
                        action="create_project",
                        project_id=None,
                        project_contract_id=None,
                        new_project=metadata,
                    )
                    before = {
                        "project": None,
                        "contract_amount_inc_tax": None,
                        "sales_amount_ex_tax": sales_amount_before,
                        "sales_tax_rate": sales_rate_before,
                    }
                    after = {
                        "project": metadata,
                        "contract_amount_inc_tax": inc,
                        "sales_amount_ex_tax": amount_ex,
                        "sales_tax_rate": rate,
                    }
                    action = "create_project"

            if inverted_period_warning is not None:
                base["issues"].append(inverted_period_warning)
            base.update(
                action=action,
                project_id=operation.get("project_id"),
                project_contract_id=operation.get("project_contract_id"),
                sales_order_id=operation.get("sales_order_id"),
                before=_jsonable(before),
                after=_jsonable(after),
            )
            source_rows.append(base)
            parsed.append(_jsonable(operation))

        # Fail closed per sales order, not per physical row.  A conflicting
        # duplicate or any other invalid row means that another row for the
        # same order cannot be treated as a trustworthy substitute.
        operation_norms = {
            str(operation.get("normalized_order_no") or "")
            for operation in parsed
        }
        blocked_norms = {
            str(row.get("normalized_order_no") or "")
            for row in source_rows
            if row.get("normalized_order_no")
            and (
                row.get("action") == "error"
                or (
                    row.get("action") == "skip"
                    and row.get("normalized_order_no") in operation_norms
                    and any(
                        issue.get("code") == "inactive_sales_order"
                        for issue in row.get("issues") or []
                    )
                )
            )
        }
        for norm in sorted(blocked_norms):
            affected_rows = [
                int(row["row_no"])
                for row in source_rows
                if row.get("normalized_order_no") == norm
            ]
            issue = _row_issue(
                min(affected_rows),
                "order_level_fail_closed",
                f"销售订单 {norm} 存在冲突或无效行，该订单全部操作已阻断",
            )
            hard_issues.append(issue)
            for row in source_rows:
                if row.get("normalized_order_no") != norm:
                    continue
                if not any(
                    item.get("code") == "order_level_fail_closed"
                    for item in row.get("issues") or []
                ):
                    row.setdefault("issues", []).append(issue)
                row["action"] = "error"
            for operation in parsed:
                if operation.get("normalized_order_no") != norm:
                    continue
                operation["blocked_action"] = operation.get("action")
                operation["action"] = "blocked"
                operation.setdefault("issues", []).append(issue)

        return _finish_plan(
            sheet,
            source_rows,
            parsed,
            hard_issues,
            extra={
                "amount_basis": "inc_tax",
                "source_sync": "f_sales_order+maintenance_project_contract",
                "missing_project_policy": (
                    "unique_active_wbdd=create_contract;"
                    "explicit_maintenance=create_project;otherwise_skip"
                ),
            },
        )

    def apply_plan(
        self,
        db: Session,
        plan: dict,
        *,
        operated_by: str,
        audit_reason: str,
        provenance: dict | None = None,
    ) -> dict:
        writes = 0
        noops = 0
        project_ids: set[str] = set()
        for item in sorted(
            plan["operations"],
            key=lambda row: (
                row["normalized_order_no"],
                row["action"],
            ),
        ):
            norm = item["normalized_order_no"]
            _advisory_lock(db, f"maintenance-bulk-sales:{norm}")
            sales: FSalesOrder | None = None
            if item.get("sales_order_id") is not None:
                sales = db.scalar(
                    select(FSalesOrder)
                    .where(FSalesOrder.id == item["sales_order_id"])
                    .with_for_update()
                )
                if sales is None:
                    raise BulkImportConflict("预览后的销售订单已不存在")
                if (
                    sales.raw_order_id != item["sales_raw_order_id"]
                    or normalize_order_no(sales.order_no) != norm
                    or _jsonable(sales.amount_ex_tax)
                    != item["expected_sales_amount_ex_tax"]
                    or _jsonable(sales.tax_rate) != item["expected_sales_tax_rate"]
                    or sales.data_status != item["expected_sales_data_status"]
                ):
                    raise BulkImportConflict("预览后的销售订单事实已变化，请重新预览")
            else:
                variants = {item["sales_order_no"], norm, f"XSDD-{norm}"}
                candidates = list(
                    db.scalars(
                        select(FSalesOrder)
                        .where(
                            or_(
                                FSalesOrder.raw_order_id == item["contract_id"],
                                FSalesOrder.order_no.in_(sorted(variants)),
                            )
                        )
                        .order_by(FSalesOrder.id)
                        .with_for_update()
                    )
                )
                if _sales_fingerprint(candidates) != item[
                    "expected_sales_candidates"
                ]:
                    raise BulkImportConflict(
                        "预览后销售事实匹配状态已变化，请重新预览"
                    )
            new_inc = Decimal(item["new_contract_amount_inc_tax"])
            new_ex = Decimal(item["new_sales_amount_ex_tax"])
            new_rate = Decimal(item["new_sales_tax_rate"])
            if item["action"] == "noop":
                contract = db.get(
                    MaintenanceProjectContract, item["project_contract_id"]
                )
                if (
                    contract is None
                    or contract.version != item["expected_contract_version"]
                    or _jsonable(contract.amount_inc_tax)
                    != item["expected_contract_amount_inc_tax"]
                ):
                    raise BulkImportConflict("预览后的合同金额已变化，请重新预览")
                _apply_sales_project_period(
                    db,
                    item=item,
                    project_id=contract.project_id,
                    audit_reason=audit_reason,
                    operated_by=operated_by,
                )
                alias_created = maintenance_project_identity.record_alias(
                    db,
                    project_id=contract.project_id,
                    alias_name=item.get("source_project_name"),
                    source="sales_order_import",
                )
                if alias_created:
                    operations.bump_workbook_revision(
                        db, project_id=contract.project_id
                    )
                project_ids.add(contract.project_id)
                noops += 1
                continue

            project_id: str
            if item["action"] == "update_contract":
                contract = db.get(
                    MaintenanceProjectContract, item["project_contract_id"]
                )
                if (
                    contract is None
                    or contract.version != item["expected_contract_version"]
                    or _jsonable(contract.amount_inc_tax)
                    != item["expected_contract_amount_inc_tax"]
                ):
                    raise BulkImportConflict("预览后的合同金额已变化，请重新预览")
                payload = operations.update_contract(
                    db,
                    project_contract_id=contract.project_contract_id,
                    version=contract.version,
                    updates={"contract_amount": new_inc},
                    reason=audit_reason,
                    operated_by=operated_by,
                )
                if payload is None:
                    raise BulkImportConflict("合同在应用期间消失")
                project_id = contract.project_id
            elif item["action"] == "create_contract":
                variants = {item["sales_order_no"], norm, f"XSDD-{norm}"}
                if _all_contracts_by_order(db, variants).get(norm):
                    raise BulkImportConflict("预览后该销售订单已出现合同关系")
                current, _history = _assignment_evidence(db, variants)
                evidence = current.get(norm, [])
                if _assignment_fingerprint(evidence) != item[
                    "expected_assignment_fingerprint"
                ]:
                    raise BulkImportConflict("预览后的 WBDD 项目挂靠已变化")
                project_id = item["project_id"]
                project = db.get(MaintenanceProject, project_id)
                if (
                    project is None
                    or not project.is_active
                    or project.version != item["expected_project_version"]
                ):
                    raise BulkImportConflict("预览后的目标项目已变化")
                payload = operations.create_contract(
                    db,
                    project_id=project_id,
                    contract_id=item["contract_id"],
                    contract_no=item["sales_order_no"],
                    contract_amount=new_inc,
                    contract_status=item["contract_status"],
                    status_mapping_state="mapped",
                    status_mapping_version="sales-order-bulk-v1",
                    included_in_total=True,
                    effective_from=date.fromisoformat(
                        item["contract_effective_from"]
                    ),
                    effective_to=None,
                    source="sales_order_bulk_v1",
                    reason=audit_reason,
                    operated_by=operated_by,
                )
                if payload is None:
                    raise BulkImportConflict("项目在创建合同期间消失")
            elif item["action"] == "create_project":
                variants = {item["sales_order_no"], norm, f"XSDD-{norm}"}
                if _all_contracts_by_order(db, variants).get(norm):
                    raise BulkImportConflict("预览后该销售订单已出现合同关系")
                current, history = _assignment_evidence(db, variants)
                if current.get(norm) or history.get(norm):
                    raise BulkImportConflict("预览后销售订单已出现项目归属")
                metadata = item["new_project"]
                for existing in db.scalars(select(MaintenanceProject)):
                    if existing.project_code.casefold() == metadata["project_code"].casefold():
                        raise BulkImportConflict("预览后 XSDD 稳定项目编号已被占用")
                # 负责人取销售订单「项目经理(必填)」首位；该列是多值且不同行
                # 顺序不一致，完整原值写进建项审计 reason（项目表无备注列，
                # 不为此加迁移），信息不丢也不猜主责。
                manager_primary = metadata.get("project_manager")
                manager_raw = metadata.get("project_manager_raw")
                create_reason = audit_reason
                if manager_raw and manager_raw != manager_primary:
                    create_reason = f"{audit_reason}；销售订单项目经理原值：{manager_raw}"
                created = catalog.create_project(
                    db,
                    project_code=metadata["project_code"],
                    display_name=metadata["display_name"],
                    project_manager_id=manager_primary,
                    # 业务类型早就从源表解析进 metadata 了，此前没往下传、就地丢弃：
                    # 生产 648 个项目 647 个 business_type 为 NULL，卡墙的业务类型
                    # 筛选一个也筛不出来。这是 D-05 认定的唯一正规建项来源，补上之后
                    # 此后新建的 XSDD 项目自带业务类型（源表没填仍是 None，不猜）。
                    business_type=metadata.get("business_type"),
                    reason=create_reason,
                    operated_by=operated_by,
                )
                project_id = created["project_id"]
                # 期限可缺可单侧（D-05）：只写销售订单上确实给了的那一侧，
                # 两侧都没有就完全不发 update，项目保持 NULL 待人工补。
                period_updates = {
                    key: date.fromisoformat(metadata[key])
                    for key in ("period_from", "period_to")
                    if metadata.get(key)
                }
                if period_updates:
                    updated = catalog.update_project(
                        db,
                        project_id=project_id,
                        version=created["version"],
                        updates=period_updates,
                        reason=audit_reason,
                        operated_by=operated_by,
                    )
                    if updated is None:
                        raise BulkImportConflict("维保项目在创建期间消失")
                payload = operations.create_contract(
                    db,
                    project_id=project_id,
                    contract_id=item["contract_id"],
                    contract_no=item["sales_order_no"],
                    contract_amount=new_inc,
                    contract_status=item["contract_status"],
                    status_mapping_state="mapped",
                    status_mapping_version="sales-order-bulk-v1",
                    included_in_total=True,
                    effective_from=date.fromisoformat(
                        item["contract_effective_from"]
                    ),
                    effective_to=None,
                    source="sales_order_bulk_v1",
                    reason=audit_reason,
                    operated_by=operated_by,
                )
                if payload is None:
                    raise BulkImportConflict("维保项目在创建合同期间消失")
            else:
                raise BulkImportConflict("预览计划包含未知销售订单动作")

            if item["action"] != "create_project":
                _apply_sales_project_period(
                    db,
                    item=item,
                    project_id=project_id,
                    audit_reason=audit_reason,
                    operated_by=operated_by,
                )
            alias_created = maintenance_project_identity.record_alias(
                db,
                project_id=project_id,
                alias_name=item.get("source_project_name"),
                source="sales_order_import",
            )
            if alias_created:
                # create/update contract already bumps in this transaction;
                # the dedupe registry makes this exactly-once.  It is the sole
                # bump for an amount-noop row that contributes a new peer name.
                operations.bump_workbook_revision(db, project_id=project_id)

            if sales is not None:
                before = {
                    "amount_ex_tax": _jsonable(sales.amount_ex_tax),
                    "tax_rate": _jsonable(sales.tax_rate),
                }
                sales.amount_ex_tax = new_ex
                sales.tax_rate = new_rate
                after = {
                    "amount_ex_tax": _jsonable(new_ex),
                    "tax_rate": _jsonable(new_rate),
                }
                if before != after:
                    db.add(SysAuditLog(
                        entity_type="sales_order_contract_amount",
                        entity_id=sales.id,
                        action="overwrite",
                        before_json=before,
                        after_json=after,
                        reason=audit_reason,
                        operated_by=operated_by,
                    ))
            writes += 1
            project_ids.add(project_id)
        return {
            "written": writes,
            "noop": noops,
            "operation": "sales_contract_amount_apply",
            "project_ids": sorted(project_ids),
        }


class ReceiptCollectionAdapter(HeaderAdapter):
    key = "receipt_collection"
    file_type = "maint_receipt"
    label = "收款单累计实收"
    aliases = {
        "order_no": ("收款明细.销售订单(必填)", "收款明细.销售订单", "销售订单", "合同编号"),
        "receipt_no": ("收款单号(必填)", "收款单号", "凭证号"),
        "receipt_date": ("收款日期(必填)", "收款日期", "到账日期"),
        "gross_amount": ("收款明细.销售收款金额(必填)", "销售收款金额"),
        "discount_amount": ("收款明细.优惠金额", "优惠金额"),
        "actual_amount": ("收款明细.实收金额", "实收金额", "到账金额"),
        "remark": ("收款明细.备注", "备注"),
        "receipt_status": ("数据状态", "收款状态", "状态"),
    }
    system_aliases = {
        "order_no": ("F0000008",),
        "receipt_no": ("SeqNo",),
        "receipt_date": ("F0000001",),
        "gross_amount": ("F0000013",),
        "discount_amount": ("F0000061",),
        "actual_amount": ("F0000062",),
        "remark": ("F0000037",),
        "receipt_status": ("Status",),
    }
    required_fields = frozenset({"order_no", "receipt_no", "receipt_date", "actual_amount"})
    # 逐行金额：合并即拒绝（一笔钱不能铺成多笔）。主表汇总列没被映射，不受影响。
    merge_guarded_fields = frozenset({"actual_amount", "gross_amount", "discount_amount"})

    @staticmethod
    def _refs(values: list[tuple[date, str]]) -> str:
        refs = [ref for _day, ref in sorted(set(values))]
        joined = ",".join(refs)
        return joined if len(joined) <= 128 else f"{refs[0]}等{len(refs)}笔"

    def build_plan(self, db: Session, sheet: DetectedSheet) -> dict:
        all_contracts, safe_contracts = _contract_maps(db)
        source_rows: list[dict] = []
        hard_issues: list[dict] = []
        by_order: dict[str, list[dict]] = defaultdict(list)
        seen_keys: dict[tuple[str, str], tuple[date, Decimal, int]] = {}

        for row_no, values in sheet.rows:
            upstream_status: str | None = None
            order_raw = _text(_value(sheet, values, "order_no"))
            receipt_no = _text(_value(sheet, values, "receipt_no"))
            norm = normalize_order_no(order_raw)
            base = {
                "row_no": row_no,
                "business_key": f"{receipt_no}|{order_raw}",
                "normalized_order_no": norm,
                "receipt_no": receipt_no,
                "issues": [],
            }
            # 归属先于校验（2026-09-08）：收款单导出是全公司的，实拍 298 行里 297 行是
            # 备件销售 / 销售换货 / 租赁 / 整机销售。这些订单没有维保合同，本来就一分钱
            # 都不会写进维保，却要先撞金额（退货负数）、状态、备注三道校验，报出一堆与
            # 维保无关的硬错误，把真正该看的错误淹掉；「其他收款」那种有单号无订单的行
            # 报的还是「销售订单号和收款单号不能为空」，与事实不符。
            #
            # 判定权威仍是**维保合同**而不是业务类型——业务类型只作分类、维保业务=是 才是
            # 建项依据（2026-09-03 拍板，见 _is_explicit_maintenance_row）。这里只是把
            # 已有的合同匹配提前，不新增任何按业务类型挡钱的规则。
            if norm and norm not in safe_contracts and not all_contracts.get(norm):
                base.update(
                    action="unmatched",
                    issues=[_row_issue(
                        row_no,
                        "project_not_found",
                        "销售订单未关联当前维保项目，本批跳过且不创建项目",
                        severity="warning",
                    )],
                )
                source_rows.append(base)
                continue
            if not norm and receipt_no:
                base.update(
                    action="unmatched",
                    issues=[_row_issue(
                        row_no,
                        "receipt_without_order",
                        f"收款单 {receipt_no} 没有销售订单（其他收款 / 预付款等），"
                        "无法归属到维保合同，本批跳过",
                        severity="warning",
                    )],
                )
                source_rows.append(base)
                continue

            try:
                if not norm or not receipt_no:
                    raise BulkImportInvalid("销售订单号和收款单号不能为空")
                receipt_status = _text(_value(sheet, values, "receipt_status"))
                if (
                    "receipt_status" in sheet.field_indexes
                    and receipt_status != config.ACTIVE_STATUS
                ):
                    # 源状态非已生效（作废 / 空值 / 其他）不再一律判无效：先照常解析，
                    # 对照台账后再定——台账仍有该单生效金额 = 上游作废与台账的冲突
                    # （receipt_voided_upstream，走裁决）；台账已裁为 0 = 已知跳过；
                    # 台账从未见过的仍判无效（见下方 state == "voided" 分支）。
                    upstream_status = receipt_status
                receipt_date = _date(_value(sheet, values, "receipt_date"), label="收款日期")
                actual = _decimal(_value(sheet, values, "actual_amount"), label="实收金额")
                gross = _optional_decimal(
                    _value(sheet, values, "gross_amount"), label="销售收款金额"
                )
                discount = _optional_decimal(
                    _value(sheet, values, "discount_amount"), label="优惠金额"
                ) or Decimal("0.00")
                if gross is not None and abs((gross - discount) - actual) > Decimal("0.01"):
                    raise BulkImportInvalid("销售收款金额-优惠金额与实收金额不一致")
                remark = _text(_value(sheet, values, "remark"))
                risk_word = next(
                    (
                        word
                        for word in ("坏账", "红冲", "冲销", "作废")
                        if word in remark
                    ),
                    None,
                )
                if risk_word:
                    raise BulkImportInvalid(
                        f"备注命中“{risk_word}”，需要人工确认，不能自动累计"
                    )
            except BulkImportInvalid as exc:
                issue = _row_issue(row_no, "invalid_receipt", str(exc))
                base.update(action="error", issues=[issue])
                source_rows.append(base)
                hard_issues.append(issue)
                continue

            if "receipt_status" not in sheet.field_indexes:
                base["issues"].append(
                    _row_issue(
                        row_no,
                        "source_status_missing",
                        "源收款导出没有状态列；已保留警告并仅按金额/备注做保守校验",
                        severity="warning",
                    )
                )

            # 文件内去重：同一 (销售订单, 收款单号) 只计一次；金额/日期不一致即冲突。
            duplicate = seen_keys.get((norm, receipt_no))
            if duplicate is not None:
                if duplicate[:2] != (receipt_date, actual):
                    issue = _row_issue(
                        row_no,
                        "duplicate_receipt_conflict",
                        f"同一收款单+销售订单与第 {duplicate[2]} 行金额/日期不一致",
                    )
                    base.update(action="error", issues=[issue])
                    hard_issues.append(issue)
                else:
                    base.update(
                        action="duplicate",
                        issues=[_row_issue(
                            row_no,
                            "duplicate_same",
                            f"与第 {duplicate[2]} 行相同，累计时只计一次",
                            severity="warning",
                        )],
                    )
                source_rows.append(base)
                continue
            seen_keys[(norm, receipt_no)] = (receipt_date, actual, row_no)

            current_relations = all_contracts.get(norm, [])
            contract = safe_contracts.get(norm)
            if contract is None:
                if not current_relations:
                    base.update(
                        action="unmatched",
                        issues=[_row_issue(
                            row_no,
                            "project_not_found",
                            "销售订单未关联当前维保项目，本批跳过且不创建项目",
                            severity="warning",
                        )],
                    )
                else:
                    issue = _row_issue(
                        row_no,
                        "contract_ambiguous",
                        "销售订单的当前项目合同关系共享、未映射或未计入总额",
                    )
                    base.update(action="error", issues=[issue])
                    hard_issues.append(issue)
                source_rows.append(base)
                continue
            base.update(
                action="matched",
                project_id=contract.project_id,
                project_contract_id=contract.project_contract_id,
                actual_amount=_jsonable(actual),
                gross_amount=_jsonable(gross),
                discount_amount=_jsonable(discount),
                receipt_date=receipt_date.isoformat(),
            )
            source_rows.append(base)
            by_order[norm].append({
                "row_no": row_no,
                "receipt_no": receipt_no,
                "receipt_date": receipt_date,
                "actual": actual,
                "remark": remark,
                "contract": contract,
                "source": base,
                "state": "voided" if upstream_status is not None else "new",
                "upstream_status": upstream_status,
            })

        # 跨批次幂等（D-16）：按 (销售订单, 收款单号) 对照台账**生效行**。同单号同金额
        # 同日期 = 已入账，跳过；同单号不同金额/日期 = 硬冲突，人工裁决，绝不覆盖或
        # 累加。被裁决取代的旧行不参与幂等判定——裁决后再预览同一文件，更正值即"已入账"；
        # 但历史行仍要取出：覆盖起点与该月的台账支撑都看它（D-16 09-07 二次补充 #12）。
        ledger_all = _ledger_receipts(db, sorted(by_order))
        ledger_rows = [row for row in ledger_all if row.is_active]
        ledger_by_key = {(row.contract_no, row.receipt_no): row for row in ledger_rows}
        ledger_known = 0
        ledger_conflicts = 0
        for norm, receipts in by_order.items():
            for receipt in receipts:
                known = ledger_by_key.get((norm, receipt["receipt_no"]))
                source = receipt["source"]
                if receipt["state"] == "voided":
                    # 上游作废（D-16 09-07 二次补充 #13）：台账仍有生效金额 = 冲突，只能
                    # 裁决为 0，不自动作废台账行；已裁为 0 = 已知；台账没见过 = 无效。
                    status_text = receipt["upstream_status"] or "空"
                    if known is None:
                        issue = _row_issue(
                            receipt["row_no"],
                            "invalid_receipt",
                            "收款状态必须明确为已生效，不接受空值、作废或其他状态",
                        )
                        source["action"] = "error"
                        source["issues"].append(issue)
                        hard_issues.append(issue)
                    elif Decimal(known.actual_amount) == 0:
                        ledger_known += 1
                        source["action"] = "known"
                        source["issues"].append(_row_issue(
                            receipt["row_no"],
                            "receipt_known",
                            (
                                f"收款单 {receipt['receipt_no']} 源状态「{status_text}」"
                                f"（非已生效），台账生效行已裁为 0（{_ledger_stamp(known)}），"
                                "本次跳过"
                            ),
                            severity="warning",
                        ))
                    else:
                        ledger_conflicts += 1
                        source["ledger_receipt"] = {
                            "receipt_no": known.receipt_no,
                            "receipt_date": known.receipt_date.isoformat(),
                            "actual_amount": _jsonable(known.actual_amount),
                            "import_batch_id": known.import_batch_id,
                        }
                        issue = _row_issue(
                            receipt["row_no"],
                            "receipt_voided_upstream",
                            (
                                f"收款单 {receipt['receipt_no']} 源状态「{status_text}」"
                                "（非已生效），但台账仍有生效行"
                                f"（{_jsonable(known.actual_amount)} / {known.receipt_date:%Y-%m-%d}"
                                f"，批次 {known.import_batch_id or '-'}）；上游作废不自动作废"
                                "台账，请先裁决把该行裁为 0"
                                "（POST /maintenance/project-batch-transfer/receipt-rulings）"
                                "，本次不累计也不作废"
                            ),
                        )
                        source["action"] = "error"
                        source["issues"].append(issue)
                        hard_issues.append(issue)
                    continue
                if known is None:
                    continue
                if (
                    known.receipt_date == receipt["receipt_date"]
                    and known.actual_amount == receipt["actual"]
                ):
                    receipt["state"] = "known"
                    ledger_known += 1
                    source["action"] = "known"
                    source["issues"].append(_row_issue(
                        receipt["row_no"],
                        "receipt_known",
                        (
                            f"收款单 {receipt['receipt_no']} 已在台账"
                            f"（批次 {known.import_batch_id or '-'}"
                            f"，{_ledger_stamp(known)}），本次跳过"
                        ),
                        severity="warning",
                    ))
                    continue
                receipt["state"] = "conflict"
                ledger_conflicts += 1
                source["ledger_receipt"] = {
                    "receipt_no": known.receipt_no,
                    "receipt_date": known.receipt_date.isoformat(),
                    "actual_amount": _jsonable(known.actual_amount),
                    "import_batch_id": known.import_batch_id,
                }
                issue = _row_issue(
                    receipt["row_no"],
                    "receipt_conflict",
                    (
                        f"收款单 {receipt['receipt_no']} 与台账不一致"
                        f"（台账 {_jsonable(known.actual_amount)} / {known.receipt_date:%Y-%m-%d}"
                        f"，本文件 {_jsonable(receipt['actual'])} / {receipt['receipt_date']:%Y-%m-%d}）"
                        "，需人工裁决，不自动覆盖也不累加"
                    ),
                )
                source["action"] = "error"
                source["issues"].append(issue)
                hard_issues.append(issue)

        # A receipt export represents cumulative history.  If any physical row
        # for an order is invalid (risk remark, status, amount/date, a
        # conflicting duplicate, or a ledger conflict), calculating from only
        # the remaining rows would silently create a partial cumulative
        # snapshot.  Freeze every known month for that order as blocked instead.
        # 连坐粒度是**收款单**，不是销售订单（2026-09-08）。一张收款单是一张资金凭证：
        # 「退换货核销 / 平账」单的正腿和负腿天生落在不同销售订单上（实拍 10 张单，7 张
        # 整单净额为 0、银行一分钱没动），按订单连坐永远够不着正腿——负腿被判无效丢掉，
        # 正腿绿色、无警告、默认勾选，一点就写进台账和 confirmed 累计快照。任何一行不可
        # 信，整张单涉及的订单都不该按剩下的行算累计。
        orders_by_receipt: dict[str, set[str]] = defaultdict(set)
        for row in source_rows:
            receipt_no = str(row.get("receipt_no") or "")
            norm = str(row.get("normalized_order_no") or "")
            if receipt_no and norm:
                orders_by_receipt[receipt_no].add(norm)
        blocked_norms: set[str] = set()
        # 自己有坏行的订单 → order_level_fail_closed；被同一张单的坏行牵连的 → receipt_level。
        own_blocked: set[str] = set()
        blocking_receipts: dict[str, set[str]] = defaultdict(set)
        for row in source_rows:
            if row.get("action") != "error":
                continue
            norm = str(row.get("normalized_order_no") or "")
            if norm:
                own_blocked.add(norm)
                blocked_norms.add(norm)
            receipt_no = str(row.get("receipt_no") or "")
            for peer in orders_by_receipt.get(receipt_no, ()):
                blocked_norms.add(peer)
                if peer != norm:
                    blocking_receipts[peer].add(receipt_no)
        blocked_operations: list[dict] = []
        for norm in sorted(blocked_norms):
            order_sources = [
                row
                for row in source_rows
                if row.get("normalized_order_no") == norm
            ]
            if norm in own_blocked:
                code = "order_level_fail_closed"
                message = (
                    f"销售订单 {norm} 存在无效/风险/冲突收款行，禁止从其余行计算部分累计"
                )
            else:
                receipts_text = "、".join(sorted(blocking_receipts.get(norm, ())))
                code = "receipt_level_fail_closed"
                message = (
                    f"收款单 {receipts_text} 存在无效/风险/冲突明细行，"
                    f"整张单涉及的销售订单（含 {norm}）本批一律不入账——"
                    "一张收款单整体成立或整体不成立，不按剩下的行算累计"
                )
            issue = _row_issue(
                min(int(row["row_no"]) for row in order_sources), code, message
            )
            hard_issues.append(issue)
            for row in order_sources:
                if not any(
                    item.get("code") in {"order_level_fail_closed", "receipt_level_fail_closed"}
                    for item in row.get("issues") or []
                ):
                    row.setdefault("issues", []).append(issue)
            receipts = by_order.pop(norm, [])
            monthly: dict[date, list[dict]] = defaultdict(list)
            for receipt in receipts:
                if receipt["state"] == "voided":
                    # 上游作废行不是要累计的收款，不为它冻结一个月份行。
                    continue
                day = receipt["receipt_date"]
                monthly[date(day.year, day.month, 1)].append(receipt)
            for month, month_rows in sorted(monthly.items()):
                contract = month_rows[0]["contract"]
                blocked_operations.append({
                    "row_no": min(row["row_no"] for row in month_rows),
                    "normalized_order_no": norm,
                    "project_id": contract.project_id,
                    "project_contract_id": contract.project_contract_id,
                    "expected_contract_version": contract.version,
                    "report_month": month.isoformat(),
                    # Deliberately absent: no partial cumulative value is ever
                    # calculated from the subset that happened to parse.
                    "new_cumulative_amount": None,
                    "receipt_reference": self._refs([
                        (row["receipt_date"], row["receipt_no"])
                        for row in month_rows
                    ]),
                    "action": "conflict",
                    "issues": [issue],
                })

        contract_ids = sorted({
            rows[0]["contract"].project_contract_id for rows in by_order.values() if rows
        })
        existing_rows = _existing_snapshots(db, contract_ids)
        previous_operators = _snapshot_operators(db, existing_rows)
        operations_plan: list[dict] = blocked_operations
        # 预览期合同状态指纹（台账生效行 + 基线 + 全部快照）：应用时加锁复算，
        # 不等即 stale_preview——预览→应用之间的台账增长 / 基线修正 / 他人预览
        # 都不能让冻结的累计值原样落库。
        contract_states: dict[str, dict] = {}

        for norm, receipts in sorted(by_order.items()):
            contract = receipts[0]["contract"]
            new_receipts = [row for row in receipts if row["state"] == "new"]
            ledger_history = [row for row in ledger_all if row.contract_no == norm]
            ledger_active = [row for row in ledger_history if row.is_active]
            contract_snapshots = [
                row for row in existing_rows
                if row.project_contract_id == contract.project_contract_id
            ]
            if not new_receipts and not _ledger_snapshot_drift(
                ledger_active=ledger_active,
                snapshots=contract_snapshots,
                ledger_history=ledger_history,
            ):
                # 全部已入账且台账推导与已确认快照一致：本文件不会改变任何快照。
                # 裁决只改台账不改快照（D-16 第 7 点）：裁决后再预览同一文件，收款全部
                # "已入账"，但快照与台账已不等——仍要出 update 行，否则更正永远浮不上来。
                continue
            items, state = self._contract_operations(
                norm=norm,
                contract=contract,
                new_receipts=new_receipts,
                anchor_row_no=min(row["row_no"] for row in receipts),
                ledger_active=ledger_active,
                ledger_history=ledger_history,
                snapshots=contract_snapshots,
                hard_issues=hard_issues,
                previous_operators=previous_operators,
            )
            operations_plan.extend(items)
            contract_states[contract.project_contract_id] = state

        return _finish_plan(
            sheet,
            source_rows,
            operations_plan,
            hard_issues,
            extra={
                "amount_basis": "actual_received_inc_tax",
                "source_amount_field": "收款明细.实收金额",
                "missing_project_policy": "skip_without_create",
                # D-16：月度累计 = 台账生效收款 ∪ 本文件新收款（见 _contract_operations）；
                # 既有快照相等即 noop，不等即显式勾选的 update，不再整合同阻断。
                "cumulative_basis": "ledger_union_file",
                "existing_snapshot_policy": "noop_or_explicit_update",
                "ledger": {
                    "known": ledger_known,
                    "conflicts": ledger_conflicts,
                },
                "contract_states": contract_states,
                "source_warnings": (
                    [
                        {
                            "code": "source_status_missing",
                            "message": "源收款工作簿没有状态列；每行均带同名 warning",
                            "severity": "warning",
                        }
                    ]
                    if "receipt_status" not in sheet.field_indexes
                    else []
                ),
            },
        )

    def _contract_operations(
        self,
        *,
        norm: str,
        contract: Any,
        new_receipts: list[dict],
        ledger_active: list[Any],
        snapshots: list[Any],
        hard_issues: list[dict],
        previous_operators: dict[str, str] | None = None,
        anchor_row_no: int | None = None,
        ledger_history: list[Any] | None = None,
    ) -> tuple[list[dict], dict]:
        """一个合同的月度目标操作：台账 ∪ 本文件新收款推导累计，逐月对照快照。

        累计(月) = 基线 + Σ台账生效收款(日期 ≤ 月末) + Σ本文件新收款(日期 ≤ 月末)。
        基线 = 覆盖起点之前最晚一条已确认、非 bulk_import 来源的快照（台账建立
        之前的历史累计只存在于快照里；没有基线，第一次增量导入要么被单调性
        守卫拦下，要么算出偏低却仍单调的累计静默写库）。台账覆盖完整时基线为 0，
        公式退化为纯收款求和。覆盖起点、基线的完整规则见 ``_cumulative_series``。

        受影响月份 = 覆盖起点及之后的所有候选月（台账月、文件月、既有快照月）——
        覆盖范围内每个月都按台账对账，快照与推导值不等即 update 行（默认不勾选）；
        覆盖起点之前的月份只作基线，不动。未确认快照同样是历史（D-16 09-07 二次
        补充 #12）：不等时与已确认同论（seed_required / cumulative_unverifiable），
        相等时出 update 行把它确认掉；从不当基线。

        月份的"台账支撑"：该月有台账导入行（含被裁决取代的历史行——裁决说明了差在
        哪笔），或该月快照本就由台账推导（``source='bulk_import'``）。没有支撑的
        月份推导值与快照不等即 ``cumulative_unverifiable``。

        合同级 fail-closed（整合同全部月份 conflict、不算任何累计值）：
        * ``snapshot_voided``：覆盖范围内有已作废快照，或基线之后、覆盖起点之前
          有已作废快照（基线跳过它、候选月也不含它，作废月的收款既不在台账也不在
          基线里，后续累计会静默丢掉它）。作废月的收款会被后续月份的累计算进去
          却永远进不了台账，只挡作废月不够；
        * ``seed_required``：合同**尚无台账**，而覆盖范围内某个已确认月份按本文件
          推导出的累计与快照不等——历史累计里的收款不在本文件里，从中间月份起
          增量导入只会把历史越算越少（旧 05 表时代的合同必须先用全量历史导出
          建账，全部月份逐一复现快照才 record_receipts 入台账）。
        行级 fail-closed：``cumulative_unverifiable``——推导值与已确认值不等（高于
        低于同论，REQUIREMENTS #57 对称）、且该月没有任何台账收款支撑（无从核实
        差在哪笔），不提供覆盖。
        """

        previous_operators = previous_operators or {}
        if ledger_history is None:
            ledger_history = ledger_active
        series = _cumulative_series(
            new_receipts=new_receipts,
            ledger_active=ledger_active,
            snapshots=snapshots,
            ledger_history=ledger_history,
        )
        coverage_start: date = series["coverage_start"]
        baseline_row = series["baseline_row"]
        baseline = (
            {
                "report_month": baseline_row.report_month.isoformat(),
                "cumulative_amount": _jsonable(baseline_row.cumulative_amount),
                "source": baseline_row.source,
            }
            if baseline_row is not None
            else None
        )
        state = {
            "contract_no": norm,
            "coverage_start": coverage_start.isoformat(),
            "fingerprint": _contract_fingerprint(
                ledger_active=ledger_active,
                snapshots=snapshots,
                baseline_row=baseline_row,
            ),
        }
        by_month: dict[date, Any] = {row.report_month: row for row in snapshots}
        if anchor_row_no is None:
            anchor_row_no = min(row["row_no"] for row in new_receipts)

        items: list[dict] = []
        fail_closed: list[dict] = []
        # 基线之后、覆盖起点之前的已作废快照对推导不可见：基线取"最晚一条已确认"
        # 跳过了它，候选月又只从覆盖起点算起。作废月的收款不在台账、基线也不含，
        # 后续每个月的累计都会静默少掉它——与覆盖范围内的作废同样整合同 fail-closed。
        for row in sorted(snapshots, key=lambda row: row.report_month):
            if row.status != "void" or row.report_month >= coverage_start:
                continue
            if baseline_row is not None and row.report_month <= baseline_row.report_month:
                continue
            position = (
                f"晚于基线 {baseline_row.report_month:%Y-%m}" if baseline_row is not None
                else "无更早基线"
            )
            fail_closed.append(_row_issue(
                anchor_row_no,
                "snapshot_voided",
                (
                    f"{contract.contract_no} {row.report_month:%Y-%m} 快照已作废且{position}"
                    f"、早于覆盖起点 {coverage_start:%Y-%m}，作废月收款不在台账，"
                    "导入不自动复活，请人工处理"
                ),
            ))
        for month in series["months"]:
            month_new = [
                row for row in new_receipts
                if _month_start(row["receipt_date"]) == month
            ]
            month_ledger = [
                row for row in ledger_active
                if _month_start(row.receipt_date) == month
            ]
            # 该月的台账支撑：导入行（含被裁决取代的历史行）落在该月。
            month_evidence = any(
                _is_import_row(row) and _month_start(row.receipt_date) == month
                for row in ledger_history
            )
            cumulative: Decimal = series["cumulative"][month]
            refs = self._refs(
                [(row.receipt_date, row.receipt_no) for row in month_ledger]
                + [(row["receipt_date"], row["receipt_no"]) for row in month_new]
            )
            current = by_month.get(month)
            item = {
                "row_no": min(row["row_no"] for row in month_new) if month_new else anchor_row_no,
                "normalized_order_no": norm,
                "project_id": contract.project_id,
                "project_contract_id": contract.project_contract_id,
                "expected_contract_version": contract.version,
                "report_month": month.isoformat(),
                "new_cumulative_amount": _jsonable(cumulative),
                "receipt_reference": refs,
                "new_receipts": [
                    {
                        "row_no": row["row_no"],
                        "receipt_no": row["receipt_no"],
                        "receipt_date": row["receipt_date"].isoformat(),
                        "actual_amount": _jsonable(row["actual"]),
                        "remark": row["remark"] or None,
                    }
                    for row in sorted(month_new, key=lambda row: row["row_no"])
                ],
                "ledger_receipt_count": len(month_ledger),
                "coverage_start": coverage_start.isoformat(),
                "baseline": baseline,
                "issues": [],
            }
            if not month_new:
                earlier_new = any(
                    _month_start(row["receipt_date"]) < month for row in new_receipts
                )
                if earlier_new:
                    item["issues"].append(_row_issue(
                        item["row_no"],
                        "cascade_from_earlier_month",
                        f"{month:%Y-%m} 本文件无新收款；累计因更早月份新增收款而变化",
                        severity="warning",
                    ))
                elif current is None:
                    # 只有台账、没有快照的月份：不是漂移，是回填。
                    item["issues"].append(_row_issue(
                        item["row_no"],
                        "ledger_backfill",
                        f"{month:%Y-%m} 该月只有台账收款、尚无快照，将按台账新建",
                        severity="info",
                    ))
                else:
                    item["issues"].append(_row_issue(
                        item["row_no"],
                        "ledger_drift",
                        f"{month:%Y-%m} 本文件无新收款；按台账推导的累计与现有快照不一致",
                        severity="warning",
                    ))
            if current is None:
                item.update(action="create", expected_collection_id=None)
            elif current.status == "void":
                issue = _row_issue(
                    item["row_no"],
                    "snapshot_voided",
                    f"{contract.contract_no} {month:%Y-%m} 快照已作废，导入不自动复活，请人工处理",
                )
                item.update(
                    action="conflict",
                    expected_collection_id=current.collection_id,
                    expected_collection_version=current.version,
                    expected_current_amount=_jsonable(current.cumulative_amount),
                    # 作废值不是已确认累计，单调性预检不得把它当成邻月现值。
                    expected_current_status="void",
                    issues=[*item["issues"], issue],
                )
                fail_closed.append(issue)
            elif current.status == "confirmed" and current.cumulative_amount == cumulative:
                item.update(
                    action="record_receipts" if month_new else "noop",
                    expected_collection_id=current.collection_id,
                    expected_collection_version=current.version,
                    expected_current_amount=_jsonable(current.cumulative_amount),
                    preserve_receipt_reference=current.receipt_reference,
                )
                if month_new:
                    # 前端按真实 code 打"仅登记入台账"标签，不再猜 action。
                    item["issues"].append(_row_issue(
                        item["row_no"],
                        "record_receipts",
                        f"累计不变，只把 {len(month_new)} 笔新收款登记入台账",
                        severity="info",
                    ))
            else:
                item.update(
                    action="update",
                    expected_collection_id=current.collection_id,
                    expected_collection_version=current.version,
                    expected_current_amount=_jsonable(current.cumulative_amount),
                    expected_current_status=current.status,
                    previous_source=current.source,
                    previous_import_batch_id=current.import_batch_id,
                    previous_updated_at=_jsonable(getattr(current, "updated_at", None)),
                    previous_operated_by=previous_operators.get(current.collection_id),
                )
                # 未确认快照同样是历史：不等时与已确认同论；相等的未确认月走 update
                # 把它确认掉，不触发建账 / 不可核实。
                mismatch = (
                    current.status in _HISTORY_STATUSES
                    and Decimal(current.cumulative_amount) != cumulative
                )
                unconfirmed = current.status == "unconfirmed"
                status_text = "未确认" if unconfirmed else "已确认"
                if mismatch and not ledger_active:
                    fail_closed.append(_row_issue(
                        item["row_no"],
                        "seed_required",
                        (
                            f"{contract.contract_no} {month:%Y-%m}："
                            f"该合同已有{'未确认' if unconfirmed else '确认'}快照"
                            "但尚无收款单台账，请先上传该合同的全量历史收款单导出建账"
                            f"（快照 {_jsonable(current.cumulative_amount)}，"
                            f"本文件推导 {_jsonable(cumulative)}）"
                        ),
                    ))
                elif mismatch and not (month_evidence or current.source == "bulk_import"):
                    # 高于 / 低于同论（REQUIREMENTS #57 对称）：该月没有任何台账收款，
                    # 推导值与人确认的值之差无从核实来自哪笔，两个方向都不提供覆盖。
                    direction = (
                        "低于" if cumulative < Decimal(current.cumulative_amount) else "高于"
                    )
                    issue = _row_issue(
                        item["row_no"],
                        "cumulative_unverifiable",
                        (
                            f"{contract.contract_no} {month:%Y-%m} 按台账推导的累计 "
                            f"{_jsonable(cumulative)} {direction}{status_text}累计 "
                            f"{_jsonable(current.cumulative_amount)}，且该月没有台账收款"
                            "支撑，无法核实差在哪笔，不提供覆盖；请上传该月完整收款单"
                            "导出或人工改回"
                        ),
                    )
                    item["action"] = "conflict"
                    item["issues"].append(issue)
                    hard_issues.append(issue)
            items.append(item)

        if fail_closed:
            # 合同级 fail-closed：与 order_level_fail_closed 同形——每个月都是
            # conflict，不留任何"从其余月份算出来"的累计值。
            reasons = {issue["code"] for issue in fail_closed}
            for issue in fail_closed:
                hard_issues.append(issue)
            for item in items:
                item["action"] = "conflict"
                item["new_cumulative_amount"] = None
                item["requires_months"] = []
                item["depends_on_months"] = []
                for issue in fail_closed:
                    if issue not in item["issues"]:
                        item["issues"].append(issue)
            state["fail_closed"] = sorted(reasons)
            return items, state

        # 一个月的累计包含更早月份的新收款：单独勾选它会把台账没有的收款算进
        # 快照（record_receipts 同理：只登记本月收款而不登记构成月的，台账与快照
        # 脱节）。带新收款的更早行必须同批勾选，预览提示、应用硬拒。带新收款却已
        # 被阻断的月份（cumulative_unverifiable 等）同样是构成月——依赖它的行永远
        # 凑不齐勾选，下面随之阻断，不留任何"从其余月份算出来"的累计值。
        constituents: list[str] = []
        for item in items:
            item["requires_months"] = list(constituents)
            item["depends_on_months"] = []
            if constituents and item["action"] in {"create", "update", "record_receipts"}:
                item["issues"].append(_row_issue(
                    item["row_no"],
                    "requires_earlier_months",
                    "累计包含更早月份 "
                    + "、".join(
                        f"{date.fromisoformat(value):%Y-%m}" for value in constituents
                    )
                    + " 的新收款，需同时勾选这些行",
                    severity="warning",
                ))
            if item["new_receipts"]:
                constituents.append(item["report_month"])

        self._precheck_monotonic(
            contract=contract,
            items=items,
            snapshots=snapshots,
            hard_issues=hard_issues,
        )

        # 被阻断月份之后的行一律随之阻断（D-16 09-07 二次补充 #13）：累计是逐月累加的，
        # 某个月对不上（不可核实 / 单调性倒退 / 作废 …）之后的每个月都经过它——不论
        # 那个月有没有本文件的新收款，后面的 create / update / record_receipts 都不能
        # 单独成立，预览即随之阻断，不留到应用时才以"缺少构成月"拒绝。
        blocked_months: list[str] = []
        for item in items:
            if item["action"] == "conflict":
                blocked_months.append(item["report_month"])
                continue
            if item["action"] not in {"create", "update", "record_receipts"}:
                continue
            if not blocked_months:
                continue
            issue = _row_issue(
                item["row_no"],
                "constituent_blocked",
                f"{contract.contract_no} "
                f"{date.fromisoformat(item['report_month']):%Y-%m} 的累计经过被阻断月份 "
                + "、".join(f"{date.fromisoformat(v):%Y-%m}" for v in blocked_months)
                + "，随之阻断；请先处理被阻断月份",
            )
            item["action"] = "conflict"
            # 经过被阻断月份的累计值不成立，不再展示、也不能被默认勾选。
            item["new_cumulative_amount"] = None
            item["issues"].append(issue)
            hard_issues.append(issue)
            blocked_months.append(item["report_month"])
        return items, state

    @staticmethod
    def _precheck_monotonic(
        *,
        contract: Any,
        items: list[dict],
        snapshots: list[Any],
        hard_issues: list[dict],
    ) -> None:
        """预览期复算 create_collection/update_collection 的单调性守卫。

        以「本文件全部行都应用后」的状态对照：受影响月份取拟写入值，其余月份取
        既有已确认值。倒退在预览里就是 blocked 行，而不是应用时整批 422。
        另对依赖同批 update 行的行给出警告并记入 ``depends_on_months``：
        * 更早的覆盖月（漂移 / 级联），**方向无关**——本行累计经过那个月按台账
          推导的值，只勾本行会写出与人确认的现值互相矛盾的快照
          （create / update / record_receipts 三种行都算）；
        * 更晚的覆盖月，只在不勾它守卫必拒时。
        应用期按该字段硬拒，不再让批次以 422 收场。
        """

        affected = {date.fromisoformat(item["report_month"]) for item in items}
        proposed: dict[date, Decimal] = {
            row.report_month: Decimal(row.cumulative_amount)
            for row in snapshots
            if row.status == "confirmed" and row.report_month not in affected
        }
        current_updates: dict[date, Decimal] = {}
        for item in items:
            month = date.fromisoformat(item["report_month"])
            if item["action"] in {"create", "update", "record_receipts", "noop"}:
                proposed[month] = Decimal(item["new_cumulative_amount"])
            elif (
                item.get("expected_current_amount") is not None
                and item.get("expected_current_status", "confirmed") == "confirmed"
            ):
                proposed[month] = Decimal(item["expected_current_amount"])
            if (
                item["action"] == "update"
                and item.get("expected_current_status") == "confirmed"
            ):
                current_updates[month] = Decimal(item["expected_current_amount"])

        for item in items:
            if item["action"] not in {"create", "update", "record_receipts"}:
                continue
            month = date.fromisoformat(item["report_month"])
            value = Decimal(item["new_cumulative_amount"])
            if item["action"] != "record_receipts":
                message = None
                for other_month, other_value in proposed.items():
                    if other_month < month and value < other_value:
                        message = "已确认累计回款不得低于更早月份的已确认累计回款"
                        break
                    if other_month > month and value > other_value:
                        message = "已确认累计回款不得高于更晚月份的已确认累计回款"
                        break
                if message is not None:
                    issue = _row_issue(
                        item["row_no"],
                        "collection_not_monotonic",
                        f"{contract.contract_no} {month:%Y-%m}：{message}",
                    )
                    item["action"] = "conflict"
                    item.setdefault("issues", []).append(issue)
                    hard_issues.append(issue)
                    continue
            depends = sorted(
                other_month
                for other_month, other_value in current_updates.items()
                if other_month != month
                and (
                    other_month < month
                    or (item["action"] != "record_receipts" and value > other_value)
                )
            )
            item["depends_on_months"] = [other.isoformat() for other in depends]
            if depends:
                item.setdefault("issues", []).append(_row_issue(
                    item["row_no"],
                    "depends_on_update",
                    "需同时勾选 "
                    + "、".join(f"{other:%Y-%m}" for other in depends)
                    + " 的覆盖行：本行累计经过这些月份按台账推导的覆盖值，"
                    "只勾本行会与现有已确认值互相矛盾或被单调性校验拒绝",
                    severity="warning",
                ))

    def apply_plan(
        self,
        db: Session,
        plan: dict,
        *,
        operated_by: str,
        audit_reason: str,
        provenance: dict | None = None,
    ) -> dict:
        actionable = {"create", "update", "record_receipts", "noop"}
        selected = [item for item in plan["operations"] if item["action"] in actionable]
        target_contracts = sorted({item["project_contract_id"] for item in selected})
        selected_months: dict[str, set[str]] = defaultdict(set)
        for item in selected:
            if item["action"] in {"create", "update", "record_receipts"}:
                selected_months[item["project_contract_id"]].add(item["report_month"])
        for item in selected:
            if item["action"] not in {"create", "update", "record_receipts"}:
                continue
            months = selected_months[item["project_contract_id"]]
            month_text = f"{date.fromisoformat(item['report_month']):%Y-%m}"
            missing = [
                value for value in item.get("requires_months") or []
                if value not in months
            ]
            if missing:
                raise BulkImportInvalid(
                    f"{item['normalized_order_no']} {month_text} 的累计包含更早月份 "
                    + "、".join(f"{date.fromisoformat(v):%Y-%m}" for v in missing)
                    + " 的新收款，必须同时勾选这些行"
                )
            # 预检已判定：不勾这些覆盖行，本行会与现值矛盾或被单调性守卫拒绝。
            # 硬拒在任何写入之前，而不是让守卫在事务中途 422、批次落成 failed。
            missing_updates = [
                value for value in item.get("depends_on_months") or []
                if value not in months
            ]
            if missing_updates:
                raise BulkImportInvalid(
                    f"{item['normalized_order_no']} {month_text} 需同时勾选 "
                    + "、".join(f"{date.fromisoformat(v):%Y-%m}" for v in missing_updates)
                    + " 的覆盖行，否则会与现有已确认值矛盾或被单调性校验拒绝"
                )
        # 台账唯一键是 DB 兜底；先按合同串行化，避免并发同单入账撞唯一约束变 500。
        for project_contract_id in target_contracts:
            _advisory_lock(db, f"maintenance-receipt-ledger:{project_contract_id}")
        # 再按项目取工作簿并发状态锁（与 update_collection / create_collection / 05 表
        # 同一把、同一顺序），**然后**才读快照与台账：否则读完到第一次写入之间，
        # 走正规入口的并发改值能在窗口里提交，逐项 CAS 与指纹复算全部按旧值通过。
        operations.lock_workbook_states(
            db, project_ids=[item["project_id"] for item in selected]
        )
        contracts = {
            row.project_contract_id: row for row in db.scalars(
                select(MaintenanceProjectContract).where(
                    MaintenanceProjectContract.project_contract_id.in_(target_contracts or [""])
                )
            )
        }
        snapshots = {
            (row.project_contract_id, row.report_month): row for row in db.scalars(
                select(MaintenanceCollectionSnapshot).where(
                    MaintenanceCollectionSnapshot.project_contract_id.in_(target_contracts or [""])
                )
            )
        }
        norms = sorted({item["normalized_order_no"] for item in selected})
        ledger_active_by_norm: dict[str, list[MaintenanceCollectionReceipt]] = defaultdict(list)
        for row in _ledger_receipts(db, norms):
            if row.is_active:
                ledger_active_by_norm[row.contract_no].append(row)
        ledger_keys = {
            (row.contract_no, row.receipt_no)
            for rows in ledger_active_by_norm.values()
            for row in rows
        }
        for item in selected:
            contract = contracts.get(item["project_contract_id"])
            if contract is None or contract.version != item["expected_contract_version"]:
                raise BulkImportConflict(
                    "预览后的项目合同关系已变化，请重新预览", code="stale_preview"
                )
            month = date.fromisoformat(item["report_month"])
            current = snapshots.get((contract.project_contract_id, month))
            if item["action"] == "create":
                if current is not None:
                    raise BulkImportConflict(
                        "预览后同合同同月份已新增回款快照，请重新预览",
                        code="stale_preview",
                    )
            else:
                if (
                    current is None
                    or current.collection_id != item["expected_collection_id"]
                    or current.version != item["expected_collection_version"]
                    or _jsonable(current.cumulative_amount)
                    != item["expected_current_amount"]
                    or (
                        item["action"] != "update"
                        and current.status != "confirmed"
                    )
                ):
                    raise BulkImportConflict(
                        "预览后的既有回款快照已变化，请重新预览", code="stale_preview"
                    )
            for receipt in item.get("new_receipts") or []:
                if (item["normalized_order_no"], receipt["receipt_no"]) in ledger_keys:
                    raise BulkImportConflict(
                        f"预览后收款单台账已变化：{receipt['receipt_no']} 已入账，请重新预览",
                        code="stale_preview",
                    )

        # 合同状态 CAS：预览冻结的累计值只在「台账生效行 + 基线 + 全部快照」与
        # 预览时完全一致的前提下才允许落库。逐月的版本 CAS 只看被写的月份，盖不住
        # 累计所依赖的基线月与台账本身（预览→应用之间他人入账 / 修正基线 / 并行预览）。
        contract_states = plan.get("contract_states") or {}
        for project_contract_id in target_contracts:
            state = contract_states.get(project_contract_id)
            if not state or not state.get("fingerprint"):
                raise BulkImportConflict(STALE_PREVIEW_MESSAGE, code="stale_preview")
            norm = str(state["contract_no"])
            contract_snapshots = [
                row for (relation_id, _month), row in snapshots.items()
                if relation_id == project_contract_id
            ]
            coverage_start = date.fromisoformat(str(state["coverage_start"]))
            contract_items = [
                item for item in selected if item["project_contract_id"] == project_contract_id
            ]
            series = _cumulative_series(
                new_receipts=[
                    {
                        "receipt_date": date.fromisoformat(receipt["receipt_date"]),
                        "actual": Decimal(receipt["actual_amount"]),
                    }
                    for item in contract_items
                    for receipt in item.get("new_receipts") or []
                ],
                ledger_active=ledger_active_by_norm.get(norm, []),
                snapshots=contract_snapshots,
                coverage_start=coverage_start,
            )
            current_fingerprint = _contract_fingerprint(
                ledger_active=ledger_active_by_norm.get(norm, []),
                snapshots=contract_snapshots,
                baseline_row=series["baseline_row"],
            )
            if not hmac.compare_digest(current_fingerprint, str(state["fingerprint"])):
                raise BulkImportConflict(STALE_PREVIEW_MESSAGE, code="stale_preview")
            # 指纹之外再复算一遍：冻结的累计值必须能从加锁后的库态 + 本批新收款重现。
            for item in contract_items:
                if item["action"] not in {"create", "update"}:
                    continue
                month = date.fromisoformat(item["report_month"])
                derived = series["cumulative"].get(month)
                if derived is None or _jsonable(derived) != item["new_cumulative_amount"]:
                    raise BulkImportConflict(
                        f"{norm} {month:%Y-%m} 累计复算 {_jsonable(derived)} 与预览冻结值 "
                        f"{item['new_cumulative_amount']} 不一致；{STALE_PREVIEW_MESSAGE}",
                        code="stale_preview",
                    )

        batch_id = (provenance or {}).get("batch_id")
        source_sha256 = str(
            (provenance or {}).get("source_sha256") or plan.get("file_hash") or ""
        )
        snapshot_source = "bulk_import" if batch_id is not None else "direct_api"
        snapshot_batch = str(batch_id) if batch_id is not None else None

        def _record_receipts(item: dict, contract: MaintenanceProjectContract) -> int:
            count = 0
            for receipt in item.get("new_receipts") or []:
                db.add(MaintenanceCollectionReceipt(
                    project_contract_id=contract.project_contract_id,
                    contract_no=item["normalized_order_no"],
                    receipt_no=receipt["receipt_no"],
                    receipt_date=date.fromisoformat(receipt["receipt_date"]),
                    actual_amount=Decimal(receipt["actual_amount"]),
                    remark=receipt.get("remark"),
                    import_batch_id=batch_id,
                    source_sha256=source_sha256,
                    is_active=True,
                    created_by=operated_by,
                ))
                count += 1
            return count

        # 写入顺序服从单调性守卫（逐行对照库内现值）：先把要"降"的月份按月升序
        # 更新，再把新增/要"升"的月份按月降序写入。最终状态单调时这个顺序保证
        # 每一步都通过守卫（升序写"升"会撞上尚未抬高的更晚月份）。
        def _month(item: dict) -> date:
            return date.fromisoformat(item["report_month"])

        decreases = sorted(
            (
                item for item in selected
                if item["action"] == "update"
                and Decimal(item["new_cumulative_amount"])
                < Decimal(item["expected_current_amount"])
            ),
            key=_month,
        )
        increases = sorted(
            (
                item for item in selected
                if item["action"] == "create"
                or (
                    item["action"] == "update"
                    and Decimal(item["new_cumulative_amount"])
                    >= Decimal(item["expected_current_amount"])
                )
            ),
            key=_month,
            reverse=True,
        )
        ledger_only = [item for item in selected if item["action"] == "record_receipts"]

        writes = 0
        updates = 0
        noops = sum(1 for item in selected if item["action"] == "noop")
        receipts_recorded = 0
        details: dict[str, dict] = {}
        for item in [*decreases, *increases, *ledger_only]:
            contract = contracts[item["project_contract_id"]]
            month = _month(item)
            key = f"{item['project_contract_id']}:{item['report_month']}"
            recorded = _record_receipts(item, contract)
            receipts_recorded += recorded
            db.flush()
            if item["action"] == "record_receipts":
                current = snapshots[(contract.project_contract_id, month)]
                details[key] = {
                    "status": "applied",
                    "message": (
                        f"{contract.contract_no} {month:%Y-%m} 累计不变"
                        f"（{item['expected_current_amount']}），登记 {recorded} 笔收款入台账"
                    ),
                    "entity_id": current.collection_id,
                    "before_version": current.version,
                    "after_version": current.version,
                    "receipts_recorded": recorded,
                }
                continue
            if item["action"] == "create":
                payload = operations.create_collection(
                    db,
                    project_id=item["project_id"],
                    project_contract_id=item["project_contract_id"],
                    report_month=month,
                    cumulative_amount=Decimal(item["new_cumulative_amount"]),
                    status="confirmed",
                    receipt_reference=item["receipt_reference"],
                    remark=None,
                    reason=audit_reason,
                    operated_by=operated_by,
                    source=snapshot_source,
                    import_batch_id=snapshot_batch,
                )
                if payload is None:
                    raise BulkImportConflict("项目在应用期间消失")
                writes += 1
                details[key] = {
                    "status": "applied",
                    "message": (
                        f"{contract.contract_no} {month:%Y-%m} 新建已确认累计 "
                        f"{item['new_cumulative_amount']}，登记 {recorded} 笔收款入台账"
                    ),
                    "entity_id": payload["collection_id"],
                    "before_version": None,
                    "after_version": payload["version"],
                    "receipts_recorded": recorded,
                }
                continue
            previous_stamp = (
                f"{item.get('previous_source') or '-'}"
                f"/批次 {item.get('previous_import_batch_id') or '-'}"
                f"/{_stamp_text(item.get('previous_updated_at'))}"
            )
            previous_operator = item.get("previous_operated_by") or "-"
            reason = (
                f"{audit_reason}：覆盖 {contract.contract_no} {month:%Y-%m} "
                f"已确认累计 {item['expected_current_amount']}→{item['new_cumulative_amount']}"
                f"（原来源 {previous_stamp}，原操作人 {previous_operator}，操作人 {operated_by}）"
            )
            payload = operations.update_collection(
                db,
                collection_id=item["expected_collection_id"],
                version=int(item["expected_collection_version"]),
                updates={
                    "cumulative_amount": Decimal(item["new_cumulative_amount"]),
                    "receipt_reference": item["receipt_reference"],
                    "status": "confirmed",
                },
                reason=reason,
                operated_by=operated_by,
                source=snapshot_source,
                import_batch_id=snapshot_batch,
            )
            if payload is None:
                raise BulkImportConflict("回款快照在应用期间消失")
            updates += 1
            details[key] = {
                "status": "applied",
                "message": (
                    f"{contract.contract_no} {month:%Y-%m} 已覆盖：原值 "
                    f"{item['expected_current_amount']} → 新值 {item['new_cumulative_amount']}"
                    f"（原来源 {previous_stamp}，原操作人 {previous_operator}），"
                    f"登记 {recorded} 笔收款入台账"
                ),
                "entity_id": payload["collection_id"],
                "before_version": item["expected_collection_version"],
                "after_version": payload["version"],
                "before_amount": item["expected_current_amount"],
                "after_amount": item["new_cumulative_amount"],
                "previous_source": item.get("previous_source"),
                "previous_import_batch_id": item.get("previous_import_batch_id"),
                "previous_updated_at": item.get("previous_updated_at"),
                "previous_operated_by": item.get("previous_operated_by"),
                "receipts_recorded": recorded,
            }
        return {
            "written": writes,
            "updated": updates,
            "noop": noops,
            "receipts_recorded": receipts_recorded,
            "operation": "collection_snapshot_upsert",
            "project_ids": sorted({item["project_id"] for item in selected}),
            "details": details,
        }


def _month_start(day: date) -> date:
    return date(day.year, day.month, 1)


def _month_end(month: date) -> date:
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _stamp_text(value: Any) -> str:
    if not value:
        return "-"
    text = str(value)
    return text[:16].replace("T", " ")


def _ledger_stamp(row: Any) -> str:
    created = getattr(row, "created_at", None)
    return _stamp_text(created.isoformat() if isinstance(created, datetime) else created)


def _ledger_receipts(db: Session, contract_nos: list[str]) -> list[MaintenanceCollectionReceipt]:
    """收款单台账按（归一化）销售订单号取行——含已作废行，幂等判定看全部，累计只看生效。"""

    if not contract_nos:
        return []
    return list(db.scalars(
        select(MaintenanceCollectionReceipt)
        .where(MaintenanceCollectionReceipt.contract_no.in_(contract_nos))
        .order_by(
            MaintenanceCollectionReceipt.contract_no,
            MaintenanceCollectionReceipt.receipt_date,
            MaintenanceCollectionReceipt.id,
        )
    ))


def _existing_snapshots(db: Session, contract_ids: list[str]) -> list[MaintenanceCollectionSnapshot]:
    if not contract_ids:
        return []
    return list(db.scalars(
        select(MaintenanceCollectionSnapshot)
        .where(MaintenanceCollectionSnapshot.project_contract_id.in_(contract_ids))
        .order_by(
            MaintenanceCollectionSnapshot.project_contract_id,
            MaintenanceCollectionSnapshot.report_month,
        )
    ))


_HISTORY_STATUSES = frozenset({"confirmed", "unconfirmed"})


def _is_import_row(row: Any) -> bool:
    """台账导入行（带批次）；裁决更正行无批次。"""

    return getattr(row, "import_batch_id", None) is not None


def _baseline_row(snapshots: list[Any], coverage_start: date) -> Any:
    """基线 = 覆盖起点之前最晚一条**已确认、非 bulk_import** 快照。

    台账推导出来的快照（bulk_import）永远重新推导、不作基线——否则裁决把收款移出
    该月后，它会带着已移走的收款当基线，下一次预览把同一笔算两次。未确认快照
    不是事实，同样不作基线。
    """

    return max(
        (
            row for row in snapshots
            if row.status == "confirmed"
            and row.source != "bulk_import"
            and row.report_month < coverage_start
        ),
        key=lambda row: row.report_month,
        default=None,
    )


def _cumulative_series(
    *,
    new_receipts: list[dict],
    ledger_active: list[Any],
    snapshots: list[Any],
    coverage_start: date | None = None,
    ledger_history: list[Any] | None = None,
) -> dict:
    """月度累计推导的唯一实现：预览规划、应用复算、裁决影响面三处共用。

    ``new_receipts`` 项形如 ``{"receipt_date": date, "actual": Decimal}``；
    ``ledger_active`` 只应传生效行，``ledger_history`` 传该合同全部台账行（含被裁决
    取代的历史行）。返回 ``coverage_start`` / ``baseline_row`` / ``baseline_amount`` /
    ``months``（升序候选月）/ ``cumulative``（月 → Decimal）。

    覆盖起点（D-16 09-07 二次补充 #12）= 下列各月的最早者：
    * 台账导入行的月份——**含被裁决取代的历史行**：裁决把收款移到别的月份后，
      原月份仍要按台账重新对账（否则原月份掉出覆盖范围，它的快照反而成了基线，
      移走的那笔被算两次）；被再次裁决取代的裁决行不算（它不是任何一次导出的证据）；
    * 台账生效行的月份（含裁决更正行）与本文件月份；
    * ``source='bulk_import'`` 快照的月份（台账推导出来的快照永远重新推导）；
    * 基线之后的未确认快照月份（未确认历史只能对账，不能当基线）。
    """

    months_new = {_month_start(row["receipt_date"]) for row in new_receipts}
    months_ledger = {_month_start(row.receipt_date) for row in ledger_active}
    if coverage_start is None:
        anchors = months_new | months_ledger
        anchors |= {
            _month_start(row.receipt_date)
            for row in ledger_history or []
            if _is_import_row(row)
        }
        anchors |= {
            row.report_month for row in snapshots
            if row.source == "bulk_import" and row.status != "void"
        }
        coverage_start = min(anchors)
        baseline = _baseline_row(snapshots, coverage_start)
        unsettled = [
            row.report_month for row in snapshots
            if row.status == "unconfirmed"
            and row.report_month < coverage_start
            and (baseline is None or row.report_month > baseline.report_month)
        ]
        if unsettled:
            coverage_start = min(unsettled)
    baseline_row = _baseline_row(snapshots, coverage_start)
    baseline_amount = (
        Decimal(baseline_row.cumulative_amount) if baseline_row is not None
        else Decimal("0.00")
    )
    candidate = (
        months_new
        | months_ledger
        | {row.report_month for row in snapshots if row.report_month >= coverage_start}
    )
    cumulative: dict[date, Decimal] = {}
    for month in sorted(candidate):
        month_end = _month_end(month)
        cumulative[month] = (
            baseline_amount
            + sum(
                (Decimal(row.actual_amount) for row in ledger_active
                 if row.receipt_date <= month_end),
                Decimal("0.00"),
            )
            + sum(
                (Decimal(row["actual"]) for row in new_receipts
                 if row["receipt_date"] <= month_end),
                Decimal("0.00"),
            )
        ).quantize(Decimal("0.01"))
    return {
        "coverage_start": coverage_start,
        "baseline_row": baseline_row,
        "baseline_amount": baseline_amount,
        "months": sorted(candidate),
        "cumulative": cumulative,
    }


def _ledger_snapshot_drift(
    *,
    ledger_active: list[Any],
    snapshots: list[Any],
    ledger_history: list[Any] | None = None,
) -> bool:
    """台账覆盖范围内是否有历史快照（已确认 / 未确认）与纯台账推导值不等
    （裁决 / 手工改值留下的漂移）。"""

    if not ledger_active:
        return False
    series = _cumulative_series(
        new_receipts=[],
        ledger_active=ledger_active,
        snapshots=snapshots,
        ledger_history=ledger_history,
    )
    return any(
        row.status in _HISTORY_STATUSES
        and row.report_month in series["cumulative"]
        and Decimal(row.cumulative_amount) != series["cumulative"][row.report_month]
        for row in snapshots
    )


def _contract_fingerprint(
    *,
    ledger_active: list[Any],
    snapshots: list[Any],
    baseline_row: Any,
) -> str:
    """合同状态指纹：台账生效行 + 基线 + 该合同全部快照（月/版本/状态/金额）。

    预览时算一次冻结进计划，应用时加锁后按库内现状重算；不等即 stale_preview。
    金额一并纳入：绕过 update_collection 的直接改值不递增版本，指纹仍要抓到。
    """

    return _canonical_hash({
        "ledger": sorted(
            (row.receipt_no, row.receipt_date.isoformat(), _jsonable(Decimal(row.actual_amount)))
            for row in ledger_active
        ),
        "baseline": (
            [baseline_row.collection_id, int(baseline_row.version)]
            if baseline_row is not None
            else None
        ),
        "snapshots": sorted(
            (
                row.report_month.isoformat(),
                int(row.version),
                str(row.status),
                _jsonable(Decimal(row.cumulative_amount)),
            )
            for row in snapshots
        ),
    })


def _snapshot_operators(db: Session, snapshots: list[Any]) -> dict[str, str]:
    """既有快照最近一次操作人（覆盖回执要点名「覆盖了谁的值」）。

    取 collection 事实审计最新一条的 operated_by；从未进过审计的快照（旧 05 表 /
    直接建库）退回来源批次 ``sys_import_batch.uploaded_by``；都没有则缺省。
    """

    collection_ids = [row.collection_id for row in snapshots]
    if not collection_ids:
        return {}
    operators: dict[str, str] = {}
    audit_rows = db.execute(
        select(
            MaintenanceProjectOperationAudit.entity_id,
            MaintenanceProjectOperationAudit.operated_by,
        )
        .where(
            MaintenanceProjectOperationAudit.entity_type == "collection",
            MaintenanceProjectOperationAudit.entity_id.in_(collection_ids),
        )
        .order_by(
            MaintenanceProjectOperationAudit.entity_id,
            MaintenanceProjectOperationAudit.operated_at.desc(),
            MaintenanceProjectOperationAudit.id.desc(),
        )
    ).all()
    for entity_id, operated_by in audit_rows:
        operators.setdefault(str(entity_id), str(operated_by))
    batch_ids = {
        int(row.import_batch_id)
        for row in snapshots
        if row.collection_id not in operators
        and row.import_batch_id
        and str(row.import_batch_id).isdigit()
    }
    if batch_ids:
        uploaded_by = {
            int(batch_id): uploaded
            for batch_id, uploaded in db.execute(
                select(SysImportBatch.id, SysImportBatch.uploaded_by).where(
                    SysImportBatch.id.in_(sorted(batch_ids))
                )
            ).all()
        }
        for row in snapshots:
            if row.collection_id in operators or not row.import_batch_id:
                continue
            if str(row.import_batch_id).isdigit():
                uploaded = uploaded_by.get(int(row.import_batch_id))
                if uploaded:
                    operators[row.collection_id] = str(uploaded)
    return operators


def _finish_plan(
    sheet: DetectedSheet,
    rows: list[dict],
    operations_plan: list[dict],
    hard_issues: list[dict],
    *,
    extra: dict,
) -> dict:
    actions: dict[str, int] = defaultdict(int)
    for row in rows:
        actions[row.get("action", "unknown")] += 1
    op_actions: dict[str, int] = defaultdict(int)
    for row in operations_plan:
        op_actions[row.get("action", "unknown")] += 1
    return _jsonable({
        "protocol_version": PROTOCOL_VERSION,
        "sheet": sheet.name,
        "header_row": sheet.header_row,
        "header_rows": list(sheet.header_rows),
        "headers": list(sheet.headers),
        "system_headers": list(sheet.system_headers),
        "field_matches": sheet.field_matches,
        "rows": rows,
        "operations": operations_plan,
        "issues": hard_issues,
        "summary": {
            "source_rows": len(rows),
            "source_actions": dict(actions),
            "target_operations": len(operations_plan),
            "operation_actions": dict(op_actions),
            "blocking_errors": len(hard_issues),
        },
        **extra,
    })


register_adapter(SalesContractAmountAdapter())
register_adapter(ReceiptCollectionAdapter())


def _synthetic_cell(value: Any) -> Any:
    """把 pandas 空值还原成原始表格的空单元格。

    合成 DetectedSheet 必须与 openpyxl 直读同形：空单元格是 None 而不是
    NaN/NaT，否则下游会把 'nan' 当成非法日期，把整张销售订单判成阻断错误。
    """

    if value is None:
        return None
    try:
        if value != value:  # NaN / NaT 自身不相等
            return None
    except (TypeError, ValueError):
        return value
    if isinstance(value, str) and not value.strip():
        return None
    return value


def transformed_sales_sheet(
    result,
    *,
    source_columns: list[str],
) -> DetectedSheet:
    """Adapt the already-bounded generic sales transform without reopening XLSX."""

    internal_by_detected = {
        "order_no": "order_no",
        "raw_order_id": "raw_order_id",
        "data_status": "data_status",
        "maintenance_business": "maintenance_business",
        "business_type": "business_type",
        "project_name": "maintenance_project_name",
        "project_manager": "maintenance_project_manager",
        "period_from": "maintenance_period_from",
        "period_to": "maintenance_period_to",
        "order_amount": "amount_inc_tax",
        "tax_flag": "is_tax_inclusive",
        "tax_rate": "tax_rate",
        "tax_amount": "tax_amount",
        "amount_ex_tax": "amount_ex_tax",
    }
    present_internal = {
        mapping.SALES_HEAD[column]
        for column in source_columns
        if column in mapping.SALES_HEAD
    }
    fields = [
        field
        for field, internal in internal_by_detected.items()
        if internal in present_internal
    ]
    field_indexes = {field: index for index, field in enumerate(fields)}
    rows: list[tuple[int, tuple[Any, ...]]] = []
    for row_no, order in enumerate(result.orders.values(), start=3):
        values: list[Any] = []
        for field in fields:
            value = order.get(internal_by_detected[field])
            if field == "tax_flag":
                value = "含税" if value is True else (
                    "不含税" if value is False else None
                )
            values.append(_synthetic_cell(value))
        rows.append((row_no, tuple(values)))
    return DetectedSheet(
        name="generic-sales-transform",
        header_row=2,
        header_rows=(1, 2),
        headers=tuple(fields),
        system_headers=(),
        field_indexes=field_indexes,
        field_matches={},
        rows=tuple(rows),
    )


def sync_uploaded_sales_workbook(
    db: Session,
    data: bytes | None,
    filename: str,
    *,
    operated_by: str,
    import_batch_id: int,
    detected_sheet: DetectedSheet | None = None,
) -> dict:
    """Project explicit maintenance sales rows during the ordinary import.

    The ordinary ETL remains the sales fact writer.  This hook reuses the
    reviewed XSDD planner/apply path, but feeds it only rows whose source
    columns explicitly declare maintenance business.  Non-maintenance rows
    are therefore incapable of creating/updating maintenance contracts.
    """

    # 优先复用 loader 已经解析好的 TransformResult（transformed_sales_sheet），
    # 不再重开 XLSX：_detect 走 read_only=False 会把每个 worksheet 都实体化，
    # 抵消 load_selected_workbook 的内存边界（真实销售导出有 19 个 sheet），
    # 而且可能选到与已入库事实不同的那一张表。同时也消除了「文件已被判定为
    # SALES、但适配器识别失败就静默 not_applicable」这条无声跳过路径
    # （Codex P1 ×2，2026-09-03）。
    adapter = _ADAPTERS["sales_contract_amount"]
    if detected_sheet is not None:
        sheet = detected_sheet
    else:
        # 文件已被判定为 SALES 后，识别失败是硬错误：静默 not_applicable 会让
        # 一份真实销售订单看起来「导入成功但一个项目都没建」。
        adapter, sheet = _detect(data)
        if adapter.key != "sales_contract_amount":
            raise BulkImportInvalid("未识别为销售订单合同事实表")
    written_raw_ids = set(db.scalars(select(FSalesOrder.raw_order_id).where(
        FSalesOrder.import_batch_id == import_batch_id
    )))
    eligible_rows = tuple(
        (row_no, values)
        for row_no, values in sheet.rows
        if _is_explicit_maintenance_row(sheet, values)
        and _text(_value(sheet, values, "raw_order_id")) in written_raw_ids
    )
    if not eligible_rows:
        return {"status": "no_maintenance_rows", "eligible_rows": 0}
    maintenance_sheet = DetectedSheet(
        name=sheet.name,
        header_row=sheet.header_row,
        header_rows=sheet.header_rows,
        headers=sheet.headers,
        system_headers=sheet.system_headers,
        field_indexes=sheet.field_indexes,
        field_matches=sheet.field_matches,
        rows=eligible_rows,
    )
    plan = adapter.build_plan(db, maintenance_sheet)
    blocking = int((plan.get("summary") or {}).get("blocking_errors") or 0)
    if blocking:
        raise BulkImportInvalid(
            f"维保销售订单自动建项有 {blocking} 个阻断错误",
            issues=plan.get("issues") or [],
        )
    result = adapter.apply_plan(
        db,
        plan,
        operated_by=operated_by,
        audit_reason=(
            f"普通销售订单导入自动建维保项目 "
            f"batch={import_batch_id} filename={filename[:128]}"
        ),
    )
    return {
        "status": "applied",
        "eligible_rows": len(eligible_rows),
        "source_actions": (plan.get("summary") or {}).get("source_actions") or {},
        **result,
    }


def build_preview(db: Session, data: bytes, filename: str) -> PreviewArtifact:
    if not filename.lower().endswith(".xlsx"):
        raise BulkImportInvalid("只接受 .xlsx 文件")
    file_hash = hashlib.sha256(data).hexdigest()
    adapter, sheet = _detect(data)
    plan = adapter.build_plan(db, sheet)
    plan.update({
        "form_type": adapter.key,
        "file_type": adapter.file_type,
        "filename": filename,
        "file_hash": file_hash,
    })
    return PreviewArtifact(
        adapter_key=adapter.key,
        file_type=adapter.file_type,
        file_hash=file_hash,
        filename=filename,
        plan=_jsonable(plan),
    )


def _preview_response(batch: SysImportBatch, token: str | None) -> dict:
    report = batch.report_json or {}
    plan = report.get("plan") or {}
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "form_type": report.get("form_type"),
        "file_hash": batch.file_hash,
        "plan_hash": report.get("plan_hash"),
        "commit_token": token,
        "summary": plan.get("summary") or {},
        "rows": plan.get("rows") or [],
        "operations": plan.get("operations") or [],
        "issues": plan.get("issues") or [],
        "result": report.get("result"),
    }


def store_preview(
    db: Session,
    artifact: PreviewArtifact,
    *,
    operated_by: str,
    operation_key: str,
) -> dict:
    operation_hash = _operation_hash(operated_by, operation_key)
    _advisory_lock(db, f"maintenance-bulk-preview:{operation_hash}")
    existing = db.scalar(
        select(SysImportBatch)
        .where(
            SysImportBatch.uploaded_by == operated_by,
            SysImportBatch.file_type.in_(SUPPORTED_BATCH_TYPES),
            SysImportBatch.report_json["operation_key_hash"].as_string()
            == operation_hash,
        )
        .order_by(SysImportBatch.id.desc())
        .limit(1)
    )
    if existing is not None:
        if existing.file_hash != artifact.file_hash or existing.file_type != artifact.file_type:
            raise BulkImportConflict("同一 Idempotency-Key 已用于另一份文件")
        if existing.status == "success":
            return _preview_response(existing, None)
        token = secrets.token_urlsafe(32)
        report = dict(existing.report_json or {})
        report["token_hash"] = _token_hash(existing.id, token)
        report["token_rotated_at"] = datetime.now(timezone.utc).isoformat()
        existing.report_json = report
        db.commit()
        return _preview_response(existing, token)

    batch = SysImportBatch(
        filename=artifact.filename,
        file_type=artifact.file_type,
        file_hash=artifact.file_hash,
        uploaded_by=operated_by,
        rows_total=int(artifact.plan["summary"]["source_rows"]),
        rows_inserted=0,
        rows_skipped=sum(
            int(artifact.plan["summary"]["source_actions"].get(key, 0))
            for key in ("noop", "unmatched", "duplicate")
        ),
        rows_error=int(artifact.plan["summary"]["blocking_errors"]),
        status="processing",
    )
    db.add(batch)
    db.flush()
    token = secrets.token_urlsafe(32)
    plan_hash = _canonical_hash(artifact.plan)
    batch.report_json = {
        "protocol_version": PROTOCOL_VERSION,
        "form_type": artifact.adapter_key,
        "operation_key_hash": operation_hash,
        "plan_hash": plan_hash,
        "token_hash": _token_hash(batch.id, token),
        "plan": artifact.plan,
        "previewed_at": datetime.now(timezone.utc).isoformat(),
    }
    for issue in artifact.plan.get("issues") or []:
        db.add(SysImportError(
            batch_id=batch.id,
            row_no=issue.get("row_no"),
            error_type=str(issue.get("code") or "bulk_import")[:32],
            error_detail=str(issue.get("message") or "")[:4000],
            raw_row=issue,
        ))
    db.commit()
    return _preview_response(batch, token)


def preview(
    db: Session,
    data: bytes,
    filename: str,
    *,
    operated_by: str,
    operation_key: str,
) -> dict:
    return store_preview(
        db,
        build_preview(db, data, filename),
        operated_by=operated_by,
        operation_key=operation_key,
    )


def get_batch(db: Session, batch_id: int, *, operated_by: str, allow_admin: bool = False) -> dict:
    batch = db.get(SysImportBatch, batch_id)
    if batch is None or batch.file_type not in SUPPORTED_BATCH_TYPES:
        raise BulkImportNotFound("批量导入批次不存在")
    if batch.uploaded_by != operated_by and not allow_admin:
        raise BulkImportNotFound("批量导入批次不存在")
    return _preview_response(batch, None)


def apply_preview(
    db: Session,
    batch_id: int,
    *,
    commit_token: str,
    plan_hash: str,
    operated_by: str,
    allow_admin: bool = False,
) -> dict:
    batch = db.scalar(
        select(SysImportBatch)
        .where(SysImportBatch.id == batch_id)
        .with_for_update()
    )
    if batch is None or batch.file_type not in SUPPORTED_BATCH_TYPES:
        raise BulkImportNotFound("批量导入批次不存在")
    if batch.uploaded_by != operated_by and not allow_admin:
        raise BulkImportNotFound("批量导入批次不存在")
    report = dict(batch.report_json or {})
    if not hmac.compare_digest(
        str(report.get("token_hash") or ""), _token_hash(batch.id, commit_token)
    ):
        raise BulkImportConflict("提交 token 无效或已轮换")
    if not hmac.compare_digest(str(report.get("plan_hash") or ""), plan_hash):
        raise BulkImportConflict("预览计划 hash 不匹配")
    if batch.status == "success":
        return {"batch_id": batch.id, "status": "success", **(report.get("result") or {})}
    plan = report.get("plan") or {}
    if _canonical_hash(plan) != plan_hash:
        raise BulkImportConflict("服务器保存的预览计划校验失败")
    if int((plan.get("summary") or {}).get("blocking_errors") or 0) > 0:
        raise BulkImportInvalid("预览包含阻断错误，不能提交", issues=plan.get("issues") or [])

    _advisory_lock(db, f"maintenance-bulk-apply:{batch.file_type}:{batch.file_hash}")
    already = db.scalar(
        select(SysImportBatch)
        .where(
            SysImportBatch.id != batch.id,
            SysImportBatch.file_type == batch.file_type,
            SysImportBatch.file_hash == batch.file_hash,
            SysImportBatch.status == "success",
        )
        .order_by(SysImportBatch.id)
        .limit(1)
    )
    if already is not None:
        prior = (already.report_json or {}).get("result") or {}
        report["result"] = {**prior, "duplicate_of_batch_id": already.id}
        report["applied_at"] = datetime.now(timezone.utc).isoformat()
        report["applied_by"] = operated_by
        batch.report_json = report
        batch.status = "duplicate"
        db.commit()
        return {"batch_id": batch.id, "status": "duplicate", **report["result"]}

    adapter = _ADAPTERS.get(str(report.get("form_type") or ""))
    if adapter is None or adapter.file_type != batch.file_type:
        raise BulkImportConflict("批次适配器不存在或协议已变化")
    audit_reason = (
        f"全项目批量导入 batch={batch.id} form={adapter.key} "
        f"sha256={batch.file_hash}"
    )
    result = adapter.apply_plan(
        db,
        plan,
        operated_by=operated_by,
        audit_reason=audit_reason,
        provenance={"batch_id": batch.id, "source_sha256": batch.file_hash},
    )
    result = _jsonable({
        **result,
        "form_type": adapter.key,
        "file_hash": batch.file_hash,
        "plan_hash": plan_hash,
    })
    report["result"] = result
    report["applied_at"] = datetime.now(timezone.utc).isoformat()
    report["applied_by"] = operated_by
    batch.report_json = report
    batch.status = "success"
    batch.rows_inserted = int(result.get("written") or 0)
    batch.rows_skipped = int(result.get("noop") or 0) + int(
        (plan.get("summary") or {}).get("source_actions", {}).get("unmatched", 0)
    )
    batch.rows_error = 0
    db.add(SysAuditLog(
        entity_type="maintenance_bulk_import",
        entity_id=batch.id,
        action="apply",
        before_json={"status": "processing", "plan_hash": plan_hash},
        after_json={"status": "success", **result},
        reason=audit_reason,
        operated_by=operated_by,
    ))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"batch_id": batch.id, "status": "success", **result}


# ---------------------------------------------------------------------------
# Public multi-file project-batch-transfer protocol
# ---------------------------------------------------------------------------


def _transfer_kind(adapter_key: str) -> str:
    return {
        "sales_contract_amount": "sales_contract",
        "receipt_collection": "receipt",
    }.get(adapter_key, adapter_key)


def _split_issues(issues: list[dict] | None) -> tuple[list[dict], list[dict]]:
    warnings: list[dict] = []
    errors: list[dict] = []
    for issue in issues or []:
        item = {
            "code": str(issue.get("code") or "bulk_import"),
            "message": str(issue.get("message") or ""),
            "field": issue.get("field"),
        }
        # info（如 ledger_backfill）与 warning 一样不阻断，只作提示。
        (warnings if issue.get("severity") in {"warning", "info"} else errors).append(item)
    return warnings, errors


def _detected_fields(adapter: FormAdapter, sheet: DetectedSheet) -> list[dict]:
    basis = {
        "order_amount": "源订单金额；含税标记为含税时直接作为合同含税额",
        "amount_inc_tax": "含税合同额",
        "amount_ex_tax": "销售事实未税金额",
        "tax_rate": "税率；缺失不按 0%",
        "tax_amount": "仅用于与含税/未税金额交叉校验",
        "gross_amount": "销售收款金额；仅用于 F-G=H 对账",
        "discount_amount": "优惠金额；仅用于 F-G=H 对账",
        "actual_amount": "累计回款的唯一金额来源",
    }
    fields = []
    for field, match in sheet.field_matches.items():
        fields.append(
            {
                "source_column": match["source_column"],
                "canonical_field": field,
                "canonical_label": adapter.aliases[field][0],
                "confidence": match["confidence"],
                "required": field in adapter.required_fields,
                "metric_basis": basis.get(field),
            }
        )
    return fields


def _project_names_for_ids(db: Session, project_ids: set[str]) -> dict[str, str]:
    if not project_ids:
        return {}
    return {
        project_id: display_name
        for project_id, display_name in db.execute(
            select(MaintenanceProject.project_id, MaintenanceProject.display_name)
            .where(MaintenanceProject.project_id.in_(sorted(project_ids)))
        )
    }


def _public_sales_rows(
    *,
    file_id: str,
    filename: str,
    plan_index: int,
    plan: dict,
    project_names_by_id: dict[str, str],
) -> tuple[list[dict], dict[str, dict]]:
    operations_by_row = {
        int(item["row_no"]): (idx, item)
        for idx, item in enumerate(plan.get("operations") or [])
    }
    rows: list[dict] = []
    row_map: dict[str, dict] = {}
    for source in plan.get("rows") or []:
        row_no = int(source["row_no"])
        op_pair = operations_by_row.get(row_no)
        warnings, errors = _split_issues(source.get("issues"))
        op_index: int | None = None
        operation: dict | None = None
        if op_pair is not None:
            op_index, operation = op_pair
        source_action = str(source.get("action") or "error")
        if operation is not None:
            internal_action = operation["action"]
            op_warnings, op_errors = _split_issues(operation.get("issues"))
            warning_codes = {item.get("code") for item in warnings}
            error_codes = {item.get("code") for item in errors}
            warnings.extend(
                item for item in op_warnings if item.get("code") not in warning_codes
            )
            errors.extend(
                item for item in op_errors if item.get("code") not in error_codes
            )
            action = {
                "create_project": "create_project",
                "create_contract": "create_contract",
                "update_contract": "update_contract",
                "noop": "skip",
                "blocked": "block",
            }[internal_action]
            row_status = (
                "blocked"
                if internal_action == "blocked"
                else ("unchanged" if internal_action == "noop" else "ready")
            )
            match_state = "ambiguous" if internal_action == "blocked" else "matched"
            project_id = operation.get("project_id")
            new_project = operation.get("new_project") or {}
            project_name = (
                new_project.get("display_name")
                or project_names_by_id.get(project_id)
            )
            match_strategy = (
                "none"
                if internal_action in {"create_project", "blocked"}
                else (
                    "candidate"
                    if internal_action == "create_contract"
                    else "exact_contract_no"
                )
            )
            canonical = {
                "sales_order_no": operation["sales_order_no"],
                "contract_amount_inc_tax": operation[
                    "new_contract_amount_inc_tax"
                ],
                "amount_ex_tax": operation["new_sales_amount_ex_tax"],
                "tax_rate": operation["new_sales_tax_rate"],
                "project_name": project_name,
            }
            target_key = f"sales:{operation['normalized_order_no']}"
            before = source.get("before")
            after = source.get("after")
        else:
            action = "block" if errors else "skip"
            row_status = "blocked" if errors else (
                "needs_review" if source_action == "unmatched" else "unchanged"
            )
            match_state = (
                "invalid"
                if errors
                else ("unmatched" if source_action == "unmatched" else "matched")
            )
            project_id = source.get("project_id")
            project_name = project_names_by_id.get(project_id)
            match_strategy = "none"
            canonical = {
                "sales_order_no": source.get("business_key"),
                "normalized_order_no": source.get("normalized_order_no"),
            }
            target_key = None
            before = source.get("before")
            after = source.get("after")
        row_key = _canonical_hash(
            [file_id, plan_index, row_no, target_key or source_action]
        )[:32]
        public = {
            "row_key": row_key,
            "file_id": file_id,
            "filename": filename,
            "detected_sheet": plan.get("sheet"),
            "source_row": row_no,
            "canonical": canonical,
            "normalized_key": source.get("normalized_order_no"),
            "idempotency_key": row_key,
            "matched_project_id": project_id,
            "matched_project_name": project_name,
            "matched_contract_id": (
                operation.get("contract_id") if operation else None
            ),
            "match_strategy": match_strategy,
            "candidate_count": 1 if project_id else 0,
            "candidates": [],
            "match_state": match_state,
            "action": action,
            "row_status": row_status,
            "before": before,
            "after": after,
            "delta": None,
            "warnings": warnings,
            "errors": errors,
            "_target_key": target_key,
        }
        rows.append(public)
        if operation is not None and row_status == "ready" and not errors:
            row_map[row_key] = {
                "plan_index": plan_index,
                "operation_index": op_index,
            }
    return rows, row_map


# 需要作为可见文字（而非 tooltip）呈现的提示码。
_HINT_CODES = frozenset({
    "requires_earlier_months",
    "depends_on_update",
    "seed_required",
    "cumulative_unverifiable",
    "receipt_conflict",
    "receipt_voided_upstream",
    "receipt_known",
})


def _dedupe_issues(issues: list[dict]) -> list[dict]:
    """同一行同一 code 只留第一条（合同级 fail-closed 会把每个月的同名 issue 都挂上来）。"""

    seen: set[str] = set()
    kept: list[dict] = []
    for issue in issues:
        if issue["code"] in seen:
            continue
        seen.add(issue["code"])
        kept.append(issue)
    return kept


def _hint_messages(warnings: list[dict], errors: list[dict]) -> list[str]:
    return [
        issue["message"]
        for issue in [*warnings, *errors]
        if issue["code"] in _HINT_CODES
    ]


def _public_receipt_rows(
    *,
    file_id: str,
    filename: str,
    plan_index: int,
    plan: dict,
    project_names_by_id: dict[str, str],
) -> tuple[list[dict], dict[str, dict]]:
    rows: list[dict] = []
    row_map: dict[str, dict] = {}
    status_warnings = [
        {
            "code": item["code"],
            "message": item["message"],
            "field": None,
        }
        for item in plan.get("source_warnings") or []
    ]
    operation_source_rows: set[int] = set()
    key_by_month: dict[tuple[str, str], str] = {}
    dependency_months: dict[str, tuple[str, list[str]]] = {}
    for op_index, operation in enumerate(plan.get("operations") or []):
        row_no = int(operation["row_no"])
        operation_source_rows.add(row_no)
        warnings, errors = _split_issues(operation.get("issues"))
        warnings = _dedupe_issues([*status_warnings, *warnings])
        errors = _dedupe_issues(errors)
        internal_action = operation["action"]
        # create → 新建快照；update → 覆盖既有已确认累计（前端默认不勾选，需显式
        # 勾选）；record_receipts → 累计不变、只把新收款登记入台账；noop → 无事可做。
        action = {
            "create": "upsert_collection_snapshot",
            "update": "update_collection_snapshot",
            "record_receipts": "record_receipts",
            "noop": "skip",
        }.get(internal_action, "block")
        row_status = (
            "ready"
            if internal_action in {"create", "update", "record_receipts"}
            else ("unchanged" if internal_action == "noop" else "blocked")
        )
        # fail-closed / 冲突行（seed_required、snapshot_voided、cumulative_unverifiable、
        # constituent_blocked、collection_not_monotonic、order_level_fail_closed）是
        # 判定无效，不是候选歧义：match_state 一律 invalid。
        match_state = (
            "matched"
            if internal_action in {"create", "update", "record_receipts", "noop"}
            else "invalid"
        )
        if internal_action == "update":
            previous_stamp = (
                f"{operation.get('previous_source') or '-'}"
                f" / 批次 {operation.get('previous_import_batch_id') or '-'}"
                f" / {_stamp_text(operation.get('previous_updated_at'))}"
            )
            month = date.fromisoformat(operation["report_month"])
            # 文案按快照真实状态：未确认月的覆盖不能写成"已确认累计"。
            current_status_text = (
                "未确认" if operation.get("expected_current_status") == "unconfirmed"
                else "已确认"
            )
            warnings.append(
                {
                    "code": "snapshot_overwrite",
                    "message": (
                        f"将覆盖 {month:%Y-%m} {current_status_text}累计 "
                        f"{operation.get('expected_current_amount')} → "
                        f"{operation['new_cumulative_amount']}（原来源 {previous_stamp}，"
                        f"原操作人 {operation.get('previous_operated_by') or '-'}）"
                    ),
                    "field": "cumulative_received_inc_tax",
                }
            )
        target_key = (
            f"receipt:{operation['project_contract_id']}:"
            f"{operation['report_month']}"
        )
        row_key = _canonical_hash(
            [file_id, plan_index, row_no, target_key]
        )[:32]
        key_by_month[(operation["project_contract_id"], operation["report_month"])] = row_key
        dependency_months[row_key] = (
            operation["project_contract_id"],
            [
                *(operation.get("requires_months") or []),
                *(operation.get("depends_on_months") or []),
            ],
        )
        public = {
            "row_key": row_key,
            "file_id": file_id,
            "filename": filename,
            "detected_sheet": plan.get("sheet"),
            "source_row": row_no,
            "canonical": {
                "sales_order_no": operation["normalized_order_no"],
                "report_month": operation["report_month"],
                "cumulative_received_inc_tax": operation[
                    "new_cumulative_amount"
                ],
                "receipt_reference": operation["receipt_reference"],
            },
            "normalized_key": operation["normalized_order_no"],
            "idempotency_key": row_key,
            "matched_project_id": operation["project_id"],
            "matched_project_name": project_names_by_id.get(
                operation["project_id"]
            ),
            "matched_contract_id": operation["project_contract_id"],
            "match_strategy": "exact_contract_no",
            "candidate_count": 1,
            "candidates": [],
            "match_state": match_state,
            "action": action,
            "row_status": row_status,
            # update 行：默认不勾选，用户必须显式确认覆盖。
            "requires_confirmation": internal_action == "update",
            "before": (
                {
                    "cumulative_amount": operation.get("expected_current_amount"),
                    "status": operation.get("expected_current_status", "confirmed"),
                    "source": operation.get("previous_source"),
                    "import_batch_id": operation.get("previous_import_batch_id"),
                    "updated_at": operation.get("previous_updated_at"),
                    "version": operation.get("expected_collection_version"),
                }
                if operation.get("expected_collection_id")
                else None
            ),
            "after": {
                "cumulative_amount": operation["new_cumulative_amount"],
                "new_receipts": len(operation.get("new_receipts") or []),
                "baseline": operation.get("baseline"),
            },
            "delta": None,
            "warnings": warnings,
            "errors": errors,
            # 必须与本行同批勾选的行（构成月 + 依赖的覆盖行）；下面统一回填 row_key。
            "depends_on_row_keys": [],
            # 需要直接显示（而非 tooltip）的提示文字。
            "hint_messages": _hint_messages(warnings, errors),
            "_target_key": target_key,
        }
        rows.append(public)
        if row_status == "ready" and not errors:
            row_map[row_key] = {
                "plan_index": plan_index,
                "operation_index": op_index,
            }
    for public in rows:
        relation_id, months = dependency_months.get(public["row_key"], (None, []))
        public["depends_on_row_keys"] = list(dict.fromkeys(
            key_by_month[(relation_id, month)]
            for month in months
            if (relation_id, month) in key_by_month
        ))

    # Preserve invalid/unmatched/duplicate source evidence that did not become
    # a monthly target operation.  Matched receipt source lines are represented
    # by their aggregate snapshot above, avoiding double-selection semantics.
    for source in plan.get("rows") or []:
        row_no = int(source["row_no"])
        if source.get("action") == "matched":
            continue
        warnings, errors = _split_issues(source.get("issues"))
        warnings = _dedupe_issues([*status_warnings, *warnings])
        errors = _dedupe_issues(errors)
        source_action = str(source.get("action") or "error")
        match_state = (
            "invalid"
            if errors
            else ("unmatched" if source_action == "unmatched" else "matched")
        )
        row_key = _canonical_hash(
            [file_id, plan_index, row_no, source.get("business_key")]
        )[:32]
        canonical: dict[str, Any] = {
            "receipt_key": source.get("business_key"),
            "sales_order_no": source.get("normalized_order_no"),
        }
        # 解析成功的收款行（已入账 / 台账冲突 / 整单阻断）带本文件的单号、日期、金额；
        # 台账冲突行另给 before = 台账现值，裁决界面按结构化字段对照，不解析提示文字。
        if source.get("receipt_date") is not None:
            canonical.update(
                receipt_no=source.get("receipt_no"),
                receipt_date=source.get("receipt_date"),
                actual_amount=source.get("actual_amount"),
            )
        rows.append(
            {
                "row_key": row_key,
                "file_id": file_id,
                "filename": filename,
                "detected_sheet": plan.get("sheet"),
                "source_row": row_no,
                "canonical": canonical,
                "normalized_key": source.get("normalized_order_no"),
                "idempotency_key": row_key,
                "matched_project_id": source.get("project_id"),
                "matched_project_name": project_names_by_id.get(
                    source.get("project_id")
                ),
                "matched_contract_id": source.get("project_contract_id"),
                "match_strategy": "none",
                "candidate_count": 0,
                "candidates": [],
                "match_state": match_state,
                "action": "block" if errors else "skip",
                "row_status": "blocked" if errors else "unchanged",
                "before": source.get("ledger_receipt"),
                "after": None,
                "delta": None,
                "warnings": warnings,
                "errors": errors,
                "depends_on_row_keys": [],
                "hint_messages": _hint_messages(warnings, errors),
                "_target_key": None,
            }
        )
    return rows, row_map


def _transfer_summary(rows: list[dict]) -> dict:
    return {
        "total": len(rows),
        "matched": sum(row["match_state"] == "matched" for row in rows),
        "ambiguous": sum(row["match_state"] == "ambiguous" for row in rows),
        "unmatched": sum(row["match_state"] == "unmatched" for row in rows),
        "invalid": sum(row["match_state"] == "invalid" for row in rows),
        "ready": sum(row["row_status"] == "ready" for row in rows),
        # D-16 收款单台账：已入账跳过 / 台账冲突 / 需显式勾选的覆盖行
        "known": sum(
            any(issue["code"] == "receipt_known" for issue in row["warnings"])
            for row in rows
        ),
        # 台账冲突含上游作废而台账仍生效（receipt_voided_upstream）：两者都走裁决。
        "receipt_conflicts": sum(
            any(
                issue["code"] in {"receipt_conflict", "receipt_voided_upstream"}
                for issue in row["errors"]
            )
            for row in rows
        ),
        "updates": sum(row["action"] == "update_collection_snapshot" for row in rows),
    }


def _enforce_project_scope(
    operations: list[dict],
    allowed_project_ids: set[str] | None,
) -> None:
    """Fail closed before preview exposure or apply writes.

    ``None`` is the shared full-scope sentinel.  A scoped account cannot create
    a project because the not-yet-created id cannot belong to its current
    visible set.  Missing or out-of-scope target ids reject the whole batch.
    """

    if allowed_project_ids is None:
        return
    for operation in operations:
        if operation.get("action") == "create_project":
            raise BulkImportScopeDenied("范围账号不能通过批量导入创建新项目")
        project_id = operation.get("project_id")
        if not project_id or str(project_id) not in allowed_project_ids:
            raise BulkImportScopeDenied("批次包含当前账号无权访问的项目，整批拒绝")


def preview_transfer(
    db: Session,
    files: list[tuple[str, bytes]],
    *,
    operated_by: str,
    allowed_project_ids: set[str] | None = None,
) -> dict:
    if not files or len(files) > MAX_TRANSFER_FILES:
        raise BulkImportInvalid(f"一次必须上传 1–{MAX_TRANSFER_FILES} 个文件")
    total_bytes = sum(len(data) for _name, data in files)
    if total_bytes > MAX_TRANSFER_TOTAL_BYTES:
        raise BulkImportInvalid("批量上传总大小超过 64 MiB")
    artifacts = [build_preview(db, data, filename) for filename, data in files]
    _enforce_project_scope(
        [
            operation
            for artifact in artifacts
            for operation in artifact.plan.get("operations") or []
        ],
        allowed_project_ids,
    )
    hashes = [artifact.file_hash for artifact in artifacts]
    if len(hashes) != len(set(hashes)):
        raise BulkImportInvalid("同一批次包含内容完全相同的重复文件")
    if sum(int(artifact.plan["summary"]["source_rows"]) for artifact in artifacts) > MAX_PREVIEW_ROWS:
        raise BulkImportInvalid(f"多文件数据行合计超过安全上限 {MAX_PREVIEW_ROWS}")

    plans = []
    file_payloads = []
    project_ids = {
        str(item["project_id"])
        for artifact in artifacts
        for item in artifact.plan.get("operations") or []
        if item.get("project_id")
    }
    project_names_by_id = _project_names_for_ids(db, project_ids)
    public_rows: list[dict] = []
    row_map: dict[str, dict] = {}
    for index, artifact in enumerate(artifacts):
        adapter = _ADAPTERS[artifact.adapter_key]
        file_id = f"file-{index + 1}-{artifact.file_hash[:12]}"
        plan = artifact.plan
        plans.append(
            {
                "file_id": file_id,
                "filename": artifact.filename,
                "form_type": artifact.adapter_key,
                "plan": plan,
            }
        )
        file_payloads.append(
            {
                "file_id": file_id,
                "filename": artifact.filename,
                "import_kind": _transfer_kind(artifact.adapter_key),
                "source_sha256": artifact.file_hash,
                "detected_sheet": plan.get("sheet"),
                "header_rows": plan.get("header_rows") or [plan.get("header_row")],
                "detected_fields": _detected_fields(
                    adapter,
                    DetectedSheet(
                        name=str(plan.get("sheet") or ""),
                        header_row=int(plan.get("header_row") or 1),
                        header_rows=tuple(plan.get("header_rows") or []),
                        headers=tuple(plan.get("headers") or []),
                        system_headers=tuple(plan.get("system_headers") or []),
                        field_indexes={},
                        field_matches=plan.get("field_matches") or {},
                        rows=(),
                    ),
                ),
                "mapping_conflicts": [],
            }
        )
        if artifact.adapter_key == "sales_contract_amount":
            rows, mapping = _public_sales_rows(
                file_id=file_id,
                filename=artifact.filename,
                plan_index=index,
                plan=plan,
                project_names_by_id=project_names_by_id,
            )
        else:
            rows, mapping = _public_receipt_rows(
                file_id=file_id,
                filename=artifact.filename,
                plan_index=index,
                plan=plan,
                project_names_by_id=project_names_by_id,
            )
        public_rows.extend(rows)
        row_map.update(mapping)

    # The same canonical target appearing in two source files is never applied
    # on first-file-wins semantics.  Both rows remain visible but blocked.
    by_target: dict[str, list[dict]] = defaultdict(list)
    for row in public_rows:
        if row.get("_target_key") and row["row_status"] == "ready":
            by_target[row["_target_key"]].append(row)
    for target, duplicates in by_target.items():
        if len(duplicates) < 2:
            continue
        for row in duplicates:
            row["match_state"] = "ambiguous"
            row["action"] = "block"
            row["row_status"] = "blocked"
            row["errors"].append(
                {
                    "code": "cross_file_duplicate_target",
                    "message": f"多个文件同时修改同一目标 {target}，请只保留一份来源",
                    "field": None,
                }
            )
            row_map.pop(row["row_key"], None)

    # 收款单：同一批次两个文件触及同一合同即整合同阻断。每个文件的计划都是
    # 独立按「台账 ∪ 本文件」推导的，谁也不知道另一个文件的收款，分别落库会让
    # 后一个月的累计漏掉前一个文件的钱；月份不重叠也一样。
    by_contract_files: dict[str, dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in public_rows:
        target = row.get("_target_key")
        if target and target.startswith("receipt:") and row.get("matched_contract_id"):
            by_contract_files[str(row["matched_contract_id"])][row["file_id"]].append(row)
    for contract_files in by_contract_files.values():
        if len(contract_files) < 2:
            continue
        filenames = "、".join(sorted({
            row["filename"] for rows in contract_files.values() for row in rows
        }))
        for rows in contract_files.values():
            for row in rows:
                order_no = (row.get("canonical") or {}).get("sales_order_no")
                row["match_state"] = "invalid"
                row["action"] = "block"
                row["row_status"] = "blocked"
                row["errors"].append(
                    {
                        "code": "cross_file_same_contract",
                        "message": (
                            f"同一批次两个文件触及同一合同 {order_no}（{filenames}），"
                            "各文件的累计推导互不知情，请合并为一份来源后重新预览"
                        ),
                        "field": None,
                    }
                )
                row_map.pop(row["row_key"], None)

    for row in public_rows:
        row.pop("_target_key", None)
    public_rows.sort(
        key=lambda row: (row["filename"], row["source_row"], row["row_key"])
    )
    summary = _transfer_summary(public_rows)
    expires_at = datetime.now(timezone.utc) + TRANSFER_TOKEN_TTL
    data_version = _canonical_hash(
        [
            {
                "form_type": item["form_type"],
                "operations": item["plan"].get("operations") or [],
            }
            for item in plans
        ]
    )
    public_payload = {
        "schema_version": TRANSFER_SCHEMA_VERSION,
        "files": file_payloads,
        "rows": public_rows,
        "summary": summary,
        "can_apply": summary["ready"] > 0,
    }
    payload_hash = _canonical_hash(
        {
            "public": public_payload,
            "plans": plans,
            "data_version": data_version,
        }
    )
    # file_hash = 原件 sha256（单文件）/ 各原件 sha256 排序后再 sha256（多文件）；
    # 逐文件 sha256 见 sys_raw_file 与 report_json.files[].source_sha256。
    # 预览 payload 与 apply 选择的指纹都留在 report_json，不再挤占 file_hash。
    batch = SysImportBatch(
        filename=(";".join(name for name, _data in files))[:256],
        file_type=TRANSFER_BATCH_TYPE,
        file_hash=_batch_file_hash(hashes),
        uploaded_by=operated_by,
        rows_total=summary["total"],
        rows_inserted=0,
        rows_skipped=summary["total"] - summary["ready"],
        rows_error=summary["invalid"] + summary["ambiguous"],
        status="processing",
    )
    db.add(batch)
    db.flush()
    # 原件在预览时即按内容寻址归档（与通用导入同一 _archive）；sys_raw_file 行
    # 要到应用成功才落——只预览不应用的批次不该把原件钉死在磁盘上，过了宽限期
    # 由 raw_archive_gc 当孤儿回收。存储路径先记在 report_json（不进公开 payload）。
    archives = [
        {
            "file_id": f"file-{index + 1}-{artifact.file_hash[:12]}",
            "filename": filename[:256],
            "file_hash": artifact.file_hash,
            "storage_path": pipeline.archive_bytes(data, artifact.file_hash),
        }
        for index, ((filename, data), artifact) in enumerate(
            zip(files, artifacts, strict=True)
        )
    ]
    secret = secrets.token_urlsafe(32)
    preview_token = f"{batch.id}.{secret}"
    batch.report_json = {
        "protocol_version": TRANSFER_SCHEMA_VERSION,
        "payload_hash": payload_hash,
        "data_version": data_version,
        "token_hash": hashlib.sha256(preview_token.encode("utf-8")).hexdigest(),
        "expires_at": expires_at.isoformat(),
        "plans": plans,
        "row_map": row_map,
        "public": public_payload,
        "archives": archives,
        "previewed_at": datetime.now(timezone.utc).isoformat(),
    }
    for row in public_rows:
        for issue in row["errors"]:
            db.add(
                SysImportError(
                    batch_id=batch.id,
                    row_no=row["source_row"],
                    error_type=str(issue["code"])[:32],
                    error_detail=str(issue["message"])[:4000],
                    raw_row={
                        "row_key": row["row_key"],
                        "file_id": row["file_id"],
                        "filename": row["filename"],
                        "issue": issue,
                    },
                )
            )
    db.commit()
    return {
        **public_payload,
        "preview_id": str(batch.id),
        "preview_token": preview_token,
        "payload_hash": payload_hash,
        "data_version": data_version,
        "expires_at": expires_at.isoformat(),
    }


# 写已确认快照 / 台账行的收款公开行动作：新建、覆盖、登记入台账。
_FACT_WRITING_ACTIONS = frozenset({
    "upsert_collection_snapshot",
    "update_collection_snapshot",
    "record_receipts",
})


def _applied_selection_statement(*, batch_id: int, selection_hash: str):
    """按 report_json.selection_hash 找已成功批次的查询（走 ix_batch_success_selection_hash）。"""

    return select(SysImportBatch).where(
        SysImportBatch.id != batch_id,
        SysImportBatch.file_type == TRANSFER_BATCH_TYPE,
        SysImportBatch.report_json["selection_hash"].as_string() == selection_hash,
        SysImportBatch.status == "success",
    )


def _batch_file_hash(file_hashes: list[str]) -> str:
    if len(file_hashes) == 1:
        return file_hashes[0]
    return hashlib.sha256("\n".join(sorted(file_hashes)).encode("ascii")).hexdigest()


def _batch_id_from_transfer_token(preview_token: str) -> int:
    prefix, separator, secret = preview_token.partition(".")
    if not separator or not prefix.isdigit() or len(secret) < 24:
        raise BulkImportConflict("预览 token 无效")
    return int(prefix)


def record_transfer_failure(
    db: Session,
    *,
    preview_token: str,
    operated_by: str,
    error_code: str,
    message: str,
    allow_admin: bool = False,
) -> None:
    """Persist a failed apply attempt after the domain transaction rolled back."""

    batch_id = _batch_id_from_transfer_token(preview_token)
    batch = db.scalar(
        select(SysImportBatch)
        .where(SysImportBatch.id == batch_id)
        .with_for_update()
    )
    if (
        batch is None
        or batch.file_type != TRANSFER_BATCH_TYPE
        or (batch.uploaded_by != operated_by and not allow_admin)
        or batch.status != "processing"
    ):
        return
    report = dict(batch.report_json or {})
    expected = str(report.get("token_hash") or "")
    actual = hashlib.sha256(preview_token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(expected, actual):
        return
    failure = {
        "error_code": error_code[:64],
        "message": message[:1000],
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "failed_by": operated_by,
    }
    report["failure"] = failure
    batch.report_json = report
    batch.status = "failed"
    batch.rows_error = max(int(batch.rows_error or 0), 1)
    db.add(
        SysAuditLog(
            entity_type="maintenance_bulk_import",
            entity_id=batch.id,
            action="failed",
            before_json={"status": "processing"},
            after_json={"status": "failed", **failure},
            reason="批量导入应用失败，业务事务已整体回滚",
            operated_by=operated_by,
        )
    )
    db.commit()


def apply_transfer(
    db: Session,
    *,
    preview_token: str,
    payload_hash: str,
    data_version: str,
    row_keys: list[str],
    operated_by: str,
    allow_admin: bool = False,
    allowed_project_ids: set[str] | None = None,
    real_operator: bool = False,
) -> dict:
    """应用冻结预览。

    ``real_operator``：HTTP 入口传入实名判定（authn == sys_user、非共享口令回退、
    账号有效）。勾选里含任何写已确认快照 / 台账行的收款行（新建、覆盖、登记入台账）
    时必须实名，与 stable 项目 API ``POST /projects/stable/{id}/collections`` 的经营
    事实写入同一门禁；缺省 False 即失败关闭。
    """

    batch_id = _batch_id_from_transfer_token(preview_token)
    batch = db.scalar(
        select(SysImportBatch)
        .where(SysImportBatch.id == batch_id)
        .with_for_update()
    )
    if batch is None or batch.file_type != TRANSFER_BATCH_TYPE:
        raise BulkImportNotFound("批量预览不存在")
    if batch.uploaded_by != operated_by and not allow_admin:
        raise BulkImportNotFound("批量预览不存在")
    report = dict(batch.report_json or {})
    expected_token = str(report.get("token_hash") or "")
    actual_token = hashlib.sha256(preview_token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(expected_token, actual_token):
        raise BulkImportConflict("预览 token 无效")
    if not hmac.compare_digest(str(report.get("payload_hash") or ""), payload_hash):
        raise BulkImportConflict("预览 payload hash 不匹配")
    if not hmac.compare_digest(str(report.get("data_version") or ""), str(data_version)):
        raise BulkImportConflict("预览数据版本不匹配")
    selected = list(dict.fromkeys(str(key) for key in row_keys))
    if not selected or len(selected) != len(row_keys):
        raise BulkImportInvalid("必须选择至少一行，且 row_keys 不能重复")
    if batch.status == "success":
        applied = [str(key) for key in report.get("selected_row_keys") or []]
        if not applied or sorted(selected) != sorted(applied):
            raise BulkImportConflict("该批次已应用，row_keys 与原应用选择不一致")
        expected_selection_hash = _canonical_hash(
            {"payload_hash": payload_hash, "row_keys": sorted(applied)}
        )
        if not hmac.compare_digest(
            str(report.get("selection_hash") or ""),
            expected_selection_hash,
        ):
            raise BulkImportConflict("已应用批次的选择证据不完整")
        result = dict(report.get("result") or {})
        _enforce_project_scope(
            [
                {
                    "action": "replay",
                    "project_id": row.get("project_id"),
                }
                for row in result.get("rows") or []
            ],
            allowed_project_ids,
        )
        return result
    if batch.status != "processing":
        raise BulkImportConflict("该预览已失败或失效，请重新上传预览")
    expires_at = datetime.fromisoformat(str(report["expires_at"]))
    if expires_at <= datetime.now(timezone.utc):
        raise BulkImportConflict("预览已过期，请重新上传预览")
    row_map = report.get("row_map") or {}
    if any(key not in row_map for key in selected):
        raise BulkImportInvalid("包含不可提交、未知或已阻断的 row_key")
    public_by_key = {
        row["row_key"]: row for row in (report.get("public") or {}).get("rows") or []
    }
    if any(
        public_by_key.get(key, {}).get("row_status") != "ready"
        for key in selected
    ):
        raise BulkImportInvalid("只能提交预览状态为 ready 的行")

    plans = report.get("plans") or []
    selected_by_plan: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for row_key in selected:
        mapping = row_map[row_key]
        selected_by_plan[int(mapping["plan_index"])].append(
            (row_key, int(mapping["operation_index"]))
        )

    selected_operations = [
        plans[plan_index]["plan"]["operations"][operation_index]
        for plan_index, mappings in selected_by_plan.items()
        for _row_key, operation_index in mappings
    ]
    _enforce_project_scope(selected_operations, allowed_project_ids)
    # 经营事实写入门禁：新建 / 覆盖已确认快照、登记台账行都是经营事实，一律实名。
    if not real_operator and any(
        public_by_key[key].get("action") in _FACT_WRITING_ACTIONS
        for key in selected
    ):
        raise BulkImportScopeDenied(REAL_OPERATOR_MESSAGE)

    selection_hash = _canonical_hash(
        {"payload_hash": payload_hash, "row_keys": sorted(selected)}
    )
    _advisory_lock(db, f"maintenance-transfer-apply:{selection_hash}")
    # 偏唯一索引下最多一行；不带 ORDER BY id LIMIT 1——那会让规划器沿主键扫描。
    already = min(
        db.scalars(
            _applied_selection_statement(batch_id=batch.id, selection_hash=selection_hash)
        ).all(),
        key=lambda row: row.id,
        default=None,
    )
    if already is not None:
        already_report = dict(already.report_json or {})
        already_selected = [
            str(key) for key in already_report.get("selected_row_keys") or []
        ]
        if (
            sorted(already_selected) != sorted(selected)
            or not hmac.compare_digest(
                str(already_report.get("selection_hash") or ""),
                selection_hash,
            )
        ):
            raise BulkImportConflict("幂等批次的已应用行选择证据不一致")
        return dict(already_report.get("result") or {})

    project_ids: set[str] = set()
    details_by_key: dict[str, dict] = {}
    audit_reason = f"全项目批量传输 batch={batch.id} payload={payload_hash}"
    for plan_index in sorted(selected_by_plan):
        plan_wrapper = plans[plan_index]
        adapter = _ADAPTERS.get(plan_wrapper["form_type"])
        if adapter is None:
            raise BulkImportConflict("预览使用的表单适配器已不存在")
        source_plan = plan_wrapper["plan"]
        op_indexes = [op_index for _key, op_index in selected_by_plan[plan_index]]
        subplan = {
            **source_plan,
            "operations": [source_plan["operations"][index] for index in op_indexes],
        }
        result = adapter.apply_plan(
            db,
            subplan,
            operated_by=operated_by,
            audit_reason=f"{audit_reason} file={plan_wrapper['file_id']}",
            provenance={
                "batch_id": batch.id,
                "source_sha256": source_plan.get("file_hash"),
            },
        )
        project_ids.update(result.get("project_ids") or [])
        details_by_key.update(result.get("details") or {})

    result_rows: list[dict] = []
    for row_key in selected:
        public = public_by_key[row_key]
        mapping = row_map[row_key]
        wrapper = plans[int(mapping["plan_index"])]
        operation = wrapper["plan"]["operations"][int(mapping["operation_index"])]
        project_id = operation.get("project_id")
        project_contract_id = operation.get("project_contract_id")
        entity_id = project_contract_id
        report_month = operation.get("report_month")
        aggregate_key = None
        if wrapper["form_type"] == "sales_contract_amount":
            relation = db.scalar(
                select(MaintenanceProjectContract)
                .where(
                    MaintenanceProjectContract.contract_id
                    == operation["contract_id"],
                    MaintenanceProjectContract.contract_no
                    == operation["sales_order_no"],
                )
                .order_by(MaintenanceProjectContract.created_at.desc())
                .limit(1)
            )
            if relation is not None:
                project_id = relation.project_id
                project_contract_id = relation.project_contract_id
                entity_id = relation.project_contract_id
        else:
            aggregate_key = f"{operation['project_contract_id']}:{operation['report_month']}"
            snapshot = db.scalar(
                select(MaintenanceCollectionSnapshot).where(
                    MaintenanceCollectionSnapshot.project_contract_id
                    == operation["project_contract_id"],
                    MaintenanceCollectionSnapshot.report_month
                    == date.fromisoformat(operation["report_month"]),
                )
            )
            entity_id = snapshot.collection_id if snapshot is not None else None
        if project_id:
            project_ids.add(project_id)
        detail = details_by_key.get(aggregate_key or "", {}) if aggregate_key else {}
        result_rows.append(
            {
                "row_key": row_key,
                "source_file": public["filename"],
                "source_sheet": public.get("detected_sheet"),
                "source_row": public["source_row"],
                "status": "applied",
                "action": public["action"],
                "project_id": project_id,
                "contract_id": operation.get("contract_id"),
                "entity_id": detail.get("entity_id", entity_id),
                "message": detail.get("message") or "已按冻结预览在同一事务中应用",
                "error_code": None,
                "before_version": detail.get(
                    "before_version", operation.get("expected_contract_version")
                ),
                "after_version": detail.get("after_version"),
                "aggregate_key": aggregate_key,
                "project_contract_id": project_contract_id,
                "report_month": report_month,
                # 覆盖回执：原值→新值、原来源/时间（D-16）
                "before_amount": detail.get("before_amount"),
                "after_amount": detail.get("after_amount"),
                "previous_source": detail.get("previous_source"),
                "previous_import_batch_id": detail.get("previous_import_batch_id"),
                "previous_updated_at": detail.get("previous_updated_at"),
                "previous_operated_by": detail.get("previous_operated_by"),
                "receipts_recorded": detail.get("receipts_recorded"),
            }
        )

    # 原件登记（sys_raw_file）与事实写入同一事务：只有应用成功的批次才钉住原件。
    # 落行前逐个核验原件仍是 sha256 相符的普通文件：预览到应用之间原件若被回收 /
    # 篡改，宁可整批回滚（ArchiveError → 500 archive_failed，批次保持 processing）
    # 也不钉住一条指向空路径的登记。
    for archive in report.get("archives") or []:
        pipeline.verify_archive(
            str(archive.get("storage_path") or ""), str(archive.get("file_hash") or "")
        )
        db.add(
            SysRawFile(
                batch_id=batch.id,
                filename=str(archive.get("filename") or "")[:256] or None,
                file_hash=archive.get("file_hash"),
                storage_path=archive.get("storage_path"),
            )
        )

    result = {
        "batch_id": str(batch.id),
        "status": "done",
        "applied": len(result_rows),
        "skipped": 0,
        "blocked": 0,
        "project_ids": sorted(project_ids),
        "invalidated_projects": sorted(project_ids),
        "audit_ref": f"maintenance_bulk_import:{batch.id}",
        "rows": result_rows,
    }
    report["result"] = result
    report["selected_row_keys"] = selected
    report["selection_hash"] = selection_hash
    report["applied_at"] = datetime.now(timezone.utc).isoformat()
    report["applied_by"] = operated_by
    # file_hash 保持原件 sha256；选择指纹只在 report_json.selection_hash。
    batch.report_json = report
    batch.status = "success"
    batch.rows_inserted = len(result_rows)
    batch.rows_skipped = int(batch.rows_total or 0) - len(result_rows)
    batch.rows_error = 0
    db.add(
        SysAuditLog(
            entity_type="maintenance_bulk_import",
            entity_id=batch.id,
            action="apply",
            before_json={
                "status": "processing",
                "payload_hash": payload_hash,
                "data_version": data_version,
            },
            after_json={
                "status": "success",
                "selection_hash": selection_hash,
                "applied": len(result_rows),
                "project_ids": sorted(project_ids),
            },
            reason=audit_reason,
            operated_by=operated_by,
        )
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


def _receipt_dict(row: MaintenanceCollectionReceipt) -> dict:
    return _jsonable({
        "id": row.id,
        "project_contract_id": row.project_contract_id,
        "contract_no": row.contract_no,
        "receipt_no": row.receipt_no,
        "receipt_date": row.receipt_date,
        "actual_amount": row.actual_amount,
        "remark": row.remark,
        "import_batch_id": row.import_batch_id,
        "source_sha256": row.source_sha256,
        "is_active": row.is_active,
        "created_by": row.created_by,
        "superseded_by": row.superseded_by,
        "ruling_id": row.ruling_id,
        "ruling_reason": row.ruling_reason,
        "ruled_by": row.ruled_by,
        "ruled_at": row.ruled_at,
    })


def rule_receipt(
    db: Session,
    *,
    contract_no: str,
    receipt_no: str,
    receipt_date: date,
    actual_amount: Decimal | str,
    reason: str,
    operated_by: str,
) -> dict:
    """人工裁决台账冲突（D-16 / REQUIREMENTS #56：冲突人工裁决，不自动覆盖不求和）。

    不改行：旧行 ``is_active=False`` + ``superseded_by`` / 裁决人 / 时间 / 理由留档，
    另插一条生效的更正行（``ruling_id``，无批次、无原件 sha256）；``sys_audit_log``
    落 ``collection_receipt/supersede``。**不动快照**——裁决改变的是台账，月度累计
    的变化由下一次预览按台账推导成 update 行、由人显式勾选写入。
    返回受影响月份（已确认快照与按新台账推导值不等的月份）供前端提示。
    """

    norm = normalize_order_no(contract_no)
    receipt_no = _text(receipt_no)
    if not norm or not receipt_no:
        raise BulkImportInvalid("销售订单号和收款单号不能为空")
    reason = str(reason or "").strip()
    if not reason or len(reason) > 1000:
        raise BulkImportInvalid("裁决理由必填且不超过 1000 字")
    amount = _decimal(actual_amount, label="实收金额")
    if not isinstance(receipt_date, date) or isinstance(receipt_date, datetime):
        raise BulkImportInvalid("收款日期无效")
    existing = db.scalar(
        select(MaintenanceCollectionReceipt).where(
            MaintenanceCollectionReceipt.contract_no == norm,
            MaintenanceCollectionReceipt.receipt_no == receipt_no,
            MaintenanceCollectionReceipt.is_active.is_(True),
        )
    )
    if existing is None:
        raise BulkImportNotFound(f"台账中没有收款单 {receipt_no}（销售订单 {norm}）的生效行")
    # 与批量应用同一把合同锁：裁决与入账串行，指纹/唯一键不会在窗口里交错。
    _advisory_lock(db, f"maintenance-receipt-ledger:{existing.project_contract_id}")
    existing = db.scalar(
        select(MaintenanceCollectionReceipt)
        .where(MaintenanceCollectionReceipt.id == existing.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is None or not existing.is_active:
        raise BulkImportConflict("该收款单台账行已被他人裁决，请刷新后重试")
    if existing.receipt_date == receipt_date and existing.actual_amount == amount:
        raise BulkImportInvalid("裁决值与台账现值相同，无需裁决")

    before = _receipt_dict(existing)
    ruling_id = str(uuid4())
    ruled_at = datetime.now(timezone.utc)
    # 生效行偏唯一索引：先让旧行退出生效集，再插更正行，避免同一 flush 内先插后改撞索引。
    existing.is_active = False
    existing.ruling_reason = reason
    existing.ruled_by = operated_by
    existing.ruled_at = ruled_at
    db.flush()
    corrected = MaintenanceCollectionReceipt(
        project_contract_id=existing.project_contract_id,
        contract_no=norm,
        receipt_no=receipt_no,
        receipt_date=receipt_date,
        actual_amount=amount,
        remark=existing.remark,
        import_batch_id=None,
        source_sha256=None,
        is_active=True,
        created_by=operated_by,
        ruling_id=ruling_id,
    )
    db.add(corrected)
    db.flush()
    existing.superseded_by = corrected.id
    db.flush()
    after = _receipt_dict(corrected)
    db.add(
        SysAuditLog(
            entity_type="collection_receipt",
            entity_id=existing.id,
            action="supersede",
            before_json=before,
            after_json={**after, "superseded_receipt_id": existing.id},
            reason=reason,
            operated_by=operated_by,
        )
    )

    ledger_history = [
        row for row in _ledger_receipts(db, [norm])
        if row.project_contract_id == existing.project_contract_id
    ]
    ledger_active = [row for row in ledger_history if row.is_active]
    snapshots = _existing_snapshots(db, [existing.project_contract_id])
    series = _cumulative_series(
        new_receipts=[],
        ledger_active=ledger_active,
        snapshots=snapshots,
        ledger_history=ledger_history,
    )
    # 影响面从 min(原月份, 新月份) 起重新推导（D-16 09-07 二次补充 #12）：跨月裁决
    # 把收款移走的原月份、移入的新月份都要对账，不能因为原月份掉出覆盖范围而报"无影响"。
    ruling_start = min(
        series["coverage_start"],
        _month_start(existing.receipt_date),
        _month_start(receipt_date),
    )
    if ruling_start < series["coverage_start"]:
        series = _cumulative_series(
            new_receipts=[],
            ledger_active=ledger_active,
            snapshots=snapshots,
            coverage_start=ruling_start,
            ledger_history=ledger_history,
        )
    affected_months = [
        {
            "report_month": row.report_month.isoformat(),
            "current_cumulative": _jsonable(Decimal(row.cumulative_amount)),
            "derived_cumulative": _jsonable(series["cumulative"][row.report_month]),
        }
        for row in snapshots
        if row.status == "confirmed"
        and row.report_month in series["cumulative"]
        and Decimal(row.cumulative_amount) != series["cumulative"][row.report_month]
    ]
    return {
        "ruling_id": ruling_id,
        "superseded_receipt_id": existing.id,
        "new_receipt_id": corrected.id,
        "affected_months": affected_months,
    }
