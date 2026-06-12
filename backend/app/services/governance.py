"""数据治理（报告#25/#11）：暴露数据质量问题，支持把非标型号排除出利润统计。

非标候选判定：pn_std 不含任何字母数字(纯中文/符号，如"一批备件""配件")，或命中关键词。
这些型号的成本/毛利往往乱套(把整批采购成本套到一笔小单)，应人工确认后排除。
"""
from decimal import Decimal

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysAuditLog

# 非标关键词（笼统/打包型号）
_NONSTD_KEYWORDS = ["一批", "配件", "打包", "若干", "其他", "其它", "杂项", "测试"]
# 纯中文/无字母数字 → 不是规范 PN
_NO_ALNUM = "pn_std !~ '[A-Za-z0-9]'"


class GovernanceError(Exception):
    """治理操作非法（如对已合并墓碑行操作）。"""


def _nonstd_clause():
    kw = or_(*[DimPart.pn_std.like(f"%{k}%") for k in _NONSTD_KEYWORDS])
    return or_(kw, text(_NO_ALNUM))


def _f(x):
    return float(x) if isinstance(x, Decimal) else x


def summary(db: Session) -> dict:
    total_parts = db.scalar(select(func.count()).select_from(DimPart))
    nonstd = db.scalar(select(func.count()).select_from(DimPart).where(_nonstd_clause()))
    needs_review = db.scalar(
        select(func.count()).select_from(DimPart).where(DimPart.needs_review.is_(True)))
    excluded = db.scalar(
        select(func.count()).select_from(DimPart).where(DimPart.is_excluded.is_(True)))

    def flag_count(flag):
        return db.scalar(
            select(func.count()).select_from(FSalesLine)
            .where(func.array_position(FSalesLine.anomaly_flags, flag).is_not(None)))

    fallback = db.scalar(
        select(func.count()).select_from(FSalesLine).where(FSalesLine.cost_source == "fallback"))

    return {
        "parts_total": total_parts,
        "nonstd_candidates": nonstd,
        "needs_review": needs_review,
        "excluded": excluded,
        "sales_no_cost": flag_count("no_cost"),
        "sales_neg_margin": flag_count("neg_margin"),
        "sales_zero_price": flag_count("zero_price"),
        "sales_amount_mismatch": flag_count("amount_mismatch"),
        "sales_fallback_cost": fallback,
    }


def list_parts(db: Session, kind: str, page: int, page_size: int) -> dict:
    """kind: nonstd | needs_review | excluded。返回带销量/营收/毛利率的型号清单。"""
    if kind == "needs_review":
        cond = DimPart.needs_review.is_(True)
    elif kind == "excluded":
        cond = DimPart.is_excluded.is_(True)
    else:
        cond = _nonstd_clause()
    cond = and_(cond, DimPart.status != "merged")   # 墓碑行不进治理清单

    total = db.scalar(select(func.count()).select_from(DimPart).where(cond))
    parts = db.execute(
        select(DimPart).where(cond).order_by(DimPart.pn_std)
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()

    items = []
    for p in parts:
        agg = db.execute(
            select(
                func.count().label("lines"),
                func.sum(FSalesLine.revenue_amount).filter(FSalesLine.counts_revenue.is_(True)),
                func.sum(FSalesLine.gross_profit).filter(FSalesLine.counts_revenue.is_(True)),
            ).join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
            .where(FSalesLine.part_id == p.id)
        ).one()
        rev = agg[1]
        gp = agg[2]
        margin = (float(gp) / float(rev)) if gp is not None and rev else None
        items.append({
            "pn_std": p.pn_std, "description": p.description,
            "needs_review": p.needs_review, "is_excluded": p.is_excluded,
            "exclude_reason": p.exclude_reason,
            "sales_lines": agg[0], "revenue": _f(rev),
            "gross_margin": round(margin, 4) if margin is not None else None,
        })
    return {"kind": kind, "total": total, "page": page, "page_size": page_size, "items": items}


def set_excluded(db: Session, pn_std: str, excluded: bool, reason: str | None,
                 operated_by: str | None) -> dict | None:
    p = db.scalar(select(DimPart).where(DimPart.pn_std == pn_std))
    if p is None:
        return None
    if p.status == "merged":
        # 墓碑行不承载业务语义；排除应作用在合并目标上，拒绝并提示
        target = db.get(DimPart, p.merged_into_id) if p.merged_into_id else None
        raise GovernanceError(
            f"型号 {pn_std} 已合并入 {target.pn_std if target else '?'}，请对目标型号操作")
    before = {"is_excluded": p.is_excluded, "exclude_reason": p.exclude_reason}
    p.is_excluded = excluded
    p.exclude_reason = reason if excluded else None
    db.flush()
    db.add(SysAuditLog(entity_type="part", entity_id=p.id,
                       action="exclude" if excluded else "include",
                       before_json=before,
                       after_json={"is_excluded": excluded, "exclude_reason": reason},
                       reason=reason, operated_by=operated_by))
    db.commit()
    return {"pn_std": pn_std, "is_excluded": excluded}
