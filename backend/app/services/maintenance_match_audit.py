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
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.business_time import business_today
from app.config import get_settings
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.services.maintenance_match_keys import exact_match_key
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
class _CandidatePreview:
    order_id: int
    request_no: str
    pn_std: str | None


@dataclass
class _LooseCandidates:
    """按宽松需求号预聚合的最小分类索引；不保留采购明细行或价格。"""
    order_ids: set[int] = field(default_factory=set)
    part_ids: set[int] = field(default_factory=set)
    eligible_orders_by_part: dict[int, set[int]] = field(
        default_factory=lambda: defaultdict(set)
    )
    previews: list[_CandidatePreview] = field(default_factory=list)
    preview_keys: set[tuple[int, int]] = field(default_factory=set)

    def add(self, order_id: int, request_no: str, part_id: int,
            pn_std: str | None, eligible: bool) -> None:
        self.order_ids.add(order_id)
        self.part_ids.add(part_id)
        if eligible:
            self.eligible_orders_by_part[part_id].add(order_id)
        preview_key = (order_id, part_id)
        if len(self.previews) < 3 and preview_key not in self.preview_keys:
            self.preview_keys.add(preview_key)
            self.previews.append(_CandidatePreview(order_id, request_no, pn_std))


def _loose_key(value: str | None) -> str | None:
    exact = exact_match_key(value)
    if not exact:
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
    keep = 1 if len(text) <= 8 else 2
    hidden = "*" * min(max(len(text) - keep * 2, 3), 12)
    return f"{text[:keep]}{hidden}{text[-keep:]}"


def _sample_ref(line_id: int) -> str:
    secret = get_settings().secret_key.encode("utf-8")
    digest = hmac.new(secret, f"maintenance-match-audit:{line_id}".encode(), hashlib.sha256)
    return f"MA-{digest.hexdigest()[:10].upper()}"


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _purchase_indexes(
    db: Session,
) -> tuple[set[tuple[str, int]], dict[str, _LooseCandidates]]:
    stmt = (
        select(
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
    excluded = {exact_match_key(pn) for pn in config.MAINT_POOL_EXCLUDE_PNS}
    exact_eligible: set[tuple[str, int]] = set()
    by_loose: dict[str, _LooseCandidates] = defaultdict(_LooseCandidates)
    for order_id, request_no, part_id, pn_std, qty, unit_price in db.execute(stmt):
        eligible = bool(
            qty is not None and qty > _ZERO
            and unit_price is not None and unit_price > _ZERO
            and exact_match_key(pn_std) not in excluded
        )
        exact = exact_match_key(request_no)
        if eligible and exact is not None:
            exact_eligible.add((exact, part_id))
        loose = _loose_key(request_no)
        if loose is not None:
            by_loose[loose].add(order_id, request_no, part_id, pn_std, eligible)
    return exact_eligible, dict(by_loose)


def _sample(line_id: int, request_no: str | None, pn_std: str | None,
            candidates: _LooseCandidates | None, reason: str) -> dict:
    return {
        "sample_ref": _sample_ref(line_id),
        "maintenance_request_no": _masked(request_no),
        "maintenance_pn": _masked(pn_std),
        "candidate_order_count": len(candidates.order_ids) if candidates else 0,
        "candidates": [
            {
                "request_no": _masked(row.request_no),
                "pn": _masked(row.pn_std),
            }
            for row in (candidates.previews if candidates else [])
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
    exact_eligible, by_loose = _purchase_indexes(db)

    bucket_counts = {code: 0 for code, _label, _repairable in _BUCKETS}
    bucket_samples: dict[str, list[dict]] = {
        code: [] for code, _label, _repairable in _BUCKETS
    }
    exact_matched = 0
    for line_id, request_no, part_id, pn_std in maintenance_rows:
        exact = exact_match_key(request_no)
        # 母集必须与现行 A0 完全一致：历史语义会把双方纯空白键视为 '' 精确命中。
        # 该语义异常另开问题处理；诊断报告不能在本 slice 偷改正式成本边界。
        if exact is not None and (exact, part_id) in exact_eligible:
            exact_matched += 1
            continue

        loose = _loose_key(request_no)
        candidates = by_loose.get(loose) if loose else None
        eligible_same_part_orders = (
            candidates.eligible_orders_by_part.get(part_id, set()) if candidates else set()
        )
        any_same_part = bool(candidates and part_id in candidates.part_ids)

        if not exact:
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
        elif candidates is not None:
            code = "request_exists_pn_diff"
            reason = "采购侧存在该需求号，但候选 PN 与维保明细不同"
        elif loose is not None:
            code = "purchase_missing_request_no"
            reason = "采购侧未发现该需求号"
        else:  # 防御性兜底；理论上已由 empty 分支覆盖。
            code = "other"
            reason = "未落入已知归因类型，需人工复核"

        bucket_counts[code] += 1
        if len(bucket_samples[code]) < sample_limit:
            bucket_samples[code].append(_sample(line_id, request_no, pn_std, candidates, reason))

    total = len(maintenance_rows)
    unmatched = total - exact_matched
    bucket_sum = sum(bucket_counts.values())
    if bucket_sum != unmatched:
        raise AssertionError(f"bucket sum {bucket_sum} != unmatched {unmatched}")

    format_count = bucket_counts["normalizable_format"]
    buckets = []
    for code, label, repairable in _BUCKETS:
        count = bucket_counts[code]
        buckets.append({
            "code": code,
            "label": label,
            "line_count": count,
            "share_of_unmatched": _ratio(count, unmatched),
            "repairable": repairable,
            "samples": bucket_samples[code],
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
