"""补库购物车「系统三查」（增补包 AB-4）。

业务口径（2026-08-16 确认，REQUIREMENTS #26/#27/#28）：
销售经理选 PN＋数量 → **系统三查** → 系统审批通过 → 导出 Excel 交人工复核。

三查：
1. ``pool_membership``  通用池归属——沿用主数据双线查（并档 PN 跟随主档一跳），
   与 ``maintenance_boss_facts.pool_membership`` 同一实现，口径不另立；
2. ``recent_activity``  半年内购销记录——采购/销售各自的样本数与加权均价；
3. ``niche_pn``         是否小众 PN。

**系统只做系统侧**：这里产出的是给人看的判定材料，不建模人工审批、不记录人工
审结果（AB-4 明示）。因此每一查都返回 ``passed`` 与 ``detail``，任何一查不过
也**不拦截**——拦不拦由导出后的人工复核决定。

一处必须讲明的取舍：业务给了「是否小众」这个查项，但没有给分界线。这里采用
**零样本边界**——半年内采购与销售样本数都为 0 才算小众，不自造数量阈值；
样本数一并回传，人工复核时可自行判断。若业务日后给出阈值，改 ``_NICHE_*``
两个常量即可。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.business_time import business_today
from app.models.dimensions import DimPart
from app.models.inventory import PartPoolPricePolicy
from app.services import pool_price_analysis
from app.services.maintenance_boss_facts import pool_membership

# 「半年」窗口（业务原话「半年内购销记录」）
LOOKBACK_DAYS = 182

# 小众判定：零样本边界，不自造数量阈值（见模块 docstring）
_NICHE_MIN_PURCHASE_SAMPLES = 1
_NICHE_MIN_SALES_SAMPLES = 1

CHECK_KEYS = ("pool_membership", "recent_activity", "niche_pn")


@dataclass(frozen=True)
class Check:
    key: str
    passed: bool
    detail: dict

    def as_dict(self) -> dict:
        return {"key": self.key, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class Screening:
    part_id: int
    pn_std: str
    checks: tuple[Check, ...]

    @property
    def all_passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def get(self, key: str) -> Check:
        return next(check for check in self.checks if check.key == key)

    def as_dict(self) -> dict:
        return {"part_id": self.part_id, "pn_std": self.pn_std,
                "all_passed": self.all_passed,
                "checks": [check.as_dict() for check in self.checks]}


def _window(as_of: date | None) -> tuple[date, date]:
    upper = as_of or business_today()
    return upper - timedelta(days=LOOKBACK_DAYS), upper


def _samples(stats: dict | None) -> int:
    return int((stats or {}).get("order_count") or 0)


def pool_floor_prices(db: Session, group_ids: list[int]) -> dict[int, Decimal | None]:
    """池内最低价（统一未税销售最低价约束）。

    没有当前有效策略 → None，由展示层渲染「—」。铁律 5：不知道 ≠ 没有下限，
    绝不用 0 顶替。
    """
    ids = [gid for gid in dict.fromkeys(group_ids) if gid is not None]
    if not ids:
        return {}
    rows = db.execute(
        select(PartPoolPricePolicy.group_id, PartPoolPricePolicy.sales_floor_ex_tax)
        .where(PartPoolPricePolicy.group_id.in_(ids),
               PartPoolPricePolicy.valid_to.is_(None))
    ).all()
    found = {gid: floor for gid, floor in rows}
    # 每个被问到的池都给一个键：调用方不该靠「键在不在」去区分「没问」和「没有
    # 约束价」——那正是最容易被写成 0 的地方。
    return {gid: found.get(gid) for gid in ids}


def screen(db: Session, *, part_ids: list[int],
           as_of: date | None = None) -> dict[int, Screening]:
    """对一批 PN 跑三查。只读，不写库、不改申请状态。"""
    ids = [int(v) for v in dict.fromkeys(part_ids)]
    if not ids:
        return {}
    parts = db.execute(
        select(DimPart.id, DimPart.pn_std).where(DimPart.id.in_(ids))
    ).all()
    pn_by_id = {pid: pn for pid, pn in parts}
    lower, upper = _window(as_of)
    facts = pool_price_analysis.aggregate_part_price_facts(
        db, ids, date_from=lower, date_to=upper)
    pools = pool_membership(db, {pn for pn in pn_by_id.values() if pn})

    out: dict[int, Screening] = {}
    for part_id in ids:
        pn_std = pn_by_id.get(part_id)
        pool = pools.get(pn_std or "", {"in_pool": None, "pool_name": None,
                                        "pool_status": None})
        part_facts = facts.get(part_id) or {}
        purchase, sales = part_facts.get("purchase"), part_facts.get("sales")
        purchase_n, sales_n = _samples(purchase), _samples(sales)

        checks = (
            # ①通用池归属：认不出型号时 in_pool 为 None——「不在池」是没有依据的
            # 断言（铁律 5），这一查按「查不了」处理，不判它通过。
            Check("pool_membership", bool(pool.get("in_pool")), {
                "in_pool": pool.get("in_pool"),
                "pool_name": pool.get("pool_name"),
                "pool_status": pool.get("pool_status"),
            }),
            # ②半年内购销记录：采购或销售任一有样本即算有记录
            Check("recent_activity", bool(purchase_n or sales_n), {
                "window": {"from": lower.isoformat(), "to": upper.isoformat()},
                "purchase_samples": purchase_n,
                "sales_samples": sales_n,
                "purchase_weighted_avg_ex_tax": (purchase or {}).get("weighted_avg"),
                "sales_weighted_avg_ex_tax": (sales or {}).get("weighted_avg"),
            }),
            # ③是否小众：零样本边界；passed=True 表示「不小众」
            Check("niche_pn",
                  purchase_n >= _NICHE_MIN_PURCHASE_SAMPLES
                  or sales_n >= _NICHE_MIN_SALES_SAMPLES,
                  {"is_niche": not (purchase_n or sales_n),
                   "purchase_samples": purchase_n, "sales_samples": sales_n,
                   "rule": "零样本边界：半年内采购与销售样本都为 0 才算小众"}),
        )
        out[part_id] = Screening(part_id=part_id, pn_std=pn_std or "",
                                 checks=checks)
    return out


def latest_sales_history(db: Session, *, part_ids: list[int],
                         as_of: date | None = None) -> dict[int, dict]:
    """每 PN 最近一笔有效销售价格点（AB-4 导出列「最近销售历史」）。"""
    ids = [int(v) for v in dict.fromkeys(part_ids)]
    if not ids:
        return {}
    lower, upper = _window(as_of)
    return pool_price_analysis._price_map_latest_raw(
        db, side="sales", part_ids=ids, lower=lower, upper=upper,
        purchase_type=None, employee=None)
