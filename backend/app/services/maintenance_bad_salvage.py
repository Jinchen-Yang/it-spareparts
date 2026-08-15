"""坏件变卖登记与贡献毛利（F5）。

写：登记/作废（实名 + 项目范围 + bad-return 管理权限，API 层把关）；
读：项目变卖清单 + 贡献毛利 = 变卖收入 − 领用含税成本（缺成本不按 0）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.maintenance_bad_salvage import MaintenanceBadSalvage
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)


class SalvageError(RuntimeError):
    """坏件变卖登记业务错误。"""


class SalvageConflict(RuntimeError):
    """重复、并发或状态冲突。"""


def _payload_digest(
    *, pn: str, qty: Decimal, revenue: Decimal, salvage_date: date
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "pn": pn,
                "qty": str(qty),
                "revenue": str(revenue),
                "salvage_date": salvage_date.isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _dict(row: MaintenanceBadSalvage, *, cost_basis: Decimal | None) -> dict:
    margin = (
        (row.revenue - cost_basis * row.qty).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if cost_basis is not None
        else None
    )
    return {
        "salvage_id": row.salvage_id,
        "pn": row.pn,
        "part_id": row.part_id,
        "qty": float(row.qty),
        "revenue": float(row.revenue),
        "salvage_date": row.salvage_date.isoformat(),
        "buyer_note": row.buyer_note,
        "reason": row.reason,
        "operated_by": row.operated_by,
        "is_active": row.is_active,
        "version": row.version,
        "cost_basis_inc_tax": float(cost_basis) if cost_basis is not None else None,
        "margin": float(margin) if margin is not None else None,
    }


def _latest_cost_basis(
    db: Session, *, project_id: str, part_id: int | None
) -> Decimal | None:
    if part_id is None:
        return None
    row = db.execute(
        select(MaintenanceSiteIssueLine.unit_cost_inc_tax)
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
        .where(
            MaintenanceSiteIssue.project_id == project_id,
            MaintenanceSiteIssueLine.part_id == part_id,
            MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
            MaintenanceSiteIssueLine.unit_cost_inc_tax.is_not(None),
        )
        .order_by(
            MaintenanceSiteIssue.issue_date.desc(),
            MaintenanceSiteIssueLine.updated_at.desc(),
        )
        .limit(1)
    ).first()
    return Decimal(row[0]) if row is not None else None


def register_salvage(
    db: Session,
    *,
    project_id: str,
    part_id: int | None,
    pn: str,
    qty: Decimal,
    revenue: Decimal,
    salvage_date: date,
    buyer_note: str | None,
    reason: str | None,
    idempotency_key: str | None,
    operated_by: str,
) -> dict | None:
    """登记一笔坏件变卖。幂等键同键重放返回既有记录，异内容失败关闭。"""
    project = db.get(MaintenanceProject, project_id)
    if project is None:
        return None
    pn = (pn or "").strip()
    if not pn:
        raise SalvageError("PN 不能为空")
    qty = Decimal(qty)
    revenue = Decimal(revenue)
    if not qty.is_finite() or qty <= 0 or qty >= Decimal("100000000000"):
        raise SalvageError("变卖数量超出允许范围")
    if not revenue.is_finite() or revenue < 0 or revenue >= Decimal("100000000000"):
        raise SalvageError("变卖收入超出允许范围")
    digest = _payload_digest(
        pn=pn, qty=qty, revenue=revenue, salvage_date=salvage_date
    )
    if idempotency_key:
        existing = db.execute(
            select(MaintenanceBadSalvage)
            .where(
                MaintenanceBadSalvage.project_id == project_id,
                MaintenanceBadSalvage.idempotency_key == idempotency_key,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            if existing.payload_digest != digest:
                raise SalvageConflict("同一幂等键对应不同变卖内容，拒绝重放")
            return _dict(
                existing, cost_basis=_latest_cost_basis(db, project_id=project_id, part_id=existing.part_id)
            )
    row = MaintenanceBadSalvage(
        salvage_id=str(uuid4()),
        project_id=project_id,
        part_id=part_id,
        pn=pn,
        qty=qty,
        revenue=revenue,
        salvage_date=salvage_date,
        buyer_note=(buyer_note or None),
        reason=reason,
        idempotency_key=idempotency_key,
        payload_digest=digest,
        operated_by=operated_by[:64],
    )
    db.add(row)
    db.flush()
    return _dict(
        row, cost_basis=_latest_cost_basis(db, project_id=project_id, part_id=part_id)
    )


def void_salvage(
    db: Session, *, salvage_id: str, operated_by: str, version: int
) -> dict:
    """作废一笔变卖登记（软作废，事实保留审计）。"""
    row = db.execute(
        select(MaintenanceBadSalvage)
        .where(MaintenanceBadSalvage.salvage_id == salvage_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise SalvageError("变卖登记不存在")
    if not row.is_active:
        raise SalvageConflict("变卖登记已作废")
    if row.version != version:
        raise SalvageConflict("变卖登记已被其他操作修改，请刷新后重试")
    row.is_active = False
    row.voided_by = operated_by[:64]
    row.voided_at = datetime.now(timezone.utc)
    row.version += 1
    db.flush()
    return _dict(
        row, cost_basis=_latest_cost_basis(db, project_id=row.project_id, part_id=row.part_id)
    )


def list_salvage(db: Session, project_id: str) -> dict:
    """项目坏件变卖清单 + 贡献毛利汇总（缺成本不按 0）。"""
    rows = db.execute(
        select(MaintenanceBadSalvage)
        .where(MaintenanceBadSalvage.project_id == project_id)
        .order_by(
            MaintenanceBadSalvage.salvage_date.desc(),
            MaintenanceBadSalvage.created_at.desc(),
        )
    ).scalars().all()
    payload = [
        _dict(
            row,
            cost_basis=_latest_cost_basis(
                db, project_id=project_id, part_id=row.part_id
            ),
        )
        for row in rows
    ]
    active = [row for row in payload if row["is_active"]]
    total_revenue = round(sum(row["revenue"] for row in active), 2)
    margins = [row["margin"] for row in active if row["margin"] is not None]
    total_margin = round(sum(margins), 2) if margins else None
    return {
        "project_id": project_id,
        "rows": payload,
        "active_count": len(active),
        "total_revenue": total_revenue,
        "total_margin": total_margin,
        "margin_completeness": (
            "complete" if active and len(margins) == len(active) else "incomplete"
        ),
    }
