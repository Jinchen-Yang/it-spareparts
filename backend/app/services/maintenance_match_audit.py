"""维保需求号匹配归因报告（DEV-13A，只读）。

本模块只解释 ``maintenance_cost`` A0 直配为什么没有命中。它不写映射、不回填
成本，也不改变成本瀑布。精确匹配键严格复刻现行 ``strip().upper()``；更宽松的
格式键只用于报告归因，绝不参与正式取价。
"""
from __future__ import annotations

import hashlib
import hmac
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.business_time import business_today
from app.config import get_settings
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.services.query_filters import active_orders

_LOOSE_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_ZERO = Decimal("0")

_BUCKETS = (
    ("empty_request_no", "单号为空", False),
    ("normalizable_format", "格式可规整", True),
    ("request_exists_pn_diff", "单号存在但 PN 不同", False),
    ("purchase_missing_request_no", "采购侧无该需求号", False),
    ("duplicate_candidates", "重复候选", False),
    ("other", "其他", False),
)


@dataclass(frozen=True)
class _PurchaseCandidate:
    line_id: int
    order_id: int
    request_no: str
    part_id: int
    pn_std: str | None
    eligible: bool


def _exact_key(value: str | None) -> str | None:
    """与 maintenance_cost._norm_key 等价；不直接复用私有函数，避免反向耦合。"""
    return value.strip().upper() if value and value.strip() else None


def _loose_key(value: str | None) -> str | None:
    exact = _exact_key(value)
    if exact is None:
        return None
    loose = _LOOSE_NON_ALNUM.sub("", exact)
    return loose or None


def _masked(value: str | None) -> str | None:
    """保留少量首尾特征供人工辨认，不返回原值。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) <= 4:
        return "*" * len(text)
    hidden = "*" * min(max(len(text) - 4, 3), 12)
    return f"{text[:2]}{hidden}{text[-2:]}"


def _sample_ref(line_id: int) -> str:
    secret = get_settings().secret_key.encode("utf-8")
    digest = hmac.new(secret, f"maintenance-match-audit:{line_id}".encode(), hashlib.sha256)
    return f"MA-{digest.hexdigest()[:10].upper()}"


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _purchase_rows(db: Session) -> list[_PurchaseCandidate]:
    stmt = (
        select(
            FPurchaseLine.id,
            FPurchaseOrder.id,
            FPurchaseOrder.linked_maintenance_order_no,
            FPurchaseLine.part_id,
            FPurchaseLine.pn_std,
            FPurchaseLine.qty,
            FPurchaseLine.unit_price,
        )
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(
            FPurchaseOrder.linked_maintenance_order_no.is_not(None),
            FPurchaseOrder.linked_maintenance_order_no != "",
        )
        .order_by(FPurchaseLine.id)
    )
    stmt = active_orders(stmt, FPurchaseOrder)
    excluded = {_exact_key(pn) for pn in config.MAINT_POOL_EXCLUDE_PNS}
    rows: list[_PurchaseCandidate] = []
    for line_id, order_id, request_no, part_id, pn_std, qty, unit_price in db.execute(stmt):
        eligible = bool(
            qty is not None and qty > _ZERO
            and unit_price is not None and unit_price > _ZERO
            and _exact_key(pn_std) not in excluded
        )
        rows.append(_PurchaseCandidate(
            line_id=line_id,
            order_id=order_id,
            request_no=request_no,
            part_id=part_id,
            pn_std=pn_std,
            eligible=eligible,
        ))
    return rows


def _sample(line_id: int, request_no: str | None, pn_std: str | None,
            candidates: list[_PurchaseCandidate], reason: str) -> dict:
    unique: dict[tuple[int, int], _PurchaseCandidate] = {}
    for candidate in candidates:
        unique[(candidate.order_id, candidate.part_id)] = candidate
    ordered = sorted(unique.values(), key=lambda row: (row.order_id, row.line_id))
    return {
        "sample_ref": _sample_ref(line_id),
        "maintenance_request_no": _masked(request_no),
        "maintenance_pn": _masked(pn_std),
        "candidate_order_count": len({row.order_id for row in candidates}),
        "candidates": [
            {
                "request_no": _masked(row.request_no),
                "pn": _masked(row.pn_std),
            }
            for row in ordered[:3]
        ],
        "reason": reason,
    }


def build_report(db: Session, *, sample_limit: int = 5) -> dict:
    """生成确定性的只读归因报告；固定两次 SELECT，查询数不随数据量增长。"""
    if not 0 <= sample_limit <= 10:
        raise ValueError("sample_limit must be between 0 and 10")

    maintenance_stmt = (
        select(
            FMaintenanceLine.id,
            FMaintenanceOrder.order_no,
            FMaintenanceLine.part_id,
            FMaintenanceLine.pn_std,
        )
        .join(FMaintenanceOrder, FMaintenanceLine.order_id == FMaintenanceOrder.id)
        .where(FMaintenanceOrder.order_date >= config.MAINT_COST_START_DATE)
        .order_by(FMaintenanceLine.id)
    )
    maintenance_stmt = active_orders(maintenance_stmt, FMaintenanceOrder)
    maintenance_rows = list(db.execute(maintenance_stmt))
    purchase_rows = _purchase_rows(db)

    exact_eligible: set[tuple[str, int]] = set()
    by_loose: dict[str, list[_PurchaseCandidate]] = defaultdict(list)
    for candidate in purchase_rows:
        exact = _exact_key(candidate.request_no)
        loose = _loose_key(candidate.request_no)
        if candidate.eligible and exact is not None:
            exact_eligible.add((exact, candidate.part_id))
        if loose is not None:
            by_loose[loose].append(candidate)

    bucket_rows: dict[str, list[dict]] = {code: [] for code, _label, _repairable in _BUCKETS}
    exact_matched = 0
    for line_id, request_no, part_id, pn_std in maintenance_rows:
        exact = _exact_key(request_no)
        if exact is not None and (exact, part_id) in exact_eligible:
            exact_matched += 1
            continue

        loose = _loose_key(request_no)
        candidates = by_loose.get(loose, []) if loose else []
        eligible_same_part_orders = {
            candidate.order_id
            for candidate in candidates
            if candidate.eligible and candidate.part_id == part_id
        }
        any_same_part = any(candidate.part_id == part_id for candidate in candidates)

        if exact is None:
            code = "empty_request_no"
            reason = "维保侧需求号为空，无法建立需求号关联"
        elif len(eligible_same_part_orders) == 1:
            code = "normalizable_format"
            reason = "仅格式符号不同，规整后唯一命中同 PN 采购候选"
        elif len(eligible_same_part_orders) > 1:
            code = "duplicate_candidates"
            reason = "规整后命中多张同 PN 采购候选，不能唯一判断"
        elif any_same_part:
            code = "other"
            reason = "存在同需求号、同 PN 记录，但不满足现行直配候选条件"
        elif candidates:
            code = "request_exists_pn_diff"
            reason = "采购侧存在该需求号，但候选 PN 与维保明细不同"
        elif loose is not None:
            code = "purchase_missing_request_no"
            reason = "采购侧未发现该需求号"
        else:  # 防御性兜底；理论上已由 empty 分支覆盖。
            code = "other"
            reason = "未落入已知归因类型，需人工复核"

        bucket_rows[code].append(_sample(line_id, request_no, pn_std, candidates, reason))

    total = len(maintenance_rows)
    unmatched = total - exact_matched
    bucket_sum = sum(len(rows) for rows in bucket_rows.values())
    if bucket_sum != unmatched:
        raise AssertionError(f"bucket sum {bucket_sum} != unmatched {unmatched}")

    format_count = len(bucket_rows["normalizable_format"])
    buckets = []
    for code, label, repairable in _BUCKETS:
        rows = bucket_rows[code]
        buckets.append({
            "code": code,
            "label": label,
            "line_count": len(rows),
            "share_of_unmatched": _ratio(len(rows), unmatched),
            "repairable": repairable,
            "samples": rows[:sample_limit],
        })

    return {
        "restricted": False,
        "as_of": business_today().isoformat(),
        "scope": {
            "definition": "active_maintenance_since_cost_start",
            "maintenance_start_date": config.MAINT_COST_START_DATE.isoformat(),
            "total_line_count": total,
            "exact_matched_line_count": exact_matched,
            "unmatched_line_count": unmatched,
            "exact_match_rate": _ratio(exact_matched, total),
        },
        "repairable": {
            "line_count": format_count,
            "rate_of_unmatched": _ratio(format_count, unmatched),
            "meaning": "技术上可规整候选；只读，不自动修改",
        },
        "buckets": buckets,
        "invariant": {
            "bucket_sum": bucket_sum,
            "equals_unmatched": bucket_sum == unmatched,
        },
    }

