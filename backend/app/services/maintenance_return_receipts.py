"""统一返还收货台账（2026-09-11 拍板口径）。

页面手工登记与（后续）RKD 入库导入共用 maintenance_rkd_return_line 一张台账：
- 归属：project 必填；source_order_id 可空（未关联需求单，补选只改归属不重复计数）。
- 统计：项目总量 = Σ(active)，需求单量 = Σ(active AND source_order_id=X)，
  未关联 = Σ(active AND source_order_id IS NULL)；三个数是同一批行的不同视角。
- 手工登记即视为已收到返件；数量限正整数（§5.2 拍板）；件况不影响数量。
- 修改/作废走版本 CAS + maintenance_project_operation_audit 留痕（前后值、原因、操作人）。
- 旧坏件口径消费方在各自查询侧冻结（source='rkd_import' AND test_result IN
  RKD_RETURN_TEST_RESULTS AND line_status='active'），本服务不改动旧口径。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha1
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import RKD_RETURN_TEST_RESULTS
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
)
from app.models.maintenance_source_assignment import (
    MaintenanceSourceOrderAssignment,
)

MANUAL_CONDITIONS = ("成品", "坏品", "废品")

ALLOWED_UPDATE_FIELDS = frozenset(
    {
        "project_id",
        "wbdd_no",
        "pn",
        "part_id",
        "description",
        "qty",
        "condition",
        "note",
        "evidence_ref",
        "occurred_at",
    }
)

_AUDIT_ENTITY_TYPE = "return_receipt"


def legacy_bad_return_filter() -> tuple:
    """旧坏件口径（D-14 分子）冻结过滤器：只看 rkd_import 来源 + 坏品类件况 + 有效行。

    台账统一后本表会出现手工行（source='manual'）与「件况全收」导入行
    （成品也入账）；旧口径消费方（返还率 / boss 坏件回收 / PN 分析 / 回收清单）
    必须用本过滤器显式冻结，不随新口径漂移（先并存后切换，2026-09-11 计划 §3.4）。
    """
    return (
        MaintenanceRkdReturnLine.source == "rkd_import",
        MaintenanceRkdReturnLine.test_result.in_(RKD_RETURN_TEST_RESULTS),
        MaintenanceRkdReturnLine.line_status == "active",
    )


class ReturnReceiptError(RuntimeError):
    """返还台账操作失败。"""


class ReturnReceiptNotFound(ReturnReceiptError):
    """返还记录或项目不存在。"""


class ReturnReceiptConflict(ReturnReceiptError):
    """版本冲突或状态不允许该操作。"""


class ReturnReceiptValidation(ReturnReceiptError):
    """入参不合法。"""


def _now() -> datetime:
    return datetime.now(UTC)


def _qty(value: Decimal) -> str:
    return format(value, ".3f")


def _audit(
    db: Session,
    *,
    project_id: str,
    entity_id: str,
    action: str,
    before: dict | None,
    after: dict | None,
    reason: str,
    operated_by: str,
) -> None:
    db.add(
        MaintenanceProjectOperationAudit(
            project_id=project_id,
            entity_type=_AUDIT_ENTITY_TYPE,
            entity_id=entity_id,
            action=action,
            before_json=before,
            after_json=after,
            reason=reason,
            operated_by=operated_by,
        )
    )


def _receipt_dict(row: MaintenanceRkdReturnLine, order_no: str | None = None) -> dict:
    return {
        "receipt_id": row.rkd_line_id,
        "project_id": row.project_id,
        "source": row.source,
        "source_order_id": row.source_order_id,
        "order_no": order_no,
        "batch_id": row.batch_id,
        "head_row_id": row.head_row_id,
        "head_no": row.head_no,
        "source_ref": row.source_ref,
        "part_id": row.part_id,
        "pn": row.pn,
        "description": row.description,
        "qty": _qty(Decimal(row.qty)),
        "condition": row.test_result,
        "note": row.note,
        "evidence_ref": row.evidence_ref,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "line_status": row.line_status,
        "version": row.version,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "voided_by": row.voided_by,
        "voided_at": row.voided_at.isoformat() if row.voided_at else None,
        "void_reason": row.void_reason,
    }


def _get_project(db: Session, project_id: str) -> MaintenanceProject:
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        raise ReturnReceiptNotFound("维保项目不存在")
    return project


def _resolve_part_id(db: Session, pn: str, part_id: int | None) -> int | None:
    if part_id is not None:
        return part_id
    return db.scalar(select(DimPart.id).where(DimPart.pn_std == pn))


def _resolve_source_order_id(
    db: Session, wbdd_no: str, project_id: str
) -> str:
    order = db.execute(
        select(FMaintenanceOrder).where(FMaintenanceOrder.order_no == wbdd_no)
    ).scalar_one_or_none()
    if order is None:
        raise ReturnReceiptValidation(f"维保需求单号不存在：{wbdd_no}")
    assignment = db.execute(
        select(MaintenanceSourceOrderAssignment)
        .where(
            MaintenanceSourceOrderAssignment.source_order_id == order.raw_order_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise ReturnReceiptValidation(f"维保需求单尚未归属项目：{wbdd_no}")
    if assignment.project_id != project_id:
        raise ReturnReceiptValidation(
            f"维保需求单 {wbdd_no} 已归属其他项目，与当前项目冲突"
        )
    return order.raw_order_id


def _validate_manual_fields(
    *,
    pn: str | None,
    qty: object,
    condition: str | None,
) -> tuple[str, Decimal]:
    if not isinstance(pn, str) or not pn.strip():
        raise ReturnReceiptValidation("PN 不能为空")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise ReturnReceiptValidation("数量必须为正整数")
    if condition is not None and condition not in MANUAL_CONDITIONS:
        raise ReturnReceiptValidation(
            "件况仅支持 成品/坏品/废品，留空表示未填写"
        )
    return pn.strip(), Decimal(qty)


def register_receipt(
    db: Session,
    *,
    project_id: str,
    pn: str,
    qty: int,
    wbdd_no: str | None = None,
    part_id: int | None = None,
    description: str | None = None,
    condition: str | None = None,
    note: str | None = None,
    evidence_ref: str | None = None,
    occurred_at: datetime | None = None,
    idempotency_key: str | None = None,
    operated_by: str,
) -> dict:
    _get_project(db, project_id)
    pn_clean, qty_clean = _validate_manual_fields(
        pn=pn, qty=qty, condition=condition
    )
    source_order_id = (
        _resolve_source_order_id(db, wbdd_no.strip(), project_id)
        if wbdd_no
        else None
    )
    resolved_part_id = _resolve_part_id(db, pn_clean, part_id)
    if idempotency_key:
        digest = sha1(
            f"{project_id}:{idempotency_key}".encode("utf-8")
        ).hexdigest()
        source_ref = f"manual:{digest}"
        existing = db.execute(
            select(MaintenanceRkdReturnLine).where(
                MaintenanceRkdReturnLine.source_ref == source_ref
            )
        ).scalar_one_or_none()
        if existing is not None:
            return {"replayed": True, **_receipt_dict(existing)}
    else:
        source_ref = f"manual:{uuid4().hex}"
    row = MaintenanceRkdReturnLine(
        rkd_line_id=str(uuid4()),
        batch_id=None,
        head_row_id=None,
        project_id=project_id,
        source_order_id=source_order_id,
        source="manual",
        head_no="手工登记",
        source_ref=source_ref,
        part_id=resolved_part_id,
        pn=pn_clean,
        description=(description.strip() or None) if description else None,
        qty=qty_clean,
        test_result=condition,
        note=(note.strip() or None) if note else None,
        evidence_ref=(evidence_ref.strip() or None) if evidence_ref else None,
        occurred_at=occurred_at or _now(),
        line_status="active",
        version=1,
        created_by=operated_by,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:  # 并发同幂等键
        db.rollback()
        existing = db.execute(
            select(MaintenanceRkdReturnLine).where(
                MaintenanceRkdReturnLine.source_ref == source_ref
            )
        ).scalar_one_or_none()
        if existing is not None:
            return {"replayed": True, **_receipt_dict(existing)}
        raise ReturnReceiptConflict("返还登记写入冲突，请重试") from exc
    _audit(
        db,
        project_id=project_id,
        entity_id=row.rkd_line_id,
        action="create",
        before=None,
        after=_receipt_dict(row),
        reason=note.strip() if note and note.strip() else "登记返还收货",
        operated_by=operated_by,
    )
    db.flush()
    return {"replayed": False, **_receipt_dict(row)}


def _load_receipt(db: Session, receipt_id: str) -> MaintenanceRkdReturnLine:
    row = db.get(MaintenanceRkdReturnLine, receipt_id)
    if row is None:
        raise ReturnReceiptNotFound("返还记录不存在")
    return row


def _order_no(db: Session, source_order_id: str | None) -> str | None:
    if source_order_id is None:
        return None
    return db.scalar(
        select(FMaintenanceOrder.order_no).where(
            FMaintenanceOrder.raw_order_id == source_order_id
        )
    )


def update_receipt(
    db: Session,
    *,
    receipt_id: str,
    expected_version: int,
    updates: dict,
    reason: str,
    operated_by: str,
) -> dict:
    if not isinstance(reason, str) or not reason.strip():
        raise ReturnReceiptValidation("必须填写修改原因")
    unknown = set(updates) - ALLOWED_UPDATE_FIELDS
    if unknown:
        raise ReturnReceiptValidation(f"不支持的修改字段：{sorted(unknown)}")
    row = _load_receipt(db, receipt_id)
    if row.line_status != "active":
        raise ReturnReceiptConflict("已作废的返还记录不能修改")
    if row.version != expected_version:
        raise ReturnReceiptConflict(
            "版本冲突：该记录已被他人修改，请刷新后重试"
        )
    old_project_id = row.project_id
    before = _receipt_dict(row, _order_no(db, row.source_order_id))

    new_project_id = updates.get("project_id", row.project_id)
    if "project_id" in updates and new_project_id != old_project_id:
        _get_project(db, new_project_id)
    if "qty" in updates:
        _validate_manual_fields(pn=updates.get("pn", row.pn), qty=updates["qty"],
                                condition=None)
        row.qty = Decimal(updates["qty"])
    if "pn" in updates:
        pn_clean, _ = _validate_manual_fields(
            pn=updates["pn"], qty=1, condition=None
        )
        row.pn = pn_clean
        if "part_id" not in updates:
            row.part_id = _resolve_part_id(db, pn_clean, None)
    if "part_id" in updates:
        row.part_id = updates["part_id"]
    if "condition" in updates:
        condition = updates["condition"]
        if condition is not None and condition not in MANUAL_CONDITIONS:
            raise ReturnReceiptValidation("件况仅支持 成品/坏品/废品，留空表示未填写")
        row.test_result = condition
    if "wbdd_no" in updates:
        wbdd_no = updates["wbdd_no"]
        row.source_order_id = (
            _resolve_source_order_id(db, wbdd_no.strip(), new_project_id)
            if wbdd_no
            else None
        )
    if "description" in updates:
        row.description = updates["description"]
    if "note" in updates:
        row.note = updates["note"]
    if "evidence_ref" in updates:
        row.evidence_ref = updates["evidence_ref"]
    if "occurred_at" in updates:
        row.occurred_at = updates["occurred_at"]
    if "project_id" in updates:
        row.project_id = new_project_id
    row.version += 1
    row.updated_by = operated_by
    row.updated_at = _now()
    after = _receipt_dict(row, _order_no(db, row.source_order_id))
    _audit(
        db,
        project_id=row.project_id,
        entity_id=row.rkd_line_id,
        action="update",
        before=before,
        after=after,
        reason=reason.strip(),
        operated_by=operated_by,
    )
    if new_project_id != old_project_id:
        _audit(
            db,
            project_id=old_project_id,
            entity_id=row.rkd_line_id,
            action="transfer_out",
            before=before,
            after=after,
            reason=reason.strip(),
            operated_by=operated_by,
        )
    db.flush()
    return after


def void_receipt(
    db: Session,
    *,
    receipt_id: str,
    expected_version: int,
    reason: str,
    operated_by: str,
) -> dict:
    if not isinstance(reason, str) or not reason.strip():
        raise ReturnReceiptValidation("必须填写作废原因")
    row = _load_receipt(db, receipt_id)
    if row.line_status != "active":
        raise ReturnReceiptConflict("该返还记录已作废")
    if row.version != expected_version:
        raise ReturnReceiptConflict(
            "版本冲突：该记录已被他人修改，请刷新后重试"
        )
    before = _receipt_dict(row, _order_no(db, row.source_order_id))
    row.line_status = "voided"
    row.version += 1
    row.updated_by = operated_by
    row.updated_at = _now()
    row.voided_by = operated_by
    row.voided_at = _now()
    row.void_reason = reason.strip()[:256]
    after = _receipt_dict(row)
    _audit(
        db,
        project_id=row.project_id,
        entity_id=row.rkd_line_id,
        action="void",
        before=before,
        after=after,
        reason=reason.strip(),
        operated_by=operated_by,
    )
    db.flush()
    return after


def search_receipts(
    db: Session,
    *,
    project_id: str,
    page: int = 1,
    page_size: int = 50,
    line_status: str = "active",
    q: str | None = None,
    source_order_id: str | None = None,
    source: str | None = None,
) -> dict:
    _get_project(db, project_id)
    stmt = (
        select(MaintenanceRkdReturnLine, FMaintenanceOrder.order_no)
        .outerjoin(
            FMaintenanceOrder,
            FMaintenanceOrder.raw_order_id
            == MaintenanceRkdReturnLine.source_order_id,
        )
        .where(MaintenanceRkdReturnLine.project_id == project_id)
    )
    if line_status in ("active", "voided"):
        stmt = stmt.where(MaintenanceRkdReturnLine.line_status == line_status)
    if source_order_id is not None:
        stmt = stmt.where(
            MaintenanceRkdReturnLine.source_order_id == source_order_id
        )
    if source is not None:
        stmt = stmt.where(MaintenanceRkdReturnLine.source == source)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            MaintenanceRkdReturnLine.pn.ilike(like)
            | MaintenanceRkdReturnLine.evidence_ref.ilike(like)
            | MaintenanceRkdReturnLine.note.ilike(like)
        )
    total = db.scalar(
        select(func.count()).select_from(stmt.subquery())
    )
    rows = db.execute(
        stmt.order_by(
            MaintenanceRkdReturnLine.occurred_at.desc().nulls_last(),
            MaintenanceRkdReturnLine.created_at.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            _receipt_dict(row, order_no) for row, order_no in rows
        ],
    }


def receipt_summary(db: Session, *, project_id: str) -> dict:
    _get_project(db, project_id)
    rows = db.execute(
        select(
            MaintenanceRkdReturnLine.source_order_id,
            func.sum(MaintenanceRkdReturnLine.qty),
        )
        .where(
            MaintenanceRkdReturnLine.project_id == project_id,
            MaintenanceRkdReturnLine.line_status == "active",
        )
        .group_by(MaintenanceRkdReturnLine.source_order_id)
    ).all()
    project_total = Decimal("0")
    unassigned = Decimal("0")
    by_demand: list[dict] = []
    for source_order_id, qty in rows:
        qty = Decimal(qty)
        project_total += qty
        if source_order_id is None:
            unassigned += qty
            continue
        by_demand.append(
            {
                "source_order_id": source_order_id,
                "order_no": _order_no(db, source_order_id),
                "qty": _qty(qty),
            }
        )
    # 汇总不变式：Σ(需求单) + 未关联 = 项目总量（机器可查，防重复/漏计）。
    demand_sum = sum(
        (Decimal(item["qty"]) for item in by_demand), Decimal("0")
    )
    assert demand_sum + unassigned == project_total, "返还汇总不变式被破坏"
    return {
        "project_id": project_id,
        "project_total_qty": _qty(project_total),
        "unassigned_qty": _qty(unassigned),
        "by_demand": by_demand,
    }


def receipt_audit(db: Session, *, receipt_id: str) -> list[dict]:
    _load_receipt(db, receipt_id)
    rows = db.execute(
        select(MaintenanceProjectOperationAudit)
        .where(
            MaintenanceProjectOperationAudit.entity_type
            == _AUDIT_ENTITY_TYPE,
            MaintenanceProjectOperationAudit.entity_id == receipt_id,
        )
        .order_by(MaintenanceProjectOperationAudit.operated_at)
    ).scalars().all()
    return [
        {
            "id": row.id,
            "project_id": row.project_id,
            "entity_id": row.entity_id,
            "action": row.action,
            "before_json": row.before_json,
            "after_json": row.after_json,
            "reason": row.reason,
            "operated_by": row.operated_by,
            "operated_at": row.operated_at.isoformat() if row.operated_at else None,
        }
        for row in rows
    ]
