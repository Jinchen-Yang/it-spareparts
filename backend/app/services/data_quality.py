"""行级数据疑点的唯一写入口与查询装配（DEV-05A）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.data_quality import FactDataQualityIssue
from app.models.dimensions import DimPart
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysAuditLog, SysImportBatch

_SIDES = {"purchase", "sales"}
_DECISIONS = {"confirmed_valid", "confirmed_source_error"}
_REOPENABLE = {*_DECISIONS, "source_changed"}


class DataQualityError(Exception):
    """疑点领域异常基类。"""


class DataQualityValidationError(DataQualityError):
    pass


class DataQualityNotFoundError(DataQualityError):
    pass


class DataQualityConflictError(DataQualityError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _required(value: str | None, label: str) -> str:
    text = (value or "").strip()
    if not text:
        raise DataQualityValidationError(f"{label}不能为空")
    return text


def _source_line(db: Session, side: str, line_id: int):
    if side not in _SIDES:
        raise DataQualityValidationError("side 只能是 purchase 或 sales")
    model = FPurchaseLine if side == "purchase" else FSalesLine
    row = db.get(model, line_id)
    if row is None:
        raise DataQualityNotFoundError("对应事实行不存在")
    return row


def _audit_snapshot(issue: FactDataQualityIssue) -> dict[str, Any]:
    return {
        "id": issue.id,
        "side": issue.side,
        "line_id": issue.line_id,
        "part_id": issue.part_id,
        "import_batch_id": issue.import_batch_id,
        "rule_code": issue.rule_code,
        "rule_version": issue.rule_version,
        "evidence": issue.evidence,
        "source_fingerprint": issue.source_fingerprint,
        "status": issue.status,
        "detected_by": issue.detected_by,
        "reviewed_by": issue.reviewed_by,
        "review_note": issue.review_note,
        "version": issue.version,
    }


def _add_audit(db: Session, issue: FactDataQualityIssue, *, action: str,
               before: dict | None, operated_by: str, reason: str | None = None) -> None:
    db.add(SysAuditLog(
        entity_type="data_quality_issue",
        entity_id=issue.id,
        action=action,
        before_json=before,
        after_json=_audit_snapshot(issue),
        reason=reason,
        operated_by=operated_by,
    ))


def _issue_dict(issue: FactDataQualityIssue) -> dict[str, Any]:
    return {
        "id": issue.id,
        "side": issue.side,
        "line_id": issue.line_id,
        "part_id": issue.part_id,
        "import_batch_id": issue.import_batch_id,
        "rule_code": issue.rule_code,
        "rule_version": issue.rule_version,
        "evidence": issue.evidence,
        "source_fingerprint": issue.source_fingerprint,
        "status": issue.status,
        "detected_by": issue.detected_by,
        "detected_at": issue.detected_at,
        "reviewed_by": issue.reviewed_by,
        "reviewed_at": issue.reviewed_at,
        "review_note": issue.review_note,
        "version": issue.version,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
    }


def create_or_refresh_issue(
    db: Session, *, side: str, line_id: int, rule_code: str, rule_version: str,
    evidence: dict, source_fingerprint: str, detected_by: str,
    commit: bool = True,
) -> dict:
    """内部检测器写入口；重复信号幂等，源变化会使既有结论失效。"""
    rule_code = _required(rule_code, "rule_code")
    rule_version = _required(rule_version, "rule_version")
    source_fingerprint = _required(source_fingerprint, "source_fingerprint")
    detected_by = _required(detected_by, "detected_by")
    if not isinstance(evidence, dict):
        raise DataQualityValidationError("evidence 必须是对象")
    source = _source_line(db, side, line_id)

    # 同一当前问题并发创建/刷新串行化，避免先查后插的唯一键竞态。
    lock_key = f"data-quality:{side}:{line_id}:{rule_code}"
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_key))))
    issue = db.scalar(select(FactDataQualityIssue).where(
        FactDataQualityIssue.side == side,
        FactDataQualityIssue.line_id == line_id,
        FactDataQualityIssue.rule_code == rule_code,
    ).with_for_update())
    if issue is None:
        issue = FactDataQualityIssue(
            side=side, line_id=line_id, part_id=source.part_id,
            import_batch_id=source.import_batch_id, rule_code=rule_code,
            rule_version=rule_version, evidence=evidence,
            source_fingerprint=source_fingerprint, status="open",
            detected_by=detected_by, version=1,
        )
        db.add(issue)
        db.flush()
        _add_audit(db, issue, action="create", before=None, operated_by=detected_by)
        if commit:
            db.commit()
        else:
            db.flush()
        db.refresh(issue)
        return _issue_dict(issue)

    changed = any((
        issue.part_id != source.part_id,
        issue.rule_version != rule_version,
        issue.evidence != evidence,
        issue.source_fingerprint != source_fingerprint,
    ))
    if not changed:
        return _issue_dict(issue)

    before = _audit_snapshot(issue)
    fingerprint_changed = issue.source_fingerprint != source_fingerprint
    issue.part_id = source.part_id
    issue.import_batch_id = source.import_batch_id
    issue.rule_version = rule_version
    issue.evidence = evidence
    issue.source_fingerprint = source_fingerprint
    issue.detected_by = detected_by
    issue.detected_at = _now()
    if fingerprint_changed and issue.status in _DECISIONS:
        issue.status = "source_changed"
    issue.version += 1
    issue.updated_at = _now()
    _add_audit(db, issue, action="refresh", before=before, operated_by=detected_by)
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(issue)
    return _issue_dict(issue)


def mark_issue_source_changed(
    db: Session, *, side: str, line_id: int, rule_code: str, rule_version: str,
    evidence: dict, source_fingerprint: str, detected_by: str,
    commit: bool = True,
) -> dict | None:
    """让不再命中规则的既有疑点失效；没有既有疑点时不新建。"""
    rule_code = _required(rule_code, "rule_code")
    rule_version = _required(rule_version, "rule_version")
    source_fingerprint = _required(source_fingerprint, "source_fingerprint")
    detected_by = _required(detected_by, "detected_by")
    if not isinstance(evidence, dict):
        raise DataQualityValidationError("evidence 必须是对象")
    source = _source_line(db, side, line_id)
    lock_key = f"data-quality:{side}:{line_id}:{rule_code}"
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_key))))
    issue = db.scalar(select(FactDataQualityIssue).where(
        FactDataQualityIssue.side == side,
        FactDataQualityIssue.line_id == line_id,
        FactDataQualityIssue.rule_code == rule_code,
    ).with_for_update())
    if issue is None:
        return None
    changed = any((
        issue.part_id != source.part_id,
        issue.rule_version != rule_version,
        issue.evidence != evidence,
        issue.source_fingerprint != source_fingerprint,
        issue.status != "source_changed",
    ))
    if not changed:
        return _issue_dict(issue)
    before = _audit_snapshot(issue)
    issue.part_id = source.part_id
    issue.import_batch_id = source.import_batch_id
    issue.rule_version = rule_version
    issue.evidence = evidence
    issue.source_fingerprint = source_fingerprint
    issue.status = "source_changed"
    issue.detected_by = detected_by
    issue.detected_at = _now()
    issue.version += 1
    issue.updated_at = _now()
    _add_audit(
        db, issue, action="source_changed", before=before, operated_by=detected_by,
        reason="源数据变化后当前已不再命中金额疑点规则",
    )
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(issue)
    return _issue_dict(issue)


def _locked_issue(
    db: Session, issue_id: int, *, allow_sales: bool = True,
) -> FactDataQualityIssue:
    issue = db.scalar(select(FactDataQualityIssue).where(
        FactDataQualityIssue.id == issue_id,
    ).with_for_update())
    if issue is None:
        raise DataQualityNotFoundError("疑点不存在")
    if not allow_sales and issue.side == "sales":
        # own_customers_only 销售角色不能看任何逐单销售明细；写端点也必须
        # 以 404 失败关闭，避免通过枚举 issue_id 反推销售疑点存在。
        raise DataQualityNotFoundError("疑点不存在")
    return issue


def decide_issue(db: Session, *, issue_id: int, decision: str, version: int,
                 note: str, operated_by: str, allow_sales: bool = True) -> dict:
    note = _required(note, "核实原因")
    operated_by = _required(operated_by, "核实账号")
    if decision not in _DECISIONS:
        raise DataQualityValidationError("decision 非法")
    issue = _locked_issue(db, issue_id, allow_sales=allow_sales)
    if issue.version != version:
        raise DataQualityConflictError("记录已被他人更新，请刷新后重试")
    if issue.status != "open":
        raise DataQualityConflictError("当前状态不能提交核实结论")
    before = _audit_snapshot(issue)
    issue.status = decision
    issue.reviewed_by = operated_by
    issue.reviewed_at = _now()
    issue.review_note = note
    issue.version += 1
    issue.updated_at = _now()
    _add_audit(db, issue, action="decision", before=before,
               operated_by=operated_by, reason=note)
    db.commit()
    db.refresh(issue)
    return _issue_dict(issue)


def reopen_issue(db: Session, *, issue_id: int, version: int, note: str,
                 operated_by: str, allow_sales: bool = True) -> dict:
    note = _required(note, "重新打开原因")
    operated_by = _required(operated_by, "核实账号")
    issue = _locked_issue(db, issue_id, allow_sales=allow_sales)
    if issue.version != version:
        raise DataQualityConflictError("记录已被他人更新，请刷新后重试")
    if issue.status not in _REOPENABLE:
        raise DataQualityConflictError("当前状态不能重新打开")
    before = _audit_snapshot(issue)
    issue.status = "open"
    issue.reviewed_by = operated_by
    issue.reviewed_at = _now()
    issue.review_note = note
    issue.version += 1
    issue.updated_at = _now()
    _add_audit(db, issue, action="reopen", before=before,
               operated_by=operated_by, reason=note)
    db.commit()
    db.refresh(issue)
    return _issue_dict(issue)


def _batch_dict(batch: SysImportBatch | None) -> dict | None:
    if batch is None:
        return None
    return {
        "id": batch.id, "filename": batch.filename, "file_type": batch.file_type,
        "uploaded_by": batch.uploaded_by, "uploaded_at": batch.uploaded_at,
    }


def _fact_map(db: Session, issues: list[FactDataQualityIssue]) -> dict[int, dict]:
    """两条批量查询装配采购/销售事实摘要，避免队列 N+1。"""
    result: dict[int, dict] = {}
    purchase: dict[int, list[int]] = {}
    sales: dict[int, list[int]] = {}
    for issue in issues:
        target = purchase if issue.side == "purchase" else sales
        target.setdefault(issue.line_id, []).append(issue.id)
    if purchase:
        rows = db.execute(
            select(FPurchaseLine, FPurchaseOrder, DimPart, SysImportBatch)
            .join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id)
            .join(DimPart, DimPart.id == FPurchaseLine.part_id)
            .outerjoin(SysImportBatch, SysImportBatch.id == FPurchaseLine.import_batch_id)
            .where(FPurchaseLine.id.in_(purchase))
        ).all()
        for line, order, part, batch in rows:
            fact = {
                "order_id": order.id, "order_no": order.order_no,
                "order_date": order.order_date, "purchaser": order.purchaser,
                "salesperson": None, "part_id": part.id, "pn_std": part.pn_std,
                "description": line.description or part.description,
                "qty": line.qty, "unit": line.unit, "unit_price": line.unit_price,
                "line_amount": line.line_amount, "batch": _batch_dict(batch),
            }
            for issue_id in purchase[line.id]:
                result[issue_id] = fact
    if sales:
        rows = db.execute(
            select(FSalesLine, FSalesOrder, DimPart, SysImportBatch)
            .join(FSalesOrder, FSalesOrder.id == FSalesLine.order_id)
            .join(DimPart, DimPart.id == FSalesLine.part_id)
            .outerjoin(SysImportBatch, SysImportBatch.id == FSalesLine.import_batch_id)
            .where(FSalesLine.id.in_(sales))
        ).all()
        for line, order, part, batch in rows:
            fact = {
                "order_id": order.id, "order_no": order.order_no,
                "order_date": order.order_date, "purchaser": None,
                "salesperson": order.salesperson, "part_id": part.id,
                "pn_std": part.pn_std, "description": line.description or part.description,
                "qty": line.qty, "unit": line.unit, "unit_price": line.unit_price,
                "line_amount": line.line_amount, "batch": _batch_dict(batch),
            }
            for issue_id in sales[line.id]:
                result[issue_id] = fact
    return result


def _search_condition(q: str):
    like = f"%{q}%"
    purchase = select(FPurchaseLine.id).join(
        FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id
    ).join(DimPart, DimPart.id == FPurchaseLine.part_id).where(
        FPurchaseLine.id == FactDataQualityIssue.line_id,
        or_(DimPart.pn_std.ilike(like), FPurchaseOrder.order_no.ilike(like)),
    ).exists()
    sales = select(FSalesLine.id).join(
        FSalesOrder, FSalesOrder.id == FSalesLine.order_id
    ).join(DimPart, DimPart.id == FSalesLine.part_id).where(
        FSalesLine.id == FactDataQualityIssue.line_id,
        or_(DimPart.pn_std.ilike(like), FSalesOrder.order_no.ilike(like)),
    ).exists()
    return or_(
        and_(FactDataQualityIssue.side == "purchase", purchase),
        and_(FactDataQualityIssue.side == "sales", sales),
    )


def list_issues(db: Session, *, status: str | None = None, side: str | None = None,
                rule_code: str | None = None, q: str | None = None,
                page: int = 1, page_size: int = 20,
                allow_sales: bool = True) -> dict:
    stmt = select(FactDataQualityIssue)
    count_stmt = select(func.count()).select_from(FactDataQualityIssue)
    conditions = []
    if not allow_sales:
        conditions.append(FactDataQualityIssue.side != "sales")
    if status:
        conditions.append(FactDataQualityIssue.status == status)
    if side:
        if side not in _SIDES:
            raise DataQualityValidationError("side 只能是 purchase 或 sales")
        conditions.append(FactDataQualityIssue.side == side)
    if rule_code:
        conditions.append(FactDataQualityIssue.rule_code == rule_code)
    if q and q.strip():
        conditions.append(_search_condition(q.strip()))
    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)
    total = db.scalar(count_stmt) or 0
    issues = db.scalars(stmt.order_by(
        FactDataQualityIssue.updated_at.desc(), FactDataQualityIssue.id.desc(),
    ).offset((page - 1) * page_size).limit(page_size)).all()
    facts = _fact_map(db, list(issues))
    items = []
    for issue in issues:
        item = _issue_dict(issue)
        # 队列只用于定位与筛选：自由文本核实原因、原始证据与内部指纹只在
        # 详情接口按权限返回，避免清单响应成为脱敏旁路。
        for private_field in ("evidence", "source_fingerprint", "review_note"):
            item.pop(private_field, None)
        item["fact"] = facts.get(issue.id)
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_issue(db: Session, issue_id: int, *, allow_sales: bool = True) -> dict | None:
    issue = db.get(FactDataQualityIssue, issue_id)
    if issue is None or (not allow_sales and issue.side == "sales"):
        return None
    data = _issue_dict(issue)
    data["fact"] = _fact_map(db, [issue]).get(issue.id)
    audits = db.scalars(select(SysAuditLog).where(
        SysAuditLog.entity_type == "data_quality_issue",
        SysAuditLog.entity_id == issue.id,
    ).order_by(SysAuditLog.id.desc())).all()
    data["audit"] = [{
        "action": row.action, "before": row.before_json, "after": row.after_json,
        "reason": row.reason, "operated_by": row.operated_by,
        "operated_at": row.operated_at,
    } for row in audits]
    return data
