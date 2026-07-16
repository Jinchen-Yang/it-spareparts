"""DEV-05B1 数据疑点规则校准预览（只读）。

这里只对正式采购事实做 SELECT 预览：不保存阈值，不调用疑点创建服务，
不触发导入、重算、审计或经营统计变更。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    Integer,
    Numeric,
    String,
    case,
    cast,
    column,
    func,
    literal,
    select,
    text,
    true,
    union_all,
    values,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import config
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.services.pricing import purchase_ex_unit


RULE_CODE = "purchase_adjacent_price_ratio"
RULE_VERSION = "preview-v1"
THRESHOLDS = (2, 3, 5, 10)
EMPTY_PURCHASE_TYPE = "(空)"
STATEMENT_TIMEOUT_MS = 3_000


class CalibrationPreviewTimeout(RuntimeError):
    """校准预览超过只读查询保护时限。"""


def _rate(candidate: int, eligible: int) -> float:
    return candidate / eligible if eligible else 0.0


def _threshold_rows(
    counts: dict[tuple[str, str, int], tuple[int, int]],
    *,
    purchase_types: list[str],
    eligible: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for multiplier in THRESHOLDS:
        increased = sum(
            counts.get((name, "increase", multiplier), (0, 0))[1]
            for name in purchase_types
        )
        decreased = sum(
            counts.get((name, "decrease", multiplier), (0, 0))[1]
            for name in purchase_types
        )
        candidate = increased + decreased
        rows.append({
            "multiplier": multiplier,
            "eligible_pairs": eligible,
            "candidate_pairs": candidate,
            "candidate_rate": _rate(candidate, eligible),
            "increased_pairs": increased,
            "decreased_pairs": decreased,
        })
    return rows


def purchase_price_preview(
    db: Session,
    *,
    date_from: date | None,
    date_to: date,
    purchase_type: str | None,
    sample_limit: int,
) -> dict[str, Any]:
    """预览相邻采购价倍率。

    单条 SQL 内物化一次相邻对 CTE，然后同时生成统计和确定性样本；
    四个固定倍率不会重复扫描四次事实表。
    """
    normalized_type = func.coalesce(FPurchaseOrder.source_type, EMPTY_PURCHASE_TYPE)
    # Money 是 NUMERIC(14,2)。直接拿除税表达式继续做比值时，SQLAlchemy 会在
    # “前值 / 本次值”分支把本次值重新 CAST 回两位小数，导致精确 2 倍变成
    # 1.9999 而漏判。先把正式未税表达式提升到无固定 scale 的 NUMERIC
    # （不截断除税循环小数），再做 lag/ratio。
    normalized_price = cast(purchase_ex_unit(), Numeric())
    tax_basis = case(
        (FPurchaseOrder.is_tax_inclusive.is_(False), "ex_tax"),
        (FPurchaseOrder.is_tax_inclusive.is_(True), "inc_tax"),
        else_="unknown_as_inc_tax",
    )

    priced_stmt = select(
        FPurchaseLine.id.label("current_line_id"),
        FPurchaseLine.order_id.label("current_order_id"),
        FPurchaseLine.part_id.label("part_id"),
        FPurchaseLine.pn_std.label("pn_std"),
        FPurchaseLine.qty.label("current_qty"),
        FPurchaseLine.unit.label("current_unit"),
        FPurchaseLine.unit_price.label("current_unit_price_raw"),
        FPurchaseLine.import_batch_id.label("current_import_batch_id"),
        FPurchaseOrder.order_no.label("current_order_no"),
        FPurchaseOrder.order_date.label("current_order_date"),
        normalized_type.label("purchase_type"),
        normalized_price.label("current_unit_price_ex_tax"),
        tax_basis.label("current_tax_basis"),
    ).join(FPurchaseOrder, FPurchaseOrder.id == FPurchaseLine.order_id).where(
        FPurchaseOrder.data_status == config.ACTIVE_STATUS,
        FPurchaseOrder.order_date.is_not(None),
        FPurchaseOrder.order_date <= date_to,
        FPurchaseLine.part_id.is_not(None),
        FPurchaseLine.qty.is_not(None),
        FPurchaseLine.qty > 0,
        FPurchaseLine.unit_price.is_not(None),
        FPurchaseLine.unit_price > 0,
    )
    if purchase_type is not None:
        priced_stmt = priced_stmt.where(normalized_type == purchase_type)
    priced = priced_stmt.cte("calibration_priced")

    ordering = (
        priced.c.current_order_date,
        priced.c.current_order_id,
        priced.c.current_line_id,
    )
    partition = (priced.c.part_id, priced.c.purchase_type)

    def previous(expr):
        return func.lag(expr).over(partition_by=partition, order_by=ordering)

    adjacent = select(
        *priced.c,
        previous(priced.c.current_line_id).label("previous_line_id"),
        previous(priced.c.current_order_id).label("previous_order_id"),
        previous(priced.c.current_order_no).label("previous_order_no"),
        previous(priced.c.current_order_date).label("previous_order_date"),
        previous(priced.c.current_qty).label("previous_qty"),
        previous(priced.c.current_unit).label("previous_unit"),
        previous(priced.c.current_unit_price_raw).label("previous_unit_price_raw"),
        previous(priced.c.current_unit_price_ex_tax).label("previous_unit_price_ex_tax"),
        previous(priced.c.current_tax_basis).label("previous_tax_basis"),
        previous(priced.c.current_import_batch_id).label("previous_import_batch_id"),
    ).cte("calibration_adjacent")

    ratio = func.greatest(
        adjacent.c.current_unit_price_ex_tax / adjacent.c.previous_unit_price_ex_tax,
        adjacent.c.previous_unit_price_ex_tax / adjacent.c.current_unit_price_ex_tax,
    )
    direction = case(
        (adjacent.c.current_unit_price_ex_tax > adjacent.c.previous_unit_price_ex_tax,
         "increase"),
        (adjacent.c.current_unit_price_ex_tax < adjacent.c.previous_unit_price_ex_tax,
         "decrease"),
        else_="unchanged",
    )
    pair_stmt = select(
        *adjacent.c,
        ratio.label("ratio"),
        direction.label("direction"),
    ).where(adjacent.c.previous_unit_price_ex_tax > 0)
    if date_from is not None:
        # 窗口只限制“本次”；priced 仍包含窗口前的最近前值。
        pair_stmt = pair_stmt.where(adjacent.c.current_order_date >= date_from)
    pairs = pair_stmt.cte("calibration_pairs").prefix_with("MATERIALIZED")

    thresholds = values(
        column("multiplier", Integer), name="calibration_threshold_values",
    ).data([(value,) for value in THRESHOLDS]).cte("calibration_thresholds")

    data_through_stmt = select(func.max(priced.c.current_order_date))
    if date_from is not None:
        data_through_stmt = data_through_stmt.where(
            priced.c.current_order_date >= date_from,
        )
    meta = select(
        literal("meta").label("kind"),
        func.jsonb_build_object(
            "eligible_pairs", func.count(),
            "distinct_parts", func.count(func.distinct(pairs.c.part_id)),
            # 有筛选范围内的有效采购、但尚未形成相邻对时也要给出真实截止日。
            "data_through", data_through_stmt.scalar_subquery(),
        ).label("payload"),
    ).select_from(pairs)

    candidate_count = func.count().filter(pairs.c.ratio >= thresholds.c.multiplier)
    stats = select(
        literal("stat").label("kind"),
        func.jsonb_build_object(
            "purchase_type", pairs.c.purchase_type,
            "direction", pairs.c.direction,
            "multiplier", thresholds.c.multiplier,
            "comparable_pairs", func.count(),
            "candidate_pairs", candidate_count,
        ).label("payload"),
    ).select_from(pairs.join(thresholds, true())).group_by(
        pairs.c.purchase_type,
        pairs.c.direction,
        thresholds.c.multiplier,
    )

    stable_key = func.md5(func.concat(
        literal(f"{RULE_VERSION}:"),
        cast(pairs.c.previous_line_id, String),
        literal(":"),
        cast(pairs.c.current_line_id, String),
    ))
    ranked_samples = select(
        thresholds.c.multiplier,
        pairs,
        func.row_number().over(
            partition_by=(thresholds.c.multiplier, pairs.c.direction),
            order_by=(stable_key, pairs.c.previous_line_id, pairs.c.current_line_id),
        ).label("sample_rank"),
    ).select_from(pairs.join(thresholds, true())).where(
        pairs.c.direction.in_(("increase", "decrease")),
        pairs.c.ratio >= thresholds.c.multiplier,
    ).cte("calibration_ranked_samples")

    samples = select(
        literal("sample").label("kind"),
        func.jsonb_build_object(
            "multiplier", ranked_samples.c.multiplier,
            "sample_rank", ranked_samples.c.sample_rank,
            "direction", ranked_samples.c.direction,
            "ratio", ranked_samples.c.ratio,
            "purchase_type", ranked_samples.c.purchase_type,
            "part_id", ranked_samples.c.part_id,
            "pn_std", ranked_samples.c.pn_std,
            "previous_line_id", ranked_samples.c.previous_line_id,
            "previous_order_id", ranked_samples.c.previous_order_id,
            "previous_order_no", ranked_samples.c.previous_order_no,
            "previous_order_date", ranked_samples.c.previous_order_date,
            "previous_qty", ranked_samples.c.previous_qty,
            "previous_unit", ranked_samples.c.previous_unit,
            "previous_unit_price_raw", ranked_samples.c.previous_unit_price_raw,
            "previous_unit_price_ex_tax", ranked_samples.c.previous_unit_price_ex_tax,
            "previous_tax_basis", ranked_samples.c.previous_tax_basis,
            "previous_import_batch_id", ranked_samples.c.previous_import_batch_id,
            "current_line_id", ranked_samples.c.current_line_id,
            "current_order_id", ranked_samples.c.current_order_id,
            "current_order_no", ranked_samples.c.current_order_no,
            "current_order_date", ranked_samples.c.current_order_date,
            "current_qty", ranked_samples.c.current_qty,
            "current_unit", ranked_samples.c.current_unit,
            "current_unit_price_raw", ranked_samples.c.current_unit_price_raw,
            "current_unit_price_ex_tax", ranked_samples.c.current_unit_price_ex_tax,
            "current_tax_basis", ranked_samples.c.current_tax_basis,
            "current_import_batch_id", ranked_samples.c.current_import_batch_id,
        ).label("payload"),
    ).where(ranked_samples.c.sample_rank <= sample_limit)

    combined = union_all(meta, stats, samples).subquery("calibration_result")
    # 该窗口查询当前快照约 300ms；失败上限防慢计划或锁等待占满 worker/连接池。
    db.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'"))
    try:
        result_rows = db.execute(select(combined.c.kind, combined.c.payload)).all()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "57014":
            raise CalibrationPreviewTimeout("规则校准预览查询超时") from exc
        raise

    meta_payload = next((row.payload for row in result_rows if row.kind == "meta"), {})
    raw_stats = [row.payload for row in result_rows if row.kind == "stat"]
    raw_samples = [row.payload for row in result_rows if row.kind == "sample"]

    counts: dict[tuple[str, str, int], tuple[int, int]] = {}
    for row in raw_stats:
        counts[(row["purchase_type"], row["direction"], int(row["multiplier"]))] = (
            int(row["comparable_pairs"]), int(row["candidate_pairs"]),
        )
    purchase_types = sorted(
        {key[0] for key in counts},
        key=lambda name: (
            -sum(counts.get((name, direction, THRESHOLDS[0]), (0, 0))[0]
                 for direction in ("increase", "decrease", "unchanged")),
            name,
        ),
    )
    eligible_total = int(meta_payload.get("eligible_pairs") or 0)

    type_rows: list[dict[str, Any]] = []
    direction_rows: list[dict[str, Any]] = []
    for name in purchase_types:
        type_eligible = sum(
            counts.get((name, direction, THRESHOLDS[0]), (0, 0))[0]
            for direction in ("increase", "decrease", "unchanged")
        )
        type_rows.append({
            "purchase_type": name,
            "eligible_pairs": type_eligible,
            "thresholds": _threshold_rows(
                counts, purchase_types=[name], eligible=type_eligible,
            ),
        })
        for direction_name in ("increase", "decrease"):
            comparable = counts.get(
                (name, direction_name, THRESHOLDS[0]), (0, 0),
            )[0]
            if not comparable:
                continue
            direction_rows.append({
                "purchase_type": name,
                "direction": direction_name,
                "comparable_pairs": comparable,
                "thresholds": [{
                    "multiplier": multiplier,
                    "candidate_pairs": counts.get(
                        (name, direction_name, multiplier), (0, 0),
                    )[1],
                    "candidate_rate": _rate(
                        counts.get((name, direction_name, multiplier), (0, 0))[1],
                        comparable,
                    ),
                } for multiplier in THRESHOLDS],
            })

    direction_order = {"increase": 0, "decrease": 1}
    raw_samples.sort(key=lambda row: (
        int(row["multiplier"]), direction_order[row["direction"]],
        int(row["sample_rank"]), int(row["current_line_id"]),
    ))

    return {
        "rule_code": RULE_CODE,
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_through": meta_payload.get("data_through"),
        "parameters": {
            "date_from": date_from,
            "date_to": date_to,
            "purchase_type": purchase_type,
            "sample_limit": sample_limit,
        },
        "eligible_pairs": eligible_total,
        "distinct_parts": int(meta_payload.get("distinct_parts") or 0),
        "thresholds": _threshold_rows(
            counts, purchase_types=purchase_types, eligible=eligible_total,
        ),
        "purchase_types": type_rows,
        "direction_groups": direction_rows,
        "samples": raw_samples,
        "sample_boundary": {
            "limit_per_threshold_direction": sample_limit,
            "ordering": "md5(preview-v1:previous_line_id:current_line_id), line ids",
            "contains_people_or_parties": False,
        },
    }
