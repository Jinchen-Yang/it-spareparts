"""售前询价 M3 价格参考批量服务。

对外只有 ``batch``：给一批 part_id 返回近 ``window_days`` 天的销售/采购
时间衰减均价、历史询价参考与口径元信息。均价算法全部复用已上线的商城
参考价（D-42 / D-43）——``resale_price_reference`` 的样本聚合、时间衰减、
双税换算与 HALF_UP 舍入是唯一实现；本模块只做批量取数、权限判定和结果整形，
不另写一套算法。

采购价可见性沿用权限中心 ``data_purchase_cost`` 的字段组判定
（``security.is_field_hidden``，与 ``apply_field_visibility`` 同源）；
无权时不查询采购样本，既避免计数泄漏也省一次查询。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.config import Settings, get_settings
from app.models.dimensions import DimPart
from app.schemas.resale_price_reference import ReferenceWindow
from app.security import UserContext, is_field_hidden
from app.services import part_overview
from app.services.resale_price_reference import (
    _Group,
    _grouped_samples_by_part,
    _price_entries,
    _purchase_grouped_samples_by_part,
    _weighted_amounts,
)

# data_purchase_cost 字段组的代表键：与 apply_field_visibility 的隐藏字段同一判定。
_PURCHASE_COST_FIELD = "purchase_price"
_WINDOW_DAYS_MAX = 366


def _method(settings: Settings) -> Literal[
    "time_decay_qty_weighted", "time_decay_line_weighted"
]:
    return (
        "time_decay_qty_weighted"
        if settings.resale_price_reference_weighting == "qty_weighted"
        else "time_decay_line_weighted"
    )


def _summary(
    groups: list[_Group],
    *,
    kind: Literal["sale", "purchase"],
    window: ReferenceWindow,
    end: date,
    settings: Settings,
    decay_cache: dict[date, Decimal] | None = None,
) -> dict | None:
    weighted = _weighted_amounts(
        groups,
        end=end,
        half_life_days=settings.resale_price_reference_half_life_days,
        weighting=settings.resale_price_reference_weighting,
        decay_cache=decay_cache,
    )
    entries = _price_entries(groups, weighted, kind=kind, window=window)
    if not entries:
        return None
    # _price_entries 固定先含税、后不含税；两项口径元信息相同。
    inc, ex = entries
    return {
        "avg_inc_tax": inc.amount,
        "avg_ex_tax": ex.amount,
        "sample_count": inc.sample_count,
        "order_count": inc.order_count,
        "latest_date": inc.latest_sale_date,
        "oldest_date": inc.oldest_sale_date,
    }


def batch(
    db: Session,
    part_ids: list[int],
    ctx: UserContext,
    *,
    window_days: int = 30,
    today: date | None = None,
) -> dict[int, dict]:
    """批量返回参考价；不存在或已合并的 id 不进入结果。"""
    if not 1 <= window_days <= _WINDOW_DAYS_MAX:
        raise ValueError(f"window_days 必须在 1..{_WINDOW_DAYS_MAX} 之间")
    requested: list[int] = []
    seen: set[int] = set()
    for part_id in part_ids:
        if part_id not in seen:
            seen.add(part_id)
            requested.append(part_id)
    if not requested:
        return {}

    rows = db.execute(
        select(DimPart.id, DimPart.pn_std).where(
            DimPart.id.in_(requested), DimPart.status == "active"
        )
    ).all()
    active = {row.id: row.pn_std for row in rows}
    resolved_ids = [part_id for part_id in requested if part_id in active]
    if not resolved_ids:
        return {}

    settings = get_settings()
    end = today or business_today()
    start = end - timedelta(days=window_days - 1)
    window = ReferenceWindow(start=start, end=end)
    method = _method(settings)
    # 整批共享同一订单日 → 衰减因子，避免 200 个 PN 重复计算同一个 50 位幂。
    decay_cache: dict[date, Decimal] = {}

    sale_groups = _grouped_samples_by_part(
        db, part_ids=resolved_ids, start=start, end=end, settings=settings
    )
    purchase_restricted = is_field_hidden(ctx, _PURCHASE_COST_FIELD)
    # 无权时绝不查询采购事实表：不产生计数、也不让 SQL 日志留下可推断的痕迹。
    purchase_groups = (
        {}
        if purchase_restricted
        else _purchase_grouped_samples_by_part(
            db, part_ids=resolved_ids, start=start, end=end, settings=settings
        )
    )
    inquiry = part_overview.inquiry_refs(
        db, [(part_id, active[part_id]) for part_id in resolved_ids]
    )

    items: dict[int, dict] = {}
    for part_id in resolved_ids:
        items[part_id] = {
            "sale": _summary(
                sale_groups.get(part_id, []),
                kind="sale",
                window=window,
                end=end,
                settings=settings,
                decay_cache=decay_cache,
            ),
            "purchase": (
                None
                if purchase_restricted
                else _summary(
                    purchase_groups.get(part_id, []),
                    kind="purchase",
                    window=window,
                    end=end,
                    settings=settings,
                    decay_cache=decay_cache,
                )
            ),
            "purchase_restricted": purchase_restricted,
            "inquiry_ref": inquiry[part_id],
            "window": {"start": start, "end": end, "days": window_days},
            "method": method,
        }
    return items
