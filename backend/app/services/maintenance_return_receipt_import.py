"""Archived RKD return jobs, immutable review plans and atomic CAS apply.

Jobs extend doc-import batches; complete workbooks are pinned in sys_raw_file.
Components remain independent raw lines, while only machine parents are counted.
"""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import zipfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, insert, or_, select, text
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.db import SessionLocal
from app.etl.pipeline import archive_bytes, verify_archive
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow as Head,
)
from app.models.maintenance_doc_import import (
    MaintenanceDocImportBatch as Batch,
)
from app.models.maintenance_doc_import import (
    MaintenanceDocLineRow as Line,
)
from app.models.maintenance_doc_import import (
    MaintenanceRkdReturnLine as Receipt,
)
from app.models.maintenance_project import MaintenanceProject as Project
from app.models.maintenance_source_assignment import (
    MaintenanceSourceOrderAssignment as Assignment,
)
from app.models.system import SysImportBatch, SysRawFile
from app.services import import_safety
from app.services import maintenance_return_receipts as ledger
from app.services.date_loose import parse_date_loose
from app.services.maintenance_doc_import import _WBDD_RE, MAX_PREVIEW_BYTES, _clean

PROTOCOL = "return_receipts_v1"
CATEGORIES = {"维保拆旧返件", "旧库退返"}
LEASE = timedelta(minutes=15)
_HEAD_FIELDS = (
    "入库单号",
    "入库日期",
    "入库类别",
    "入库备件/整机",
    "数据状态",
    "数据ID(不可修改)",
    "项目名称",
    "维保需求单",
    "整机PN",
    "整机描述",
    "整机SN",
    "整机测试结果",
    "整机数量",
    "备注",
)
_LINE_FIELDS = (
    "备件明细.备件PN",
    "备件明细.备件描述",
    "备件明细.备件SN",
    "备件明细.入库数量",
    "备件明细.测试结果",
    "备件明细.数据ID(不可修改)",
    "备件明细.序号",
    "备件明细.备注",
)


class ImportError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _lock(db: Session, value: str) -> None:
    key = int.from_bytes(
        hashlib.sha256(value.encode()).digest()[:8], "big", signed=True
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def parse_standard(data: bytes) -> dict:
    """Stream real double headers without trusting worksheet dimensions.

    Blank parent cells inherit every parent field only inside a stable block.
    New detail sequences cannot silently inherit a preceding document's status.
    The complete archive retains attachments omitted by the streaming reader.
    """
    import_safety.validate_xlsx_zip(data, max_bytes=MAX_PREVIEW_BYTES)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.testzip() is not None:
            raise ImportError("invalid_doc", "Excel ZIP 完整性校验失败", 422)
    rows = iter(import_safety.stream_first_sheet_rows(data))
    codes, names = next(rows, ()), next(rows, ())
    if not any(str(v or "").startswith("F00") for v in codes):
        raise ImportError("invalid_doc", "必须使用字段码/字段名双表头的标准入库单", 422)
    headers = [str(v or "").strip().replace("(必填)", "") for v in names]
    required = {
        "数据ID(不可修改)",
        "入库单号",
        "入库类别",
        "数据状态",
        "备件明细.数据ID(不可修改)",
        "备件明细.备件PN",
        "备件明细.入库数量",
    }
    if not required.issubset(headers) or len(
        [h for h in headers if h in required]
    ) != len(required):
        raise ImportError("invalid_doc", "标准入库单关键字段缺失或重复", 422)
    indexes = {h: i for i, h in enumerate(headers)}
    heads, current = [], None
    head_count = line_count = 0
    excluded = Counter()
    for row_no, row in enumerate(rows, 3):

        def cell(name):
            i = indexes.get(name)
            return (
                str(row[i]).strip()
                if i is not None and i < len(row) and row[i] is not None
                else ""
            )

        h = {name: cell(name) for name in _HEAD_FIELDS}
        l = {name: cell(name) for name in _LINE_FIELDS}
        is_start = bool(h["数据ID(不可修改)"] or h["入库单号"])
        has_line = any(l.values())
        if is_start:
            if (
                current is not None
                and h["数据ID(不可修改)"]
                and h["数据ID(不可修改)"] == current["raw"]["数据ID(不可修改)"]
            ):
                if any(
                    value and current["raw"].get(name) != value
                    for name, value in h.items()
                ):
                    raise ImportError(
                        "source_conflict", "同一主单 ID 存在不同主表内容", 422
                    )
            else:
                current = {"row_no": row_no, "raw": h, "lines": [], "seen_lines": False}
                head_count += 1
                if h["入库类别"] in CATEGORIES:
                    heads.append(current)
                else:
                    excluded[h["入库类别"] or "类别缺失"] += 1
        elif any(h.values()):
            raise ImportError(
                "invalid_doc", "主表续行含不完整主单身份，拒绝跨单继承", 422
            )
        if has_line:
            line_count += 1
            if current is None:
                raise ImportError("invalid_doc", "明细缺少所属主单", 422)
            if (
                not is_start
                and l["备件明细.序号"] in ("1", "1.0")
                and current["seen_lines"]
            ):
                raise ImportError(
                    "invalid_doc", "明细序号重新从 1 开始但缺少主单身份", 422
                )
            current["seen_lines"] = True
            if current["raw"]["入库类别"] in CATEGORIES:
                current["lines"].append({"row_no": row_no, "raw": l})
    return {
        "heads": heads,
        "head_rows": head_count,
        "line_rows": line_count,
        "excluded": dict(excluded),
    }


def enqueue(
    db: Session, data: bytes, filename: str, operator: str, key: str
) -> tuple[Batch, bool]:
    digest = hashlib.sha256(data).hexdigest()
    _lock(db, f"doc-key:{operator}:{key}")
    existing = db.scalar(
        select(Batch).where(Batch.uploaded_by == operator, Batch.idempotency_key == key)
    )
    if existing:
        if (
            existing.file_hash != digest
            or (existing.report_json or {}).get("protocol") != PROTOCOL
        ):
            raise ImportError(
                "idempotency_conflict", "同一幂等键对应不同文件或导入协议"
            )
        return existing, False
    path = archive_bytes(data, digest)
    _lock(db, "return-archive:" + digest)
    archive_batch = db.scalar(
        select(SysImportBatch).where(
            SysImportBatch.file_type == "return_receipts",
            SysImportBatch.file_hash == digest,
            SysImportBatch.status == "success",
        )
    )
    if archive_batch is None:
        archive_batch = SysImportBatch(
            filename=filename[:256],
            file_type="return_receipts",
            file_hash=digest,
            uploaded_by=operator,
            status="success",
        )
        db.add(archive_batch)
        db.flush()
        db.add(
            SysRawFile(
                batch_id=archive_batch.id,
                filename=filename[:256],
                file_hash=digest,
                storage_path=path,
            )
        )
    batch = Batch(
        batch_id=str(uuid4()),
        doc_type="rkd_inbound",
        file_hash=digest,
        filename=filename[:255],
        idempotency_key=key,
        uploaded_by=operator,
        status="pending",
        report_json={
            "protocol": PROTOCOL,
            "status": "queued",
            "generation": 1,
            "storage_path": path,
            "archive_batch_id": archive_batch.id,
        },
    )
    db.add(batch)
    db.commit()
    return batch, True


def _load(db: Session, batch_id: str, *, lock=False) -> Batch:
    batch = db.get(Batch, batch_id, with_for_update=lock, populate_existing=True)
    if batch is None or (batch.report_json or {}).get("protocol") != PROTOCOL:
        raise ImportError("not_found", "返件导入任务不存在", 404)
    return batch


def _state(batch: Batch, **changes) -> None:
    batch.report_json = {**batch.report_json, **changes}


def control(db: Session, batch_id: str, action: str) -> Batch:
    batch = _load(db, batch_id, lock=True)
    report = batch.report_json
    if report["status"] == "applied":
        raise ImportError(
            "already_applied", "已应用任务不能取消或重试，请重新上传建立预览"
        )
    if action == "retry":
        if report["status"] in ("queued", "processing"):
            started = datetime.fromisoformat(
                report.get("started_at") or batch.uploaded_at.isoformat()
            )
            if datetime.now(UTC) - started < LEASE:
                raise ImportError("job_running", "任务正在运行，可先取消后重试")
        _state(
            batch,
            status="queued",
            generation=report["generation"] + 1,
            error=None,
            rows=[],
            plan_hash=None,
            preview_token=None,
        )
        batch.status = "pending"
    else:
        _state(
            batch,
            status="cancelled",
            generation=report["generation"] + 1,
            preview_token=None,
        )
    db.commit()
    return batch


def _save_raw(db: Session, batch: Batch, parsed: dict) -> None:
    db.execute(delete(Line).where(Line.batch_id == batch.batch_id))
    db.execute(delete(Head).where(Head.batch_id == batch.batch_id))
    heads, lines = [], []
    for item in parsed["heads"]:
        raw = item["raw"]
        head_id = str(uuid4())
        day, _ = parse_date_loose(raw["入库日期"])
        heads.append(
            {
                "row_id": head_id,
                "batch_id": batch.batch_id,
                "row_no": item["row_no"],
                "raw_json": raw,
                "head_no": raw["入库单号"] or None,
                "head_date": day,
                "category": raw["入库类别"],
                "wbdd_no": _clean(raw["维保需求单"], _WBDD_RE),
                "project_name": raw["项目名称"] or None,
                "data_status": raw["数据状态"],
                "issues": [],
            }
        )
        for detail in item["lines"]:
            l = detail["raw"]
            # Validate quantities before writing NUMERIC to avoid DB rounding.
            lines.append(
                {
                    "row_id": str(uuid4()),
                    "batch_id": batch.batch_id,
                    "head_row_id": head_id,
                    "row_no": detail["row_no"],
                    "raw_json": l,
                    "line_key": l["备件明细.数据ID(不可修改)"] or None,
                    "pn": (l["备件明细.备件PN"] or None),
                    "issues": [],
                }
            )
    for model, values in ((Head, heads), (Line, lines)):
        for offset in range(0, len(values), 500):
            db.execute(insert(model), values[offset : offset + 500])
    batch.head_rows, batch.line_rows = parsed["head_rows"], parsed["line_rows"]


def _canonical(row: Receipt) -> dict:
    return {
        "head_no": row.head_no,
        "project_id": row.project_id,
        "source_order_id": row.source_order_id,
        "pn": row.pn,
        "qty": format(row.qty, ".3f"),
        "condition": row.test_result,
        "description": row.description,
        "note": row.note,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "line_status": row.line_status,
        "receipt_kind": row.receipt_kind,
        "review_required": row.review_required,
        "source_metadata": (row.source_payload or {}).get("source_metadata"),
    }


def build_plan(
    db: Session, batch: Batch, *, locked=False, allowed_project_ids=None
) -> dict:
    heads = list(
        db.scalars(
            select(Head).where(Head.batch_id == batch.batch_id).order_by(Head.row_no)
        )
    )
    details = list(
        db.scalars(
            select(Line).where(Line.batch_id == batch.batch_id).order_by(Line.row_no)
        )
    )
    by_head = {}
    for line in details:
        by_head.setdefault(line.head_row_id, []).append(line)
    projects = list(db.scalars(select(Project).where(Project.is_active.is_(True))))
    project_names = {}
    for p in projects:
        for name in {p.project_code, p.display_name}:
            project_names.setdefault(name, []).append(p)
    wbdds = {h.wbdd_no for h in heads if h.wbdd_no}
    orders = (
        list(
            db.scalars(
                ledger.active_beta_maintenance_orders(
                    select(FMaintenanceOrder).where(
                        FMaintenanceOrder.order_no.in_(wbdds),
                        FMaintenanceOrder.data_status == "已生效",
                    ),
                    FMaintenanceOrder,
                )
            )
        )
        if wbdds
        else []
    )
    order_map = {}
    for order in orders:
        order_map.setdefault(order.order_no, []).append(order)
    assignments = (
        list(
            db.scalars(
                select(Assignment).where(
                    Assignment.source_order_id.in_([o.raw_order_id for o in orders]),
                    Assignment.is_active.is_(True),
                )
            )
        )
        if orders
        else []
    )
    assignment_map = {a.source_order_id: a for a in assignments}
    project_map = {p.project_id: p for p in projects}
    rows, identities = [], {}
    relation_snapshot = []
    for head in heads:
        raw, head_lines = head.raw_json, by_head.get(head.row_id, [])
        matches = project_names.get(head.project_name, []) if head.project_name else []
        project = matches[0] if len(matches) == 1 else None
        source_order_id = None
        relation_error = None
        if head.wbdd_no:
            found = order_map.get(head.wbdd_no, [])
            order = found[0] if len(found) == 1 else None
            assignment = assignment_map.get(order.raw_order_id) if order else None
            if not order or order.data_status != "已生效" or not assignment:
                relation_error = "需求单无有效且唯一的项目归属"
            else:
                assigned_project = project_map.get(assignment.project_id)
                if (
                    not assigned_project
                    or len(matches) > 1
                    or (
                        project is not None
                        and project.project_id != assignment.project_id
                    )
                ):
                    relation_error = "需求单归属与源项目不一致或项目无法唯一识别"
                else:
                    project, source_order_id = assigned_project, order.raw_order_id
        elif raw.get("维保需求单"):
            relation_error = "源需求单号无法识别"
        if project is None:
            relation_error = relation_error or "项目未能唯一关联"
        if (
            project is not None
            and allowed_project_ids is not None
            and project.project_id not in allowed_project_ids
        ):
            raise ImportError("permission_denied", "导入涉及当前无权访问的项目", 403)
        relation_snapshot.append(
            [
                head.row_id,
                project.project_id if project else None,
                project.version if project else None,
                source_order_id,
                relation_error,
            ]
        )
        machine = raw.get("入库备件/整机") == "整机"
        units = (
            [(None, raw)] + [(l, l.raw_json) for l in head_lines]
            if machine
            else [(l, l.raw_json) for l in head_lines]
        )
        if not units:
            units = [(None, {})]
        parent_key = None
        for detail, values in units:
            kind = (
                "component"
                if machine and detail is not None
                else "machine"
                if machine
                else "part"
            )
            head_identity = raw.get("数据ID(不可修改)") or ""
            line_identity = values.get("备件明细.数据ID(不可修改)") or (
                "@machine" if kind == "machine" else ""
            )
            identity = {
                "source": "h3yun_rkd",
                "head_id": head_identity,
                "line_id": line_identity,
            }
            source_ref = "rkd:native:" + _hash(identity)
            row_key = source_ref if kind != "component" else "component:" + source_ref
            if kind == "machine":
                parent_key = row_key
            pn = (
                values.get("整机PN")
                if kind == "machine"
                else values.get("备件明细.备件PN")
            )
            qty_raw = "1" if kind == "machine" else values.get("备件明细.入库数量")
            condition = (
                values.get("整机测试结果")
                if kind == "machine"
                else values.get("备件明细.测试结果")
            )
            issues = []
            if raw.get("入库备件/整机") not in ("备件", "整机"):
                issues.append("入库备件/整机类型无法识别")
            try:
                qty = Decimal(str(qty_raw))
                if (
                    not qty.is_finite()
                    or not 0 < qty < 10**11
                    or qty != qty.quantize(Decimal(".001"))
                ):
                    raise ValueError()
            except (InvalidOperation, ValueError, TypeError):
                qty = None
                issues.append("数量必须为正值且最多三位小数，源值已保留")
            if not pn or len(pn) > 128:
                issues.append("PN 缺失或超长")
            if not head_identity or not line_identity:
                issues.append("缺少稳定主单或明细 ID")
            if not head.head_no or not head.head_date:
                issues.append("单号或入库日期缺失")
            after = {
                "head_no": head.head_no,
                "project_id": project.project_id if project else None,
                "source_order_id": source_order_id,
                "pn": pn,
                "qty": format(qty, ".3f") if qty is not None else str(qty_raw or ""),
                "condition": condition or None,
                "description": (
                    values.get("整机描述")
                    if kind == "machine"
                    else values.get("备件明细.备件描述")
                )
                or None,
                "note": (values.get("备件明细.备注") or raw.get("备注")) or None,
                "occurred_at": datetime.combine(
                    head.head_date, datetime.min.time(), tzinfo=UTC
                ).isoformat()
                if head.head_date
                else None,
                "line_status": "active" if head.data_status == "已生效" else "voided",
                "receipt_kind": "machine" if machine else "part",
                "review_required": bool(
                    qty is not None and qty != qty.to_integral_value()
                ),
                "source_metadata": {
                    "category": head.category,
                    "sn": (
                        values.get("整机SN")
                        if kind == "machine"
                        else values.get("备件明细.备件SN")
                    )
                    or None,
                    "machine_source_qty": raw.get("整机数量")
                    if kind == "machine"
                    else None,
                    "components": [line.raw_json for line in head_lines]
                    if kind == "machine"
                    else None,
                },
            }
            if (
                len(after["description"] or "") > 256
                or len(after["note"] or "") > 512
                or len(condition or "") > 64
            ):
                issues.append("描述、备注或件况超过台账字段长度")
            row = {
                "row_key": row_key,
                "head_row_id": head.row_id,
                "head_no": head.head_no,
                "source_ref": source_ref,
                "identity": identity,
                "pn": pn,
                "qty": after["qty"],
                "condition": condition or None,
                "project_id": after["project_id"],
                "project_name": project.display_name if project else head.project_name,
                "wbdd_no": head.wbdd_no,
                "kind": kind,
                "parent_row_key": parent_key if kind == "component" else None,
                "review_required": after["review_required"],
                "after": after,
                "action": "create",
                "reason": "新增返件",
                "legacy_ref": "rkd:"
                + hashlib.sha1(f"{head.head_no}:{line_identity}".encode()).hexdigest(),
            }
            if kind == "component":
                row.update(
                    action="excluded", reason="整机组成明细，仅展示，计数取整机 1 台"
                )
            elif after["line_status"] == "voided":
                row.update(action="excluded", reason="源单据未生效，不新增返件")
            elif issues:
                row.update(action="invalid", reason="；".join(issues))
            elif relation_error:
                row.update(action="pending", reason=relation_error)
            if kind != "component":
                previous = identities.get(source_ref)
                if previous:
                    if previous["after"] != after:
                        previous.update(
                            action="invalid", reason="同一来源 ID 对应不同内容"
                        )
                        row.update(action="invalid", reason="同一来源 ID 对应不同内容")
                    else:
                        row.update(action="excluded", reason="文件内重复来源明细")
                else:
                    identities[source_ref] = row
            rows.append(row)
    refs = {r[k] for r in rows for k in ("source_ref", "legacy_ref")}
    native_heads = {r["identity"]["head_id"] for r in rows}
    query = (
        select(Receipt)
        .where(
            or_(
                Receipt.source_ref.in_(refs),
                Receipt.source_payload["identity"]["head_id"].astext.in_(native_heads),
            )
        )
        .order_by(Receipt.rkd_line_id)
    )
    if locked:
        query = query.with_for_update().execution_options(populate_existing=True)
    existing = {r.source_ref: r for r in db.scalars(query)} if refs else {}
    if allowed_project_ids is not None and any(
        r.project_id not in allowed_project_ids for r in existing.values()
    ):
        raise ImportError("permission_denied", "既有返件属于当前无权访问的项目", 403)
    receipt_snapshot = []
    for row in rows:
        if row["kind"] == "machine":
            related = [
                r
                for r in existing.values()
                if r.receipt_kind == "part"
                and (
                    (r.source_payload or {}).get("identity", {}).get("head_id")
                    == row["identity"]["head_id"]
                    or r.source_ref
                    in {
                        c["legacy_ref"]
                        for c in rows
                        if c["parent_row_key"] == row["row_key"]
                    }
                )
            ]
            if related:
                row.update(
                    action="invalid",
                    reason="整机组成件已由其他入口入账，须先明确核对更正，避免重复计数",
                )
    for row in rows:
        if (
            row["kind"] == "component"
            or row["action"] in ("invalid", "pending")
            or row["reason"] == "文件内重复来源明细"
        ):
            continue
        old = existing.get(row["source_ref"])
        legacy = existing.get(row["legacy_ref"])
        if old is None and legacy is not None:
            row.update(
                action="invalid",
                reason="存在旧入口同单号明细，来源身份无法证明，请先人工核对",
            )
            continue
        if old:
            if (
                allowed_project_ids is not None
                and old.project_id not in allowed_project_ids
            ):
                raise ImportError(
                    "permission_denied", "既有返件属于当前无权访问的项目", 403
                )
            before = _canonical(old)
            receipt_snapshot.append(
                [old.rkd_line_id, old.version, before, old.source_payload]
            )
            row.update(
                receipt_id=old.rkd_line_id, before=before, expected_version=old.version
            )
            if row["after"]["line_status"] == "voided":
                # Cancellation is an explicit status correction, not a rewrite
                # from potentially incomplete fields in a cancelled export.
                row["after"] = {**before, "line_status": "voided"}
            if old.line_status == "voided" and row["after"]["line_status"] == "active":
                row.update(action="invalid", reason="返件已作废，重复上传不得复活")
            elif before == row["after"]:
                row.update(action="unchanged", reason="来源内容无变化")
            elif (
                old.source_payload
                and old.source_payload.get("canonical") == row["after"]
            ):
                row.update(action="unchanged", reason="原文件无变化，保留页面人工修改")
            else:
                row.update(
                    action="change", reason="来源内容变化，需明确确认更正并说明原因"
                )
        else:
            receipt_snapshot.append([row["source_ref"], None])
    creates = [row for row in rows if row["action"] == "create"]
    if creates:
        manual_query = (
            select(Receipt)
            .where(
                Receipt.source == "manual",
                Receipt.line_status == "active",
                Receipt.project_id.in_({row["project_id"] for row in creates}),
                Receipt.pn.in_({row["pn"] for row in creates}),
                Receipt.qty.in_({Decimal(row["qty"]) for row in creates}),
                Receipt.occurred_at.is_not(None),
            )
            .order_by(Receipt.rkd_line_id)
        )
        if locked:
            manual_query = manual_query.with_for_update().execution_options(
                populate_existing=True
            )
        manual_by_identity = {}
        for manual in db.scalars(manual_query):
            receipt_date = business_today(manual.occurred_at).isoformat()
            key = (
                manual.project_id,
                manual.pn,
                format(manual.qty, ".3f"),
                receipt_date,
            )
            manual_by_identity.setdefault(key, []).append(
                {
                    "receipt_id": manual.rkd_line_id,
                    "qty": format(manual.qty, ".3f"),
                    "occurred_at": manual.occurred_at.isoformat(),
                    "receipt_date": receipt_date,
                    "version": manual.version,
                }
            )
        for row in creates:
            source_date = business_today(
                datetime.fromisoformat(row["after"]["occurred_at"])
            ).isoformat()
            matches = manual_by_identity.get(
                (row["project_id"], row["pn"], row["qty"], source_date), []
            )
            if matches:
                row["possible_duplicates"] = matches
                row["reason"] = (
                    "可能与手工登记重复，请核对后明确确认新增；不会合并手工记录"
                )
    summary = {
        name: 0
        for name in ("create", "unchanged", "change", "pending", "excluded", "invalid")
    }
    for row in rows:
        summary[row["action"]] += 1
    summary["blocking_errors"] = summary["pending"] + summary["invalid"]
    return {
        "rows": rows,
        "summary": summary,
        "possible_duplicates_count": sum(
            bool(row.get("possible_duplicates")) for row in creates
        ),
        "plan_hash": _hash(
            [rows, relation_snapshot, receipt_snapshot, batch.file_hash]
        ),
    }


def run_job(batch_id: str, generation: int) -> None:
    """Generation CAS discards cancelled/retried workers, including after restart."""
    with SessionLocal() as db:
        batch = _load(db, batch_id, lock=True)
        if (
            batch.report_json["generation"] != generation
            or batch.report_json["status"] != "queued"
        ):
            return
        _state(batch, status="processing", started_at=datetime.now(UTC).isoformat())
        path, digest = batch.report_json["storage_path"], batch.file_hash
        db.commit()
    try:
        verify_archive(path, digest)
        parsed = parse_standard(Path(path).read_bytes())
        with SessionLocal() as db:
            batch = _load(db, batch_id, lock=True)
            if (
                batch.report_json["generation"] != generation
                or batch.report_json["status"] != "processing"
            ):
                return
            _save_raw(db, batch, parsed)
            _state(batch, excluded=parsed["excluded"])
            plan = build_plan(db, batch)
            _state(
                batch,
                **plan,
                status="ready",
                preview_token=secrets.token_urlsafe(32),
                expires_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            )
            batch.issue_rows = plan["summary"]["blocking_errors"]
            db.commit()
    except Exception as exc:
        with SessionLocal() as db:
            batch = _load(db, batch_id, lock=True)
            if (
                batch.report_json["generation"] != generation
                or batch.report_json["status"] != "processing"
            ):
                return
            error = (
                {"code": exc.code, "message": str(exc)}
                if isinstance(exc, ImportError)
                else {
                    "code": "parse_failed",
                    "message": "任务解析失败，可重试；原件已保留",
                }
            )
            _state(batch, status="failed", error=error, preview_token=None)
            batch.status = "failed"
            db.commit()


def public_job(
    db: Session, batch: Batch, *, allowed_project_ids=None, offset=0, limit=100
) -> dict:
    report = batch.report_json
    if allowed_project_ids is not None:
        if any(
            project_id and project_id not in allowed_project_ids
            for r in report.get("rows", [])
            for project_id in (
                r.get("project_id"),
                (r.get("before") or {}).get("project_id"),
            )
        ):
            raise ImportError("permission_denied", "预览包含当前无权访问的项目", 403)
        duplicate_ids = {
            candidate["receipt_id"]
            for row in report.get("rows", [])
            for candidate in row.get("possible_duplicates", [])
        }
        if duplicate_ids and any(
            project_id not in allowed_project_ids
            for project_id in db.scalars(
                select(Receipt.project_id).where(Receipt.rkd_line_id.in_(duplicate_ids))
            )
        ):
            raise ImportError(
                "permission_denied", "疑似重复记录已转至当前无权访问的项目", 403
            )
    if report.get("rows"):
        build_plan(db, batch, allowed_project_ids=allowed_project_ids)
    elif allowed_project_ids is not None:
        if any(
            r.get("project_id") and r["project_id"] not in allowed_project_ids
            for r in report.get("rows", [])
        ):
            raise ImportError("permission_denied", "预览包含当前无权访问的项目", 403)
    rows = report.get("rows", [])
    public = {
        "batch_id": batch.batch_id,
        "filename": batch.filename,
        "status": report["status"],
        "error": report.get("error"),
        "summary": report.get("summary"),
        "possible_duplicates_count": report.get("possible_duplicates_count", 0),
        "excluded_reasons": report.get("excluded", {}),
        "rows_total": len(rows),
        "offset": offset,
        "limit": limit,
        "plan_hash": report.get("plan_hash"),
        "preview_token": report.get("preview_token"),
    }
    public["rows"] = [
        {
            k: v
            for k, v in row.items()
            if k not in {"identity", "source_ref", "legacy_ref", "head_row_id"}
        }
        for row in rows[offset : offset + limit]
    ]
    return public


def apply(
    db: Session,
    batch_id: str,
    operator: str,
    *,
    plan_hash: str,
    preview_token: str,
    confirm_changes=False,
    confirm_possible_duplicates=False,
    reason=None,
    allowed_project_ids=None,
    scope_provider=None,
) -> dict:
    ledger.lock_receipt_context(db)
    _lock(db, "return-receipt-import-apply")
    batch = _load(db, batch_id)
    report = batch.report_json
    if report["status"] != "ready":
        raise ImportError("invalid_state", "任务不处于可应用预览状态，请重新预览")
    if (
        not secrets.compare_digest(report.get("preview_token") or "", preview_token)
        or report["plan_hash"] != plan_hash
    ):
        raise ImportError("invalid_token", "预览令牌或计划摘要不匹配")
    if datetime.now(UTC) > datetime.fromisoformat(report["expires_at"]):
        raise ImportError("stale_preview", "预览已过期，请重新预览")
    # Project editors and viewer/manager assignments use name -> workbook
    # state -> project, not DATA_CHANGE. Freeze that writer boundary too, before
    # the batch/receipt locks. Include original and proposed owners of transfers.
    from app.services import maintenance_project_operations, project_names

    source_names = list(
        db.scalars(
            select(Head.project_name).where(
                Head.batch_id == batch_id,
                Head.project_name.is_not(None),
            )
        )
    )
    project_names.lock_display_name_identities(db, source_names)
    current = build_plan(db, batch)
    project_ids = sorted(
        {
            project_id
            for row in report.get("rows", []) + current["rows"]
            for project_id in (
                row.get("project_id"),
                (row.get("before") or {}).get("project_id"),
            )
            if project_id
        }
    )
    maintenance_project_operations.lock_workbook_states(db, project_ids=project_ids)
    for project_id in project_ids:
        db.scalar(
            select(Project)
            .where(Project.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    batch = _load(db, batch_id, lock=True)
    report = batch.report_json
    if (
        report["status"] != "ready"
        or not secrets.compare_digest(report.get("preview_token") or "", preview_token)
        or report.get("plan_hash") != plan_hash
    ):
        raise ImportError("stale_preview", "预览任务在等待期间已变化，请重新预览")
    if scope_provider is not None:
        allowed_project_ids = scope_provider()
    verify_archive(report["storage_path"], batch.file_hash)
    plan = build_plan(db, batch, locked=True, allowed_project_ids=allowed_project_ids)
    if plan["plan_hash"] != plan_hash:
        raise ImportError("stale_preview", "预览后返件或项目归属已变化，请重新预览")
    if plan["summary"]["blocking_errors"]:
        raise ImportError(
            "blocking_errors", "预览存在无效或待关联行，整批拒绝应用", 422
        )
    if plan["possible_duplicates_count"] and not confirm_possible_duplicates:
        raise ImportError(
            "possible_duplicate_confirmation_required",
            "存在疑似重复的手工登记，请核对并明确确认仍新增独立收货记录",
            422,
        )
    if plan["summary"]["change"] and (
        not confirm_changes or not (reason or "").strip()
    ):
        raise ImportError("confirmation_required", "请明确确认更正并填写原因", 422)
    # A model merge locks dimensions in id order. Freeze this binding decision
    # in that same order; a merge already holding the row wins and its newly
    # inactive source is excluded when PostgreSQL rechecks the waiting query.
    write_pns = {
        item["after"]["pn"]
        for item in plan["rows"]
        if item["action"] in ("create", "change")
    }
    part_map = {
        pn: part_id
        for part_id, pn in db.execute(
            select(DimPart.id, DimPart.pn_std)
            .where(DimPart.pn_std.in_(write_pns), DimPart.status == "active")
            .order_by(DimPart.id)
            .with_for_update(read=True)
        )
    }
    now = datetime.now(UTC)
    applied = 0
    for item in plan["rows"]:
        if item["action"] not in ("create", "change"):
            continue
        canonical = item["after"]
        row = db.get(Receipt, item["receipt_id"]) if item.get("receipt_id") else None
        before = (
            {**ledger._receipt_dict(row), "source_evidence": row.source_payload}
            if row
            else None
        )
        part_id = part_map.get(canonical["pn"])
        if row is None:
            row = Receipt(
                rkd_line_id=str(uuid4()),
                batch_id=batch.batch_id,
                head_row_id=item["head_row_id"],
                source="rkd_import",
                source_ref=item["source_ref"],
                head_no=item["head_no"],
                created_by=operator,
                created_at=now,
                version=1,
            )
            db.add(row)
        else:
            row.version += 1
            row.updated_by, row.updated_at = operator, now
            row.batch_id, row.head_row_id = batch.batch_id, item["head_row_id"]
        for key in (
            "head_no",
            "project_id",
            "source_order_id",
            "pn",
            "description",
            "note",
            "line_status",
            "receipt_kind",
            "review_required",
        ):
            setattr(row, key, canonical[key])
        row.qty = Decimal(canonical["qty"])
        row.test_result = canonical["condition"]
        row.occurred_at = datetime.fromisoformat(canonical["occurred_at"])
        row.part_id = part_id
        if row.line_status == "voided":
            row.voided_at, row.voided_by, row.void_reason = (
                now,
                operator,
                reason.strip(),
            )
        source_head = db.get(Head, item["head_row_id"])
        source_head.project_id = row.project_id
        row.source_payload = {
            "identity": item["identity"],
            "canonical": canonical,
            "source_metadata": canonical["source_metadata"],
            "category": source_head.category,
            "file_hash": batch.file_hash,
            "batch_id": batch.batch_id,
        }
        evidence_after = {
            **ledger._receipt_dict(row),
            "source_evidence": row.source_payload,
        }
        ledger._audit(
            db,
            project_id=row.project_id,
            entity_id=row.rkd_line_id,
            action="import_create" if before is None else "import_correct",
            before=before,
            after=evidence_after,
            reason=(reason or "标准入库单返件导入").strip(),
            operated_by=operator,
        )
        applied += 1
    db.flush()
    batch.status, batch.applied_by, batch.applied_at = "applied", operator, now
    _state(batch, status="applied", preview_token=None, applied_lines=applied)
    db.commit()
    return {
        "batch_id": batch_id,
        "status": "applied",
        "applied_lines": applied,
        "summary": plan["summary"],
    }
