"""统一返还收货台账（2026-09-11 拍板口径）。

页面手工登记与（后续）RKD 入库导入共用 maintenance_rkd_return_line 一张台账：
- 归属：project 必填；source_order_id 可空（未关联需求单，补选只改归属不重复计数）。
- 统计：项目总量 = Σ(active)，需求单量 = Σ(active AND source_order_id=X)，
  未关联 = Σ(active AND source_order_id IS NULL)；三个数是同一批行的不同视角。
- 手工登记即视为已收到返件；数量限正整数（§5.2 拍板）；件况不影响数量。
- 修改/作废走版本 CAS + maintenance_project_operation_audit 留痕（前后值、原因、操作人）。
- 旧坏件口径按导入来源、坏品件况和有效状态筛选；修改导入事实会同步改变
  旧读模型的结果，手工来源不会进入旧指标，前置库账本不受影响。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha1
from uuid import uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.config import DATA_CHANGE_ADVISORY_LOCK_KEY, RKD_RETURN_CATEGORIES, RKD_RETURN_TEST_RESULTS
from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_doc_import import MaintenanceDocLineRow, MaintenanceRkdReturnLine
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceProjectOperationAudit,
)
from app.models.maintenance_source_assignment import (
    MaintenanceSourceOrderAssignment,
)
from app.services.query_filters import active_beta_maintenance_orders

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
    """旧坏件口径（D-14 分子）：只看 rkd_import 来源 + 坏品类件况 + 有效行。

    台账统一后本表会出现手工行（source='manual'）与「件况全收」导入行
    （成品也入账）；旧口径消费方（返还率 / boss 坏件回收 / PN 分析 / 回收清单）
    保留原指标定义（先并存后切换，计划 §3.4）。这不是历史快照：被更正的
    导入数量、件况、项目和作废状态会反映在这些实时读模型中。
    """
    return (
        MaintenanceRkdReturnLine.source == "rkd_import",
        MaintenanceRkdReturnLine.test_result.in_(RKD_RETURN_TEST_RESULTS),
        MaintenanceRkdReturnLine.line_status == "active",
        # Historical rows already passed the old importer's category allowlist.
        # New source evidence must explicitly retain that same category scope.
        or_(MaintenanceRkdReturnLine.source_payload.is_(None),
            MaintenanceRkdReturnLine.source_payload["category"].astext.in_(RKD_RETURN_CATEGORIES)),
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
        "receipt_kind": row.receipt_kind,
        "review_required": row.review_required,
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


def lock_receipt_context(db: Session) -> None:
    """Order/identity writers take this lock exclusively, before any fact lock.

    Receipt commands share it, so independent receipts still run concurrently.
    Hold it through authorization, validation and commit to prevent reassign,
    tombstone or project merge from changing the facts being authorized.
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock_shared(:key)"),
        {"key": DATA_CHANGE_ADVISORY_LOCK_KEY},
    )


def lock_receipt_projects(db: Session, project_ids: set[str]) -> None:
    """Keep master fields and manager/viewer scope stable until receipt commit.

    Project writers take state -> project -> assignment locks. Receipt commands
    never acquire workbook state, and acquire these shared project locks before
    receipt rows, so scope writers cannot revoke an already authorized write.
    """
    db.execute(
        select(MaintenanceProject.project_id)
        .where(MaintenanceProject.project_id.in_(project_ids))
        .order_by(MaintenanceProject.project_id)
        .with_for_update(read=True)
    ).all()


def _get_project(db: Session, project_id: str, *, lock: bool = False) -> MaintenanceProject:
    stmt = select(MaintenanceProject).where(MaintenanceProject.project_id == project_id)
    if lock:
        stmt = stmt.with_for_update(read=True).execution_options(populate_existing=True)
    project = db.scalar(stmt)
    if project is None:
        raise ReturnReceiptNotFound("维保项目不存在")
    if lock and not project.is_active:
        raise ReturnReceiptValidation("维保项目已停用，不能登记或修改返还")
    return project


def _resolve_part_id(db: Session, pn: str, part_id: int | None) -> int | None:
    if part_id is not None:
        part = db.get(DimPart, part_id, with_for_update={"read": True}, populate_existing=True)
        if part is None or part.status != "active" or part.pn_std != pn:
            raise ReturnReceiptValidation("所选备件与 PN 不一致，请重新选择")
        return part_id
    return db.scalar(select(DimPart.id).where(
        DimPart.pn_std == pn, DimPart.status == "active",
    ).with_for_update(read=True))


def _resolve_source_order_id(
    db: Session, wbdd_no: str, project_id: str
) -> str:
    orders = db.scalars(active_beta_maintenance_orders(
        select(FMaintenanceOrder).where(
            FMaintenanceOrder.order_no == wbdd_no,
            FMaintenanceOrder.data_status == "已生效",
        ).with_for_update(read=True).execution_options(populate_existing=True),
        FMaintenanceOrder,
    )).all()
    if len(orders) != 1:
        raise ReturnReceiptValidation("维保需求单不存在、已失效或单号不唯一")
    order = orders[0]
    assignment = db.execute(
        select(MaintenanceSourceOrderAssignment)
        .where(
            MaintenanceSourceOrderAssignment.source_order_id == order.raw_order_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        ).with_for_update(read=True).execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if assignment is None:
        raise ReturnReceiptValidation(f"维保需求单尚未归属项目：{wbdd_no}")
    if assignment.project_id != project_id:
        raise ReturnReceiptValidation(
            f"维保需求单 {wbdd_no} 已归属其他项目，与当前项目冲突"
        )
    return order.raw_order_id


def _optional_text(value: object, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ReturnReceiptValidation(f"{name}格式或长度不合法")
    return value.strip() or None


def _occurred_at(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ReturnReceiptValidation("收货时间格式不合法")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _validate_manual_fields(
    *,
    pn: str | None,
    qty: object,
    condition: str | None,
) -> tuple[str, Decimal]:
    if not isinstance(pn, str) or not pn.strip() or len(pn) > 128:
        raise ReturnReceiptValidation("PN 不能为空")
    if isinstance(qty, bool) or not isinstance(qty, int) or not 0 < qty < 10**11:
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
    lock_receipt_context(db)
    _get_project(db, project_id, lock=True)
    pn_clean, qty_clean = _validate_manual_fields(
        pn=pn, qty=qty, condition=condition
    )
    source_order_id = (
        _resolve_source_order_id(db, wbdd_no.strip(), project_id)
        if wbdd_no
        else None
    )
    resolved_part_id = _resolve_part_id(db, pn_clean, part_id)
    description = _optional_text(description, "描述", 256)
    note = _optional_text(note, "备注", 512)
    evidence_ref = _optional_text(evidence_ref, "凭证", 128)
    occurred_at = _occurred_at(occurred_at)
    idempotency_key = _optional_text(idempotency_key, "幂等键", 128)
    request_values = {
        "project_id": project_id, "source_order_id": source_order_id,
        "pn": pn_clean, "part_id": resolved_part_id, "qty": _qty(qty_clean),
        "description": (description.strip() or None) if description else None,
        "condition": condition, "note": (note.strip() or None) if note else None,
        "evidence_ref": (evidence_ref.strip() or None) if evidence_ref else None,
    }
    request_values["occurred_at"] = occurred_at.isoformat() if occurred_at else None
    if idempotency_key:
        digest = sha1(
            f"{project_id}:{idempotency_key}".encode("utf-8")
        ).hexdigest()
        source_ref = f"manual:{digest}"
        # Serialize only retries of the same command. Different registrations
        # proceed independently; transaction ownership stays with the caller.
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                   {"key": source_ref})
        existing = db.execute(
            select(MaintenanceRkdReturnLine).where(
                MaintenanceRkdReturnLine.source_ref == source_ref
            ).with_for_update().execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if existing is not None:
            original = db.scalar(select(MaintenanceProjectOperationAudit.after_json)
                .where(MaintenanceProjectOperationAudit.entity_type == _AUDIT_ENTITY_TYPE,
                       MaintenanceProjectOperationAudit.entity_id == existing.rkd_line_id,
                       MaintenanceProjectOperationAudit.action == "create")
                .order_by(MaintenanceProjectOperationAudit.id).limit(1))
            # Persist the command separately from the resulting timestamp: an
            # omitted date is different from a later explicit date on retry.
            original_request = original.get("request") if original else None
            if original_request is None and original is not None:
                original_request = {k: original.get(k) for k in request_values}
                if occurred_at is None:
                    original_request["occurred_at"] = None
            if original_request != request_values:
                raise ReturnReceiptConflict("同一登记请求的内容已变化，请核对后重新登记")
            if existing.project_id != project_id or existing.line_status != "active":
                raise ReturnReceiptConflict("原登记已转移或作废，不可重复提交恢复")
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
    db.flush()
    _audit(
        db,
        project_id=project_id,
        entity_id=row.rkd_line_id,
        action="create",
        before=None,
        after={**_receipt_dict(row), "request": request_values},
        reason=note.strip() if note and note.strip() else "登记返还收货",
        operated_by=operated_by,
    )
    db.flush()
    return {"replayed": False, **_receipt_dict(row)}


def _load_receipt(
    db: Session, receipt_id: str, *, lock: bool = False,
    target_project_id: str | None = None,
) -> MaintenanceRkdReturnLine:
    if lock:
        project_id = db.scalar(select(MaintenanceRkdReturnLine.project_id).where(
            MaintenanceRkdReturnLine.rkd_line_id == receipt_id,
        ))
        if project_id is None:
            raise ReturnReceiptNotFound("返还记录不存在")
        lock_receipt_projects(db, {project_id, target_project_id} if target_project_id else {project_id})
    row = db.get(MaintenanceRkdReturnLine, receipt_id,
                 with_for_update=lock, populate_existing=lock)
    if row is None:
        raise ReturnReceiptNotFound("返还记录不存在")
    if lock and row.project_id != project_id:
        raise ReturnReceiptConflict("返还记录归属已变化，请刷新后重试")
    return row


def _order_no(db: Session, source_order_id: str | None) -> str | None:
    if source_order_id is None:
        return None
    return db.scalar(
        select(FMaintenanceOrder.order_no).where(
            FMaintenanceOrder.raw_order_id == source_order_id
        )
    )


def demand_candidates(
    db: Session, *, project_id: str, page: int, page_size: int, q: str | None = None,
) -> dict:
    """Receipt selectors expose only effective WBDD identities in this project."""
    _get_project(db, project_id)
    stmt = active_beta_maintenance_orders(
        select(FMaintenanceOrder.raw_order_id, FMaintenanceOrder.order_no,
               FMaintenanceOrder.order_date)
        .join(MaintenanceSourceOrderAssignment,
              MaintenanceSourceOrderAssignment.source_order_id == FMaintenanceOrder.raw_order_id)
        .where(MaintenanceSourceOrderAssignment.project_id == project_id,
               MaintenanceSourceOrderAssignment.is_active.is_(True),
               FMaintenanceOrder.data_status == "已生效"),
        FMaintenanceOrder,
    )
    if q and q.strip():
        stmt = stmt.where(FMaintenanceOrder.order_no.icontains(q.strip(), autoescape=True))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(FMaintenanceOrder.order_no, FMaintenanceOrder.raw_order_id)
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return {"rows": [{"source_order_id": row.raw_order_id, "order_no": row.order_no,
                       "order_date": row.order_date.isoformat() if row.order_date else None}
                      for row in rows],
            "total": total, "page": page, "page_size": page_size}


def update_receipt(
    db: Session,
    *,
    receipt_id: str,
    expected_version: int,
    updates: dict,
    reason: str,
    operated_by: str,
) -> dict:
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
        raise ReturnReceiptValidation("必须填写修改原因")
    unknown = set(updates) - ALLOWED_UPDATE_FIELDS
    if unknown:
        raise ReturnReceiptValidation(f"不支持的修改字段：{sorted(unknown)}")
    if "project_id" in updates and (
        not isinstance(updates["project_id"], str) or not updates["project_id"]
    ):
        raise ReturnReceiptValidation("项目不能为空")
    lock_receipt_context(db)
    row = _load_receipt(db, receipt_id, lock=True, target_project_id=updates.get("project_id"))
    if row.line_status != "active":
        raise ReturnReceiptConflict("已作废的返还记录不能修改")
    if row.version != expected_version:
        raise ReturnReceiptConflict("版本冲突：该记录已被他人修改，请刷新后重试")
    old_project_id = row.project_id
    new_project_id = updates.get("project_id", old_project_id)
    if not isinstance(new_project_id, str) or not new_project_id:
        raise ReturnReceiptValidation("项目不能为空")
    for project_id in sorted({old_project_id, new_project_id}):
        _get_project(db, project_id, lock=True)

    # Build a fully validated final state before mutating any ORM attribute.
    # A caller may catch the validation error and commit unrelated work, or
    # use an autoflush Session; neither may persist a partial receipt edit.
    values = {"project_id": new_project_id}
    pn = updates.get("pn", row.pn)
    if "pn" in updates:
        pn, _ = _validate_manual_fields(pn=pn, qty=1, condition=None)
        values["pn"] = pn
    if "qty" in updates:
        _, qty = _validate_manual_fields(pn=pn, qty=updates["qty"], condition=None)
        if row.receipt_kind == "machine" and qty != 1:
            raise ReturnReceiptValidation("整机返还固定记 1 台，组成明细不单独计数")
        values["qty"] = qty
        values["review_required"] = False
    if "part_id" in updates:
        values["part_id"] = (
            _resolve_part_id(db, pn, updates["part_id"])
            if updates["part_id"] is not None else None
        )
    elif "pn" in updates:
        values["part_id"] = _resolve_part_id(db, pn, None)
    if "condition" in updates:
        condition = updates["condition"]
        if condition is not None and condition not in MANUAL_CONDITIONS:
            raise ReturnReceiptValidation("件况仅支持 成品/坏品/废品，留空表示未填写")
        values["test_result"] = condition
    if "wbdd_no" in updates:
        wbdd_no = _optional_text(updates["wbdd_no"], "需求单", 64)
        values["source_order_id"] = (
            _resolve_source_order_id(db, wbdd_no, new_project_id) if wbdd_no else None
        )
    elif new_project_id != old_project_id and row.source_order_id is not None:
        raise ReturnReceiptValidation("转移项目时请重新选择或明确清空维保需求单")
    elif row.source_order_id is not None:
        # Even a note-only edit must not silently retain a now-invalid link.
        current_order_no = _order_no(db, row.source_order_id)
        if not current_order_no or _resolve_source_order_id(
            db, current_order_no, new_project_id
        ) != row.source_order_id:
            raise ReturnReceiptValidation("维保需求单归属已变化，请重新选择或明确清空")
    for key, limit in (("description", 256), ("note", 512), ("evidence_ref", 128)):
        if key in updates:
            values[key] = _optional_text(updates[key], key, limit)
    if "occurred_at" in updates:
        values["occurred_at"] = _occurred_at(updates["occurred_at"])
    before = _receipt_dict(row, _order_no(db, row.source_order_id))
    after_order_no = _order_no(db, values.get("source_order_id", row.source_order_id))
    for key, value in values.items():
        setattr(row, key, value)
    row.version += 1
    row.updated_by = operated_by
    row.updated_at = _now()
    after = _receipt_dict(row, after_order_no)
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
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 256:
        raise ReturnReceiptValidation("必须填写作废原因")
    lock_receipt_context(db)
    row = _load_receipt(db, receipt_id, lock=True)
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
    unassigned: bool = False,
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
    if unassigned:
        stmt = stmt.where(MaintenanceRkdReturnLine.source_order_id.is_(None))
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
    # Only read evidence for authorized receipts on this page. A source correction
    # moves head_row_id to its new batch; old raw rows remain audit evidence.
    head_ids = {
        row.head_row_id for row, _ in rows
        if row.receipt_kind == "machine" and row.head_row_id is not None
    }
    components: dict[str, list[dict]] = {head_id: [] for head_id in head_ids}
    if head_ids:
        for detail in db.scalars(
            select(MaintenanceDocLineRow)
            .where(MaintenanceDocLineRow.head_row_id.in_(head_ids))
            .order_by(MaintenanceDocLineRow.row_no, MaintenanceDocLineRow.row_id)
        ):
            raw = detail.raw_json
            components[detail.head_row_id].append({
                "row_id": detail.row_id,
                "pn": raw.get("备件明细.备件PN") or None,
                "description": raw.get("备件明细.备件描述") or None,
                "qty": raw.get("备件明细.入库数量"),
                "condition": raw.get("备件明细.测试结果") or None,
            })
    items = []
    for row, order_no in rows:
        item = _receipt_dict(row, order_no)
        if row.receipt_kind == "machine":
            item["components"] = components.get(row.head_row_id, [])
        items.append(item)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
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
