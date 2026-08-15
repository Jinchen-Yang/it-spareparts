"""维保前置库账本服务（B1）。

口径（2026-08-15 已确认 + Codex 审核修复）：
- 入账 = 氚云发货单维保供货（shipment_in）、采购直发（purchase_in）；
- 出账 = 项目结束收回（return_out）、变卖（salvage_out）；
- 现场领用不写本账本；无收货环节；不记 SN；
- 幂等 = (source_type, source_ref) 全局唯一 + payload_hash 摘要校验：
  同摘要重放原结果，异摘要失败关闭；
- 结存行用 SELECT FOR UPDATE 防并发丢失更新；
- occurred_at = 业务发生时间（发货日期），库龄以此为准。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, time, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.maintenance_front_stock import (
    SOURCE_TYPES,
    MaintenanceFrontStock,
    MaintenanceFrontStockLedger,
)

KIND_SIGNS = {
    "shipment_in": Decimal(1),
    "purchase_in": Decimal(1),
    "return_out": Decimal(-1),
    "salvage_out": Decimal(-1),
    "salvage_in": Decimal(1),
}


class FrontStockError(RuntimeError):
    """前置库账本业务错误基类。"""


class FrontStockNegativeBalance(FrontStockError):
    """出账后结存为负：失败关闭，不写账。"""


class FrontStockInvalidMovement(FrontStockError):
    """非法流水类型/来源/数量。"""


class FrontStockPayloadConflict(FrontStockError):
    """同一来源事件以不同 payload 重放：失败关闭。"""


def movement_payload_hash(
    *,
    kind: str,
    project_id: str,
    part_id: int,
    qty: Decimal,
    warehouse_name: str,
    unit_cost_ex_tax: Decimal | None,
    unit_cost_inc_tax: Decimal | None,
    occurred_at: datetime | None,
) -> str:
    payload = {
        "kind": kind,
        "project_id": project_id,
        "part_id": part_id,
        "qty": str(qty),
        "warehouse_name": warehouse_name,
        "unit_cost_ex_tax": str(unit_cost_ex_tax) if unit_cost_ex_tax is not None else None,
        "unit_cost_inc_tax": str(unit_cost_inc_tax) if unit_cost_inc_tax is not None else None,
        "occurred_at": occurred_at.isoformat() if occurred_at is not None else None,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _signed(kind: str, qty: Decimal) -> Decimal:
    sign = KIND_SIGNS.get(kind)
    if sign is None:
        raise FrontStockInvalidMovement(f"未知流水类型：{kind}")
    if qty <= 0:
        raise FrontStockInvalidMovement("数量必须为正数")
    return qty * sign


def _get_or_create_stock(
    db: Session,
    *,
    project_id: str,
    part_id: int,
    warehouse_name: str,
) -> MaintenanceFrontStock:
    # 用事务级 advisory lock 串行化同一 (project, part, warehouse) 的结存行
    # 创建：SELECT ... FOR UPDATE 对“不存在的行”无法互斥（phantom），
    # 两个会话可同时 INSERT 竞态；advisory xact lock 在 commit 时释放，
    # 后到者等锁后必然读到已提交的行（round-5 Blocker 9）。
    lock_key = int(
        hashlib.sha256(
            f"front-stock:{project_id}:{part_id}:{warehouse_name}".encode("utf-8")
        ).hexdigest()[:15],
        16,
    )
    db.execute(select(func.pg_advisory_xact_lock(lock_key)))
    stock = db.execute(
        select(MaintenanceFrontStock)
        .where(
            MaintenanceFrontStock.project_id == project_id,
            MaintenanceFrontStock.part_id == part_id,
            MaintenanceFrontStock.warehouse_name == warehouse_name,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if stock is not None:
        return stock
    stock = MaintenanceFrontStock(
        stock_id=str(uuid4()),
        project_id=project_id,
        part_id=part_id,
        warehouse_name=warehouse_name,
        qty=Decimal("0"),
        version=1,
    )
    db.add(stock)
    db.flush()
    return stock


def apply_movement(
    db: Session,
    *,
    project_id: str,
    part_id: int,
    kind: str,
    source_type: str,
    source_ref: str,
    qty: Decimal,
    warehouse_name: str = "",
    unit_cost_ex_tax: Decimal | None = None,
    unit_cost_inc_tax: Decimal | None = None,
    occurred_at: datetime | None = None,
    reason: str | None = None,
    operated_by: str,
) -> MaintenanceFrontStockLedger:
    """入/出账一笔记账。

    幂等：同 (source_type, source_ref) 已存在时，比较 payload_hash：
    一致返回既有流水（重放），不一致抛 FrontStockPayloadConflict（失败关闭）。
    """
    if source_type not in SOURCE_TYPES:
        raise FrontStockInvalidMovement(f"未知来源类型：{source_type}")
    if not source_ref or not source_ref.strip():
        raise FrontStockInvalidMovement("来源引用不可为空")
    if len(source_ref) > 256:
        raise FrontStockInvalidMovement("来源引用超过 256 字符上限")
    if not operated_by or not operated_by.strip():
        raise FrontStockInvalidMovement("操作人不可为空")
    qty = Decimal(qty)
    signed = _signed(kind, qty)
    payload_hash = movement_payload_hash(
        kind=kind,
        project_id=project_id,
        part_id=part_id,
        qty=qty,
        warehouse_name=warehouse_name,
        unit_cost_ex_tax=unit_cost_ex_tax,
        unit_cost_inc_tax=unit_cost_inc_tax,
        occurred_at=occurred_at,
    )

    existing = db.execute(
        select(MaintenanceFrontStockLedger)
        .where(
            MaintenanceFrontStockLedger.source_type == source_type,
            MaintenanceFrontStockLedger.source_ref == source_ref,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise FrontStockPayloadConflict(
                f"来源事件 {source_ref} 以不同内容重放，拒绝入账"
            )
        return existing

    stock = _get_or_create_stock(
        db, project_id=project_id, part_id=part_id, warehouse_name=warehouse_name
    )
    # 关键区（结存变更 + 流水插入）用 savepoint 包裹：
    # 并发同来源唯一冲突时回滚本笔全部变更，再回读重放/409；
    # 负结存/其他失败不留下半写入的结存或幽灵结存行。
    savepoint = db.begin_nested()
    try:
        new_qty = stock.qty + signed
        if new_qty < 0:
            raise FrontStockNegativeBalance(
                f"前置库结存不足：{stock.qty} < 出账 {qty}（{kind}）"
            )
        stock.qty = new_qty
        if signed > 0:
            stock.last_inbound_at = occurred_at or datetime.now(timezone.utc)
            if unit_cost_ex_tax is not None and unit_cost_inc_tax is not None:
                stock.unit_cost_ex_tax = unit_cost_ex_tax
                stock.unit_cost_inc_tax = unit_cost_inc_tax
            else:
                # 单侧成本或全缺：整体置 unknown，禁止单侧值冒充双口径完整估值
                # 未知成本批次：整体置 unknown，禁止旧单价冒充新批成本
                stock.unit_cost_ex_tax = None
                stock.unit_cost_inc_tax = None
        stock.version += 1

        ledger = MaintenanceFrontStockLedger(
            ledger_id=str(uuid4()),
            stock_id=stock.stock_id,
            project_id=project_id,
            part_id=part_id,
            kind=kind,
            source_type=source_type,
            source_ref=source_ref,
            qty_change=signed,
            qty_after=new_qty,
            payload_hash=payload_hash,
            occurred_at=occurred_at,
            unit_cost_ex_tax=unit_cost_ex_tax,
            unit_cost_inc_tax=unit_cost_inc_tax,
            reason=reason,
            operated_by=operated_by[:64],
        )
        db.add(ledger)
        db.flush()
    except FrontStockNegativeBalance:
        savepoint.rollback()
        raise
    except IntegrityError:
        # 并发同来源：另一事务先写入同一 (source_type, source_ref)。
        # 回滚本笔结存变更，回读既有流水做稳定重放/409。
        savepoint.rollback()
        concurrent = db.execute(
            select(MaintenanceFrontStockLedger)
            .where(
                MaintenanceFrontStockLedger.source_type == source_type,
                MaintenanceFrontStockLedger.source_ref == source_ref,
            )
        ).scalar_one_or_none()
        if concurrent is None:
            raise
        if concurrent.payload_hash != payload_hash:
            raise FrontStockPayloadConflict(
                f"来源事件 {source_ref} 以不同内容重放，拒绝入账"
            )
        return concurrent
    return ledger


def balance_rows(db: Session, project_id: str) -> list[dict]:
    """项目前置库结存（含库龄天数与金额估值；缺成本不按 0）。

    附加 F3 字段：该 PN 在本项目最近领用日期与「超 90 天未领用」标记
    （领用事实=已确认/已更正的现场领用单明细，不写前置库账本）。
    """
    from app.models.dimensions import DimPart
    from app.models.maintenance_project_operations import (
        MaintenanceSiteIssue,
        MaintenanceSiteIssueLine,
    )

    rows = db.execute(
        select(
            MaintenanceFrontStock,
            DimPart.pn_std,
            DimPart.description,
        )
        .join(DimPart, DimPart.id == MaintenanceFrontStock.part_id)
        .where(MaintenanceFrontStock.project_id == project_id)
        .order_by(MaintenanceFrontStock.warehouse_name, DimPart.pn_std)
    ).all()
    consumption: dict[int, datetime] = {}
    if rows:
        part_ids = [stock.part_id for stock, _, _ in rows]
        for part_id, last_date in db.execute(
            select(
                MaintenanceSiteIssueLine.part_id,
                func.max(MaintenanceSiteIssue.issue_date),
            )
            .join(
                MaintenanceSiteIssue,
                MaintenanceSiteIssue.issue_id == MaintenanceSiteIssueLine.issue_id,
            )
            .where(
                MaintenanceSiteIssue.project_id == project_id,
                MaintenanceSiteIssueLine.part_id.in_(part_ids),
                MaintenanceSiteIssue.normalized_status.in_(
                    ("confirmed", "corrected")
                ),
            )
            .group_by(MaintenanceSiteIssueLine.part_id)
        ):
            consumption[part_id] = datetime.combine(
                last_date, time.min
            ).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    result = []
    for stock, pn, description in rows:
        age_days = None
        if stock.last_inbound_at is not None:
            age_days = max(0, (now - stock.last_inbound_at).days)
        last_consumed_at = consumption.get(stock.part_id)
        days_since_consumption = (
            max(0, (now - last_consumed_at).days)
            if last_consumed_at is not None
            else None
        )
        # 超 90 天未领用：入库已超 90 天且（从未领用或领用也已超 90 天）。
        # 新入库未领用件不算超期（round-5 Blocker 3）。
        stale_90d = bool(
            stock.qty > 0
            and age_days is not None
            and age_days > 90
            and (days_since_consumption is None or days_since_consumption > 90)
        )
        result.append(
            {
                "stock_id": stock.stock_id,
                "part_id": stock.part_id,
                "pn": pn,
                "description": description,
                "warehouse_name": stock.warehouse_name,
                "qty": float(stock.qty),
                "unit_cost_ex_tax": (
                    float(stock.unit_cost_ex_tax)
                    if stock.unit_cost_ex_tax is not None
                    else None
                ),
                "unit_cost_inc_tax": (
                    float(stock.unit_cost_inc_tax)
                    if stock.unit_cost_inc_tax is not None
                    else None
                ),
                "value_ex_tax": (
                    float(stock.qty * stock.unit_cost_ex_tax)
                    if stock.unit_cost_ex_tax is not None
                    else None
                ),
                "value_inc_tax": (
                    float(stock.qty * stock.unit_cost_inc_tax)
                    if stock.unit_cost_inc_tax is not None
                    else None
                ),
                "last_inbound_at": (
                    stock.last_inbound_at.isoformat()
                    if stock.last_inbound_at is not None
                    else None
                ),
                "age_days": age_days,
                "last_consumed_at": (
                    last_consumed_at.isoformat()
                    if last_consumed_at is not None
                    else None
                ),
                "days_since_last_consumption": days_since_consumption,
                "stale_90d": stale_90d,
            }
        )
    return result


def ledger_entries(db: Session, project_id: str, *, limit: int = 200) -> list[dict]:
    rows = db.execute(
        select(MaintenanceFrontStockLedger)
        .where(MaintenanceFrontStockLedger.project_id == project_id)
        .order_by(MaintenanceFrontStockLedger.created_at.desc())
        .limit(min(max(limit, 1), 1000))
    ).scalars().all()
    return [
        {
            "ledger_id": row.ledger_id,
            "part_id": row.part_id,
            "kind": row.kind,
            "source_type": row.source_type,
            "source_ref": row.source_ref,
            "qty_change": float(row.qty_change),
            "qty_after": float(row.qty_after),
            "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
            "reason": row.reason,
            "operated_by": row.operated_by,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]
