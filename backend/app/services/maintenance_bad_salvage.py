"""坏件变卖登记与贡献毛利（F5，round-5 修复版）。

写：登记/作废（实名 + 项目范围 + bad-return 管理权限，API 层把关）；
- 登记时冻结成本证据（cost_basis_inc_tax + 来源行 + 算法版本），后续事实
  变化不改写历史毛利；
- 件在前置库有结存时同事务做 salvage_out 减账本（stock_deducted=true）；
  结存为 0（坏件本不在库）时只记事实不扣账（stock_deducted=false）；
  部分在库（0 < 结存 < 数量）无法判定 → 失败关闭；
- 作废时对已扣账部分做 salvage_in 反向回冲（流水不删改）。
读：项目变卖清单 + 贡献毛利（按冻结成本；缺成本不按 0）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.maintenance_bad_salvage import MaintenanceBadSalvage
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import (
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.services import maintenance_front_stock as front_stock

COST_ALGORITHM_VERSION = "salvage-cost-latest-confirmed-v1"


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


def _dict(row: MaintenanceBadSalvage) -> dict:
    margin = (
        (row.revenue - row.cost_basis_inc_tax * row.qty).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if row.cost_basis_inc_tax is not None
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
        "stock_deducted": row.stock_deducted,
        # 冻结成本证据：登记时快照，不随后续事实变化
        "cost_basis_inc_tax": (
            float(row.cost_basis_inc_tax)
            if row.cost_basis_inc_tax is not None
            else None
        ),
        "cost_source_ref": row.cost_source_ref,
        "cost_algorithm_version": row.cost_algorithm_version,
        "margin": float(margin) if margin is not None else None,
    }


def _latest_cost_basis(
    db: Session, *, project_id: str, part_id: int | None
) -> tuple[Decimal | None, str | None]:
    if part_id is None:
        return None, None
    row = db.execute(
        select(
            MaintenanceSiteIssueLine.unit_cost_inc_tax,
            MaintenanceSiteIssueLine.issue_line_id,
        )
        .join(
            MaintenanceSiteIssue,
            MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
        )
        .where(
            MaintenanceSiteIssue.project_id == project_id,
            MaintenanceSiteIssueLine.part_id == part_id,
            MaintenanceSiteIssue.normalized_status.in_(("confirmed", "corrected")),
            MaintenanceSiteIssueLine.unit_cost_inc_tax.is_not(None),
            # 2026-08-19：作废领用行不能作为坏件变卖成本依据（#55）
            MaintenanceSiteIssueLine.is_active.is_(True),
        )
        .order_by(
            MaintenanceSiteIssue.issue_date.desc(),
            MaintenanceSiteIssueLine.updated_at.desc(),
        )
        .limit(1)
    ).first()
    if row is None:
        return None, None
    return Decimal(row[0]), str(row[1])


def _deduct_stock(
    db: Session,
    *,
    project_id: str,
    part_id: int,
    qty: Decimal,
    salvage_id: str,
    operated_by: str,
) -> tuple[bool, str]:
    """对前置库结存做 salvage_out；返回 (是否扣账, 描述)。

    - 总结存 ≥ qty → 从各仓库顺序扣账，返回 (True, ...)；
    - 总结存 == 0 → 坏件/已消耗件正常场景，不扣账返回 (False, "no_front_stock")；
    - 0 < 总结存 < qty → 无法判定，SalvageError 失败关闭。
    """
    rows = front_stock.balance_rows(db, project_id)
    part_rows = [row for row in rows if row["part_id"] == part_id]
    total = sum(Decimal(str(row["qty"])) for row in part_rows)
    if total == 0:
        return False, "no_front_stock"
    if total < qty:
        raise SalvageError(
            f"前置库结存 {total} 小于变卖数量 {qty}，无法判定扣账，拒绝登记"
        )
    remaining = qty
    for row in part_rows:
        if remaining <= 0:
            break
        take = min(Decimal(str(row["qty"])), remaining)
        if take <= 0:
            continue
        front_stock.apply_movement(
            db,
            project_id=project_id,
            part_id=part_id,
            kind="salvage_out",
            source_type="salvage",
            source_ref=f"salvage:{salvage_id}:{row['stock_id']}",
            qty=take,
            warehouse_name=row["warehouse_name"],
            occurred_at=datetime.now(timezone.utc),
            reason=f"坏件变卖登记 {salvage_id}",
            operated_by=operated_by,
        )
        remaining -= take
    return True, "deducted"


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
    """登记一笔坏件变卖（冻结毛利 + 同事务 salvage_out）。"""
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
    if part_id is not None:
        part = db.get(DimPart, part_id)
        if part is None or part.pn_std != pn:
            raise SalvageError("PN 与所选备件不一致")
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
        ).scalar_one_or_none()
        if existing is not None:
            if existing.payload_digest != digest:
                raise SalvageConflict("同一幂等键对应不同变卖内容，拒绝重放")
            return _dict(existing)
    cost_basis, cost_source_ref = _latest_cost_basis(
        db, project_id=project_id, part_id=part_id
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
        cost_basis_inc_tax=cost_basis,
        cost_source_ref=cost_source_ref,
        cost_algorithm_version=(
            COST_ALGORITHM_VERSION if cost_basis is not None else None
        ),
        operated_by=operated_by[:64],
    )
    db.add(row)
    db.flush()
    deducted, _basis = _deduct_stock(
        db,
        project_id=project_id,
        part_id=part_id,
        qty=qty,
        salvage_id=row.salvage_id,
        operated_by=operated_by,
    )
    row.stock_deducted = deducted
    db.flush()
    return _dict(row)


def void_salvage(
    db: Session, *, salvage_id: str, operated_by: str, version: int
) -> dict:
    """作废一笔变卖登记：软作废 + 对已扣账部分 salvage_in 回冲（流水不删改）。"""
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
    if row.stock_deducted and row.part_id is not None:
        # 回冲：按原扣账数量全额 salvage_in（仓库名取自结存行）
        ledger_rows = db.execute(
            select(front_stock.MaintenanceFrontStockLedger).where(
                front_stock.MaintenanceFrontStockLedger.source_ref.like(
                    f"salvage:{row.salvage_id}:%"
                )
            )
        ).scalars().all()
        stock_ids = [ledger.stock_id for ledger in ledger_rows]
        warehouse_by_stock: dict[str, str] = {}
        if stock_ids:
            for stock in db.execute(
                select(front_stock.MaintenanceFrontStock).where(
                    front_stock.MaintenanceFrontStock.stock_id.in_(stock_ids)
                )
            ).scalars():
                warehouse_by_stock[stock.stock_id] = stock.warehouse_name
        for ledger in ledger_rows:
            front_stock.apply_movement(
                db,
                project_id=row.project_id,
                part_id=row.part_id,
                kind="salvage_in",
                source_type="salvage",
                source_ref=f"salvage-reverse:{row.salvage_id}:{ledger.ledger_id}",
                qty=ledger.qty_change * Decimal("-1"),
                warehouse_name=warehouse_by_stock.get(ledger.stock_id, ""),
                occurred_at=datetime.now(timezone.utc),
                reason=f"变卖登记作废回冲 {row.salvage_id}",
                operated_by=operated_by[:64],
            )
    row.is_active = False
    row.voided_by = operated_by[:64]
    row.voided_at = datetime.now(timezone.utc)
    row.version += 1
    db.flush()
    return _dict(row)


def list_salvage(db: Session, project_id: str) -> dict:
    """项目坏件变卖清单 + 贡献毛利汇总（按冻结成本；缺成本不按 0）。"""
    rows = db.execute(
        select(MaintenanceBadSalvage)
        .where(MaintenanceBadSalvage.project_id == project_id)
        .order_by(
            MaintenanceBadSalvage.salvage_date.desc(),
            MaintenanceBadSalvage.created_at.desc(),
        )
    ).scalars().all()
    payload = [_dict(row) for row in rows]
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
