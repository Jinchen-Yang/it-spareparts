"""老板经营看板聚合（P1 第二阶段第一刀）。

只读分析。一切金额走**未税口径**（config.PROFIT_VAT_RATE=13%，见 profit._ex_tax_*），
与利润引擎同源；成本/毛利读 recompute 落库的 f_sales_line 计算字段。

核心防误导口径（甲方 2026-07-11）：
- 正式利润只统计**有成本的销售行**；看板同时给 **成本覆盖率** 与 **未配成本营收**，否则毛利虚高。
- 经营 KPI **默认排除未来日期**（如 2026-12-02 的销售单），并单独计入"未来日期"异常。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from sqlalchemy import exists, or_

from app import config, security
from app.models.dimensions import DimCustomer, DimPart
from app.models.inventory import PartPool, PartPoolMember
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services import pool_metrics
from app.services.query_filters import active_orders
# 税价换算收敛到 pricing 单一真值源（复审二轮 Standards：财务规则防多处漂移）。
# 保留 `_`-前缀别名，本模块内既有调用点不动。
from app.services.pricing import (
    purchase_ex_tax_expr as _purchase_ex_tax_expr,
    purchase_ex_unit as _purchase_ex_unit,
    sale_ex_unit as _sale_ex_unit,
)

# 取消/作废口径：非"已生效"里明确终止的两种状态（其余如进行中/草稿单独计）
_CANCELLED_STATUS = ("已取消", "作废")


def _f(x) -> float | None:
    return float(x) if x is not None else None


def _r(x, n=2) -> float | None:
    return round(float(x), n) if x is not None else None


def kpi(db: Session, date_from: date | None, date_to: date | None,
        as_of: date | None = None, user_ctx: security.UserContext | None = None) -> dict:
    """顶部经营指标。金额未税。默认排除未来日期（> as_of）。

    返回：销售额/采购额/已配成本销售额/毛利额/毛利率/成本覆盖率/未配成本营收/被排除营收
    + 订单健康（已生效/进行中/取消/异常行/未来日期单）。
    """
    today = as_of or date.today()
    upper = min(date_to, today) if date_to else today   # 未来日期一律排除出 KPI

    sl = FSalesLine
    counts = sl.counts_revenue.is_(True)
    costed = and_(counts, sl.cost_moving_avg.is_not(None))

    # ---- 销售侧（已生效 + 计营收 + [from, min(to,today)]）----
    sales_stmt = (
        select(
            func.sum(sl.revenue_amount).filter(counts).label("sales_ex_tax"),
            func.sum(sl.revenue_amount).filter(costed).label("sales_costed"),
            func.sum(sl.gross_profit).filter(costed).label("gross_profit"),
            func.sum(sl.revenue_amount).filter(sl.counts_revenue.is_(False)).label("excluded_rev"),
            func.count().filter(and_(counts, func.cardinality(sl.anomaly_flags) > 0)).label("anomaly_lines"),
        )
        .join(FSalesOrder, sl.order_id == FSalesOrder.id)
    )
    sales_stmt = active_orders(sales_stmt, FSalesOrder)
    if date_from:
        sales_stmt = sales_stmt.where(FSalesOrder.order_date >= date_from)
    sales_stmt = sales_stmt.where(FSalesOrder.order_date <= upper)
    if user_ctx is not None:
        sales_stmt = security.apply_data_scope(sales_stmt, user_ctx)
    s = db.execute(sales_stmt).one()

    sales_ex = s.sales_ex_tax or Decimal(0)
    sales_costed = s.sales_costed or Decimal(0)
    gross = s.gross_profit or Decimal(0)
    margin = (gross / sales_costed) if sales_costed else None            # 毛利率分母=已配成本营收
    coverage = (sales_costed / sales_ex) if sales_ex else None           # 成本覆盖率
    uncosted = sales_ex - sales_costed                                   # 未配成本营收（利润未计）

    # ---- 采购侧（已生效 + [from, min(to,today)]，未税额）----
    pur_stmt = (
        select(func.sum(_purchase_ex_tax_expr()).label("purchase_ex_tax"))
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
    )
    pur_stmt = active_orders(pur_stmt, FPurchaseOrder)
    if date_from:
        pur_stmt = pur_stmt.where(FPurchaseOrder.order_date >= date_from)
    pur_stmt = pur_stmt.where(FPurchaseOrder.order_date <= upper)
    purchase_ex = db.execute(pur_stmt).scalar() or Decimal(0)

    # ---- 订单健康（全状态，销售+采购，[from, to] 不裁未来——反而要数出未来单）----
    counts_by_status = _order_health(db, date_from, date_to, today)

    return {
        "window": {"date_from": date_from.isoformat() if date_from else None,
                   "date_to": date_to.isoformat() if date_to else None,
                   "as_of": today.isoformat(), "future_excluded": True},
        # 金额（未税）
        "sales_ex_tax": _f(sales_ex),
        "purchase_ex_tax": _f(purchase_ex),
        "sales_costed_ex_tax": _f(sales_costed),
        "gross_profit": _f(gross),
        "gross_margin": round(float(margin), 4) if margin is not None else None,
        "cost_coverage": round(float(coverage), 4) if coverage is not None else None,
        "sales_uncosted_ex_tax": _f(uncosted),
        "excluded_revenue": _f(s.excluded_rev),
        # 订单健康
        **counts_by_status,
        "anomaly_lines": s.anomaly_lines or 0,
    }


def _purchase_price_stats(db: Session, date_from: date | None, upper: date) -> dict[int, dict]:
    """每 part_id 的采购价统计（未税）：加权均价/中位价/最低/最高/样本数/最近采购日。
    口径：已生效 + 计入成本的采购类型(COST_PURCHASE_TYPES) + 单价>0。
    最低/最高仅作参考（可能异常低/小量/脏数据），标杆看加权均价/中位价（甲方评审）。"""
    ex = _purchase_ex_unit()
    stmt = (
        select(
            FPurchaseLine.part_id,
            (func.sum(_purchase_ex_tax_expr()) / func.nullif(func.sum(FPurchaseLine.qty), 0)).label("wavg"),
            func.percentile_cont(0.5).within_group(ex).label("median"),
            func.min(ex).label("pmin"), func.max(ex).label("pmax"),
            func.count().label("samples"), func.max(FPurchaseOrder.order_date).label("last_date"),
        )
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.unit_price.is_not(None), FPurchaseLine.unit_price > 0,
               FPurchaseLine.qty.is_not(None), FPurchaseLine.qty > 0,
               FPurchaseOrder.source_type.in_(config.COST_PURCHASE_TYPES))
    )
    stmt = active_orders(stmt, FPurchaseOrder)
    if date_from:
        stmt = stmt.where(FPurchaseOrder.order_date >= date_from)
    stmt = stmt.where(FPurchaseOrder.order_date <= upper).group_by(FPurchaseLine.part_id)
    out = {}
    for r in db.execute(stmt):
        out[r.part_id] = {"wavg": _r(r.wavg), "median": _r(r.median), "min": _r(r.pmin),
                          "max": _r(r.pmax), "samples": r.samples,
                          "last_date": r.last_date.isoformat() if r.last_date else None}
    return out


def _sale_price_stats(db: Session, date_from: date | None, upper: date) -> dict[int, dict]:
    """每 part_id 的销售价统计（未税）：加权均价/中位价/最低/最高/样本数/最近成交日。
    口径：已生效 + 计营收 + 单价>0（¥0 赠送/换货不计价）。"""
    ex = _sale_ex_unit()
    stmt = (
        select(
            FSalesLine.part_id,
            (func.sum(FSalesLine.revenue_amount).filter(FSalesLine.counts_revenue.is_(True))
             / func.nullif(func.sum(FSalesLine.qty).filter(FSalesLine.counts_revenue.is_(True)), 0)).label("wavg"),
            func.percentile_cont(0.5).within_group(ex).label("median"),
            func.min(ex).label("smin"), func.max(ex).label("smax"),
            func.count().label("samples"), func.max(FSalesOrder.order_date).label("last_date"),
        )
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.counts_revenue.is_(True),
               FSalesLine.unit_price.is_not(None), FSalesLine.unit_price > 0,
               FSalesLine.qty.is_not(None), FSalesLine.qty > 0)
    )
    stmt = active_orders(stmt, FSalesOrder)
    if date_from:
        stmt = stmt.where(FSalesOrder.order_date >= date_from)
    stmt = stmt.where(FSalesOrder.order_date <= upper).group_by(FSalesLine.part_id)
    out = {}
    for r in db.execute(stmt):
        out[r.part_id] = {"wavg": _r(r.wavg), "median": _r(r.median), "min": _r(r.smin),
                          "max": _r(r.smax), "samples": r.samples,
                          "last_date": r.last_date.isoformat() if r.last_date else None}
    return out


RANKING_SORTS = ("gross_profit", "revenue", "qty_sold", "order_count")


def part_ranking(db: Session, date_from: date | None, date_to: date | None,
                 cost_method: str = "moving_avg", top: int = 20,
                 part_id: int | None = None, pn: str | None = None,
                 pool_group_id: int | None = None,
                 sort: str = "gross_profit", order: str = "desc",
                 page: int = 1, page_size: int = 50,
                 as_of: date | None = None, user_ctx: security.UserContext | None = None) -> dict:
    """型号盈亏排名（未税双成本法）：赚钱榜 + 亏损榜，各带采购/销售价统计。

    毛利按 cost_method(moving_avg|fifo) 排序；两法毛利都返回。默认排除未来日期。
    只统计计营收且已配成本的行进毛利；无成本行只计营收（coverage 反映）。

    v2 增量：part_id（精确，优先于 pn）/ pn（pn_std 全等，不做模糊——相似 PN 不得混入）/
    pool_group_id（限有效池成员）筛选；items 分页块（全量榜单按 sort/order 服务端排序）；
    行内补池归属 pool_group_id/pool_name 与去重 order_count。旧 profitable/loss/counts 原样。
    """
    today = as_of or date.today()
    upper = min(date_to, today) if date_to else today
    use_fifo = cost_method == "fifo"

    sl = FSalesLine
    counts = sl.counts_revenue.is_(True)
    costed = and_(counts, sl.cost_moving_avg.is_not(None))
    stmt = (
        select(
            sl.part_id, DimPart.pn_std, DimPart.description, DimPart.brand,
            func.sum(sl.revenue_amount).filter(counts).label("revenue"),
            func.sum(sl.qty).filter(counts).label("qty_sold"),
            func.sum(sl.revenue_amount).filter(costed).label("rev_costed"),
            func.sum(sl.cost_moving_avg * sl.qty).filter(costed).label("cost_ma"),
            func.sum(sl.cost_fifo * sl.qty).filter(costed).label("cost_ff"),
            func.count().filter(counts).label("lines"),
            func.count(func.distinct(sl.order_id)).filter(counts).label("order_count"),
            func.count().filter(func.array_position(sl.anomaly_flags, "no_cost").is_not(None)).label("no_cost"),
        )
        .join(FSalesOrder, sl.order_id == FSalesOrder.id)
        .join(DimPart, sl.part_id == DimPart.id)
    )
    stmt = active_orders(stmt, FSalesOrder)
    if date_from:
        stmt = stmt.where(FSalesOrder.order_date >= date_from)
    stmt = stmt.where(FSalesOrder.order_date <= upper)
    # 精确筛选：part_id 优先；pn 走 pn_std 全等（绝不 ILIKE——"PN-A"不得召回"PN-A1"）
    pn_clean = (pn or "").strip() or None
    if part_id is not None:
        stmt = stmt.where(sl.part_id == part_id)
    elif pn_clean:
        stmt = stmt.where(DimPart.pn_std == pn_clean)
    if pool_group_id is not None:
        member_sub = (select(PartPoolMember.part_id)
                      .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
                      .where(PartPoolMember.group_id == pool_group_id,
                             PartPool.status == "active"))
        stmt = stmt.where(sl.part_id.in_(member_sub))
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)
    stmt = stmt.group_by(sl.part_id, DimPart.pn_std, DimPart.description, DimPart.brand)

    pstats = _purchase_price_stats(db, date_from, upper)
    sstats = _sale_price_stats(db, date_from, upper)

    def _gp(rev, cost):
        return _r(float(rev) - float(cost)) if rev is not None and cost is not None else None

    def _margin(rev, cost):
        return round((float(rev) - float(cost)) / float(rev), 4) if rev and cost is not None and float(rev) else None

    rows = []
    for r in db.execute(stmt):
        rc = r.rev_costed
        gp_mov, gp_ff = _gp(rc, r.cost_ma), _gp(rc, r.cost_ff)
        rows.append({
            "part_id": r.part_id, "pn_std": r.pn_std, "description": r.description, "brand": r.brand,
            "revenue": _f(r.revenue), "qty_sold": _f(r.qty_sold),
            "order_count": r.order_count,
            "revenue_costed": _f(rc),
            "cost_coverage": round(float(rc) / float(r.revenue), 4) if rc and r.revenue else None,
            "no_cost": r.no_cost, "lines": r.lines,
            "gross_profit_moving": gp_mov, "gross_margin_moving": _margin(rc, r.cost_ma),
            "gross_profit_fifo": gp_ff, "gross_margin_fifo": _margin(rc, r.cost_ff),
            "purchase_price": pstats.get(r.part_id), "sale_price": sstats.get(r.part_id),
            "_sort": (gp_ff if use_fifo else gp_mov),
        })

    # 池归属（批量一条，防 N+1）
    pool_map = pool_metrics.active_pool_map(db, [x["part_id"] for x in rows])
    for x in rows:
        pm = pool_map.get(x["part_id"])
        x["pool_group_id"] = pm["group_id"] if pm else None
        x["pool_name"] = pm["pool_name"] if pm else None

    # 确定性地基：GROUP BY 输出无序（HashAggregate 不保证顺序），先按 part_id 定序，
    # 后续所有 sorted() 均稳定 → 指标并列时以 part_id 破并列，分页不丢行/重行（审计 P1）。
    rows.sort(key=lambda x: x["part_id"])

    # 有成本才进赚钱/亏损榜（无成本行毛利未知，单独留在 coverage 里，不硬塞进盈亏榜误导）
    ranked = [x for x in rows if x["_sort"] is not None]
    n_profit = sum(1 for x in ranked if x["_sort"] > 0)
    n_loss = sum(1 for x in ranked if x["_sort"] < 0)
    profitable = sorted([x for x in ranked if x["_sort"] > 0], key=lambda x: x["_sort"], reverse=True)[:top]
    loss = sorted([x for x in ranked if x["_sort"] < 0], key=lambda x: x["_sort"])[:top]

    window = {"date_from": date_from.isoformat() if date_from else None,
              "date_to": date_to.isoformat() if date_to else None,
              "as_of": today.isoformat(), "cost_method": cost_method}
    filters = {"part_id": part_id, "pn": pn_clean, "pool_group_id": pool_group_id}
    profit_restricted = security.is_field_hidden(user_ctx, "gross_profit")

    # items 分页块：全量榜单服务端排序。毛利排序对无利润权限角色是行序侧信道 →
    # 退回按营收排序（与订单端点 ranking_restricted 同一先例）。
    if sort not in RANKING_SORTS:
        sort = "gross_profit"
    ranking_restricted = profit_restricted and sort == "gross_profit"
    effective_sort = "revenue" if ranking_restricted else sort
    desc = order != "asc"

    def _key(x):
        v = x["_sort"] if effective_sort == "gross_profit" else x.get(effective_sort)
        if v is None:
            return float("-inf") if desc else float("inf")
        return v

    items_all = sorted(rows, key=_key, reverse=desc)
    items_page = items_all[(page - 1) * page_size: page * page_size]
    items = {"total": len(rows), "page": page, "page_size": page_size,
             "sort": sort, "effective_sort": effective_sort, "order": order,
             "ranking_restricted": ranking_restricted, "items": items_page}

    for x in rows:
        x.pop("_sort", None)

    # 结构性收敛（复审三轮 P0-1）：data_profit=false 时，连"哪些型号赚/亏、各几个"都不能给——
    # 字段 mask 只置空金额，型号落在哪个榜 + 榜内计数本身泄漏利润结论。整块归属一律撤下。
    # items 块无盈亏归属（非毛利排序 + 金额键随组脱敏），可保留。
    if profit_restricted:
        return {
            "window": window, "filters": filters, "profit_restricted": True,
            "profitable": [], "loss": [],
            "counts": {"total_parts": len(rows), "with_cost": len(ranked),
                       "profitable": None, "loss": None,
                       "no_cost_parts": len(rows) - len(ranked)},
            "ranking": items,
        }
    return {
        "window": window, "filters": filters, "profit_restricted": False,
        "profitable": profitable, "loss": loss,
        "counts": {"total_parts": len(rows), "with_cost": len(ranked),
                   "profitable": n_profit, "loss": n_loss,
                   "no_cost_parts": len(rows) - len(ranked)},
        "ranking": items,
    }


_GRAIN = {"day": "day", "week": "week", "month": "month"}


def trend(db: Session, date_from: date | None, date_to: date | None,
          granularity: str = "day", as_of: date | None = None,
          user_ctx: security.UserContext | None = None) -> dict:
    """经营趋势：销售额/采购额/毛利额（未税）按日/周/月。默认排除未来日期。

    毛利只累计已配成本行；销售额累计全部计营收行——同一桶里毛利<销售额属正常
    （无成本行有营收无毛利），前端可据此看覆盖缺口。
    """
    today = as_of or date.today()
    upper = min(date_to, today) if date_to else today
    grain = _GRAIN.get(granularity, "day")

    sl = FSalesLine
    counts = sl.counts_revenue.is_(True)
    costed = and_(counts, sl.cost_moving_avg.is_not(None))
    s_bucket = func.date_trunc(grain, FSalesOrder.order_date)
    s_stmt = (
        select(s_bucket.label("b"),
               func.sum(sl.revenue_amount).filter(counts).label("sales"),
               func.sum(sl.gross_profit).filter(costed).label("gp"))
        .join(FSalesOrder, sl.order_id == FSalesOrder.id)
    )
    s_stmt = active_orders(s_stmt, FSalesOrder)
    if date_from:
        s_stmt = s_stmt.where(FSalesOrder.order_date >= date_from)
    s_stmt = s_stmt.where(FSalesOrder.order_date <= upper)
    if user_ctx is not None:
        s_stmt = security.apply_data_scope(s_stmt, user_ctx)
    s_stmt = s_stmt.group_by(s_bucket)

    p_bucket = func.date_trunc(grain, FPurchaseOrder.order_date)
    p_stmt = (
        select(p_bucket.label("b"), func.sum(_purchase_ex_tax_expr()).label("purchase"))
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
    )
    p_stmt = active_orders(p_stmt, FPurchaseOrder)
    if date_from:
        p_stmt = p_stmt.where(FPurchaseOrder.order_date >= date_from)
    p_stmt = p_stmt.where(FPurchaseOrder.order_date <= upper).group_by(p_bucket)

    buckets: dict[str, dict] = {}

    def _key(b):
        return b.date().isoformat() if hasattr(b, "date") else b.isoformat()

    for r in db.execute(s_stmt):
        if r.b is None:
            continue
        buckets.setdefault(_key(r.b), {"sales_ex_tax": 0.0, "purchase_ex_tax": 0.0, "gross_profit": 0.0})
        buckets[_key(r.b)]["sales_ex_tax"] = _f(r.sales) or 0.0
        buckets[_key(r.b)]["gross_profit"] = _f(r.gp) or 0.0
    for r in db.execute(p_stmt):
        if r.b is None:
            continue
        buckets.setdefault(_key(r.b), {"sales_ex_tax": 0.0, "purchase_ex_tax": 0.0, "gross_profit": 0.0})
        buckets[_key(r.b)]["purchase_ex_tax"] = _f(r.purchase) or 0.0

    series = [{"period": k, **v} for k, v in sorted(buckets.items())]
    return {"granularity": grain, "as_of": today.isoformat(),
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "series": series}


def _sales_parts(db: Session, order_ids: list[int], date_from: date | None, upper: date,
                 manual_restricted: bool) -> dict[int, list[dict]]:
    """当页销售订单的行明细批量装配（固定 ≤5 条 SQL，与订单数/页大小无关，防 N+1）：
    ① 当页全部行+主数据 ② 行内 PN→有效池映射 ③ 涉及池当前约束价
    ④ 涉及池窗口销售加权均价（③④ 在 pool_metrics 内各一条）。"""
    if not order_ids:
        return {}
    so = FSalesOrder
    rows = db.execute(
        select(FSalesLine.order_id, FSalesLine.id, FSalesLine.part_id,
               DimPart.pn_std, DimPart.description, DimPart.brand,
               FSalesLine.qty, _sale_ex_unit().label("unit_ex"),
               FSalesLine.revenue_amount, FSalesLine.counts_revenue,
               so.data_status, so.order_date)
        .join(so, FSalesLine.order_id == so.id)
        .join(DimPart, FSalesLine.part_id == DimPart.id)
        .where(FSalesLine.order_id.in_(order_ids))
        .order_by(FSalesLine.order_id, FSalesLine.id)
    ).all()
    pool_map = pool_metrics.active_pool_map(db, [r.part_id for r in rows])
    gids = {v["group_id"] for v in pool_map.values()}
    policies = pool_metrics.current_policies(db, gids)
    gstats = pool_metrics.sales_group_stats(db, date_from, upper, group_ids=gids) if gids else {}
    out: dict[int, list[dict]] = {}
    for r in rows:
        pm = pool_map.get(r.part_id)
        gid = pm["group_id"] if pm else None
        pool_avg = (gstats.get(gid) or {}).get("weighted_avg_unit_price") if gid else None
        floor = (policies.get(gid) or {}).get("sales_floor_ex_tax") if gid else None
        # 判定用未税原值（round 是输出规则不是比较输入，防与池级 SQL 计数在分厘边界打架）
        unit_raw = float(r.unit_ex) if r.unit_ex is not None else None
        ref = pool_metrics.price_reference("sales", unit_raw, gid is not None,
                                           pool_avg, floor, manual_restricted)
        # 该行是否在池统计口径内（已生效+非未来+计营收+计价）：页面红标数与池
        # violation_count 的对账钩子——展示行全打标签，但计数只认口径内的行
        in_scope = (r.data_status == config.ACTIVE_STATUS
                    and bool(r.order_date and r.order_date <= upper)
                    and bool(r.counts_revenue)
                    and unit_raw is not None and unit_raw > 0
                    and r.qty is not None and r.qty > 0)
        out.setdefault(r.order_id, []).append({
            "line_id": r.id, "part_id": r.part_id, "pn_std": r.pn_std,
            "description": r.description, "brand": r.brand,
            "quantity": _f(r.qty), "unit_price_ex_tax": _r(r.unit_ex),
            "amount": _f(r.revenue_amount), "counts_revenue": bool(r.counts_revenue),
            "in_stats_scope": in_scope,
            "pool_group_id": gid, "pool_name": pm["pool_name"] if pm else None,
            "pool_avg_sale_price": pool_avg,
            "min_sale_price": None if manual_restricted else floor,
            **ref,
        })
    return out


def _purchase_parts(db: Session, order_ids: list[int], date_from: date | None, upper: date,
                    manual_restricted: bool) -> dict[int, list[dict]]:
    """当页采购订单的行明细批量装配（同 _sales_parts，固定 ≤5 条 SQL 防 N+1）。"""
    if not order_ids:
        return {}
    rows = db.execute(
        select(FPurchaseLine.order_id, FPurchaseLine.id, FPurchaseLine.part_id,
               DimPart.pn_std, DimPart.description, DimPart.brand,
               FPurchaseLine.qty, _purchase_ex_unit().label("unit_ex"),
               _purchase_ex_tax_expr().label("amount"),
               FPurchaseOrder.data_status, FPurchaseOrder.order_date,
               FPurchaseOrder.source_type)
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .join(DimPart, FPurchaseLine.part_id == DimPart.id)
        .where(FPurchaseLine.order_id.in_(order_ids))
        .order_by(FPurchaseLine.order_id, FPurchaseLine.id)
    ).all()
    pool_map = pool_metrics.active_pool_map(db, [r.part_id for r in rows])
    gids = {v["group_id"] for v in pool_map.values()}
    policies = pool_metrics.current_policies(db, gids)
    gstats = pool_metrics.purchase_group_stats(db, date_from, upper, group_ids=gids) if gids else {}
    out: dict[int, list[dict]] = {}
    for r in rows:
        pm = pool_map.get(r.part_id)
        gid = pm["group_id"] if pm else None
        pool_avg = (gstats.get(gid) or {}).get("weighted_avg_unit_price") if gid else None
        ceiling = (policies.get(gid) or {}).get("purchase_ceiling_ex_tax") if gid else None
        # 判定用未税原值（round 是输出规则不是比较输入，防与池级 SQL 计数在分厘边界打架）
        unit_raw = float(r.unit_ex) if r.unit_ex is not None else None
        ref = pool_metrics.price_reference("purchase", unit_raw, gid is not None,
                                           pool_avg, ceiling, manual_restricted)
        # 该行是否在池统计口径内（已生效+非未来+计成本采购类型+计价）——对账钩子
        in_scope = (r.data_status == config.ACTIVE_STATUS
                    and bool(r.order_date and r.order_date <= upper)
                    and r.source_type in config.COST_PURCHASE_TYPES
                    and unit_raw is not None and unit_raw > 0
                    and r.qty is not None and r.qty > 0)
        out.setdefault(r.order_id, []).append({
            "line_id": r.id, "part_id": r.part_id, "pn_std": r.pn_std,
            "description": r.description, "brand": r.brand,
            "quantity": _f(r.qty), "unit_price_ex_tax": _r(r.unit_ex),
            "amount": _r(r.amount),
            "in_stats_scope": in_scope,
            "pool_group_id": gid, "pool_name": pm["pool_name"] if pm else None,
            "pool_avg_purchase_price": pool_avg,
            "max_purchase_price": None if manual_restricted else ceiling,
            **ref,
        })
    return out


def _orders_containing_part(line_model, part_id: int | None, pool_group_id: int | None):
    """全局筛选（UI v2）：按"订单内含该型号 / 含该有效池成员"生成整单召回子查询。
    过滤在订单集合层做而非行 WHERE——聚合口径必须仍是整单（型号数/总量/金额不因筛选缩水）。"""
    conds = []
    if part_id is not None:
        conds.append(select(line_model.order_id).where(line_model.part_id == part_id))
    if pool_group_id is not None:
        member_sub = (select(PartPoolMember.part_id)
                      .join(PartPool, PartPool.group_id == PartPoolMember.group_id)
                      .where(PartPoolMember.group_id == pool_group_id,
                             PartPool.status == "active"))
        conds.append(select(line_model.order_id).where(line_model.part_id.in_(member_sub)))
    return conds


def sales_orders(db: Session, *, date_from: date | None = None, date_to: date | None = None,
                 status: str | None = None, q: str | None = None, order_no: str | None = None,
                 customer: str | None = None,
                 salesperson: str | None = None, business_type: str | None = None,
                 part_id: int | None = None, pool_group_id: int | None = None,
                 sort: str = "order_date", order: str = "desc",
                 page: int = 1, page_size: int = 50, as_of: date | None = None,
                 user_ctx: security.UserContext | None = None) -> dict:
    """订单拉通-销售侧：**一张销售订单一行**（复审 P1-4，此前是明细行粒度）。
    多型号聚合为 型号数/总量/总营收/总毛利。金额未税。status 留空=仅已生效、'全部'=不限。
    linked_purchase：是否有**已生效**采购单经 linked_sales_order_no 关联（复审：不再算取消单）。
    默认/具体状态视图排除未来单；status='全部' 保留管理诊断能力。

    受限销售（is_scoped_sales）按 security.py 的统一策略不可见任何逐单
    成交信息。必须在任何过滤/计数 SQL 前短路，避免用 customer/salesperson/
    order_no 等条件猜测某单是否存在。"""
    today = as_of or date.today()
    if security.is_scoped_sales(user_ctx):
        # 稳定的受限响应：total=None 明确表示“不可见”，不用 0 伪装成
        # “查无数据”；且不随任何查询条件/排序改变，消除存在性侧信道。
        return {
            "contract_version": 2, "total": None, "page": page, "page_size": page_size,
            "as_of": today.isoformat(), "effective_sort": None,
            "ranking_restricted": True, "profit_restricted": True,
            "parts_restricted": True, "orders_restricted": True,
            "manual_reference_restricted": security.is_field_hidden(
                user_ctx, "purchase_ceiling_ex_tax"),
            "items": [],
        }
    sl, so = FSalesLine, FSalesOrder
    counts, costed = sl.counts_revenue.is_(True), and_(sl.counts_revenue.is_(True), sl.cost_moving_avg.is_not(None))
    # 已生效采购单关联才算"拉通"（取消/进行中不算）
    linked = exists().where(and_(FPurchaseOrder.linked_sales_order_no == so.order_no,
                                 FPurchaseOrder.data_status == config.ACTIVE_STATUS))
    rev = func.sum(sl.revenue_amount).filter(counts)
    gp = func.sum(sl.gross_profit).filter(costed)
    base = (
        select(
            so.id, so.order_no, so.order_date, so.salesperson,
            DimCustomer.name_normalized.label("customer"), so.business_type, so.data_status,
            func.count(func.distinct(sl.part_id)).label("part_count"),
            func.sum(sl.qty).filter(counts).label("total_qty"),
            rev.label("total_revenue"), gp.label("total_gross_profit"),
            linked.label("linked_purchase"),
        )
        .join(sl, sl.order_id == so.id)
        .join(DimCustomer, so.customer_id == DimCustomer.id, isouter=True)
    )
    if status == "全部":
        pass
    elif status:
        base = base.where(so.data_status == status)
    else:
        base = active_orders(base, so)
    if date_from:
        base = base.where(so.order_date >= date_from)
    # 老板默认“最近有效单”不能被未来日期污染；只有显式
    # status='全部' 的管理诊断视图可看未来单。date_to 即使传到未来也被裁到 today。
    upper = date_to if status == "全部" else min(date_to, today) if date_to else today
    if upper:
        base = base.where(so.order_date <= upper)
    if order_no and order_no.strip():
        base = base.where(so.order_no == order_no.strip())
    if q and q.strip():
        kw = f"%{q.strip()}%"   # 订单粒度：单号直匹 或 含匹配型号（TODO 第②块接统一型号搜索）
        sub = (select(FSalesLine.order_id).join(DimPart, FSalesLine.part_id == DimPart.id)
               .where(or_(DimPart.pn_std.ilike(kw), FSalesLine.description.ilike(kw), FSalesLine.brand.ilike(kw))))
        base = base.where(or_(so.order_no.ilike(kw), so.id.in_(sub)))
    if customer:
        base = base.where(DimCustomer.name_normalized.ilike(f"%{customer.strip()}%"))
    if salesperson:
        base = base.where(so.salesperson.ilike(f"%{salesperson.strip()}%"))
    if business_type:
        base = base.where(so.business_type == business_type)
    for sub in _orders_containing_part(FSalesLine, part_id, pool_group_id):
        base = base.where(so.id.in_(sub))
    if user_ctx is not None:
        base = security.apply_data_scope(base, user_ctx)
    base = base.group_by(so.id, so.order_no, so.order_date, so.salesperson,
                         DimCustomer.name_normalized, so.business_type, so.data_status)

    total = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    # 结构性泄漏防护（复审三轮同类扩展）：利润被脱敏的角色不得按 gross_profit 排序——
    # 即便金额置空，行序本身泄漏盈亏排名。退回按日期排序。
    profit_restricted = security.is_field_hidden(user_ctx, "gross_profit")
    ranking_restricted = sort == "gross_profit" and profit_restricted
    effective_sort = "order_date" if ranking_restricted else sort
    if ranking_restricted:
        sort = "order_date"
    sort_expr = {"order_date": so.order_date, "revenue": rev, "gross_profit": gp,
                 "part_count": func.count(func.distinct(sl.part_id))}.get(sort, so.order_date)
    direction = sort_expr.desc().nullslast() if order == "desc" else sort_expr.asc().nullslast()
    stmt = base.order_by(direction, so.id.desc()).limit(page_size).offset((page - 1) * page_size)

    items = []
    for r in db.execute(stmt):
        items.append({
            "order_id": r.id, "order_no": r.order_no,
            "order_date": r.order_date.isoformat() if r.order_date else None,
            "occurred_date": r.order_date.isoformat() if r.order_date else None,
            "is_future": bool(r.order_date and r.order_date > today),
            "salesperson": r.salesperson, "customer": r.customer,
            "business_type": r.business_type, "data_status": r.data_status,
            "part_count": r.part_count, "pn_count": r.part_count,
            "total_qty": _f(r.total_qty), "total_quantity": _f(r.total_qty),
            "total_revenue": _f(r.total_revenue), "total_amount": _f(r.total_revenue),
            "total_gross_profit": _f(r.total_gross_profit),
            "linked_purchase": bool(r.linked_purchase),
        })
    # v2：嵌套 parts（价格参考随行）。受限销售已在入口整段短路。
    manual_restricted = security.is_field_hidden(user_ctx, "purchase_ceiling_ex_tax")
    stats_upper = min(date_to, today) if date_to else today
    parts_map = _sales_parts(
        db, [i["order_id"] for i in items], date_from, stats_upper, manual_restricted)
    for it in items:
        parts = parts_map.get(it["order_id"], [])
        it["parts"] = parts
        it["pn_preview"] = list(dict.fromkeys(p["pn_std"] for p in parts if p["pn_std"]))[:3]
    return {"contract_version": 2, "total": total, "page": page, "page_size": page_size,
            "as_of": today.isoformat(),
            "effective_sort": effective_sort, "ranking_restricted": ranking_restricted,
            "profit_restricted": profit_restricted, "parts_restricted": False,
            "orders_restricted": False,
            "manual_reference_restricted": manual_restricted, "items": items}


def purchase_orders(db: Session, *, date_from: date | None = None, date_to: date | None = None,
                    status: str | None = None, q: str | None = None,
                    order_no: str | None = None,
                    source_type: str | None = None, purchaser: str | None = None,
                    part_id: int | None = None, pool_group_id: int | None = None,
                    sort: str = "order_date", order: str = "desc",
                    page: int = 1, page_size: int = 50, as_of: date | None = None,
                    user_ctx: security.UserContext | None = None) -> dict:
    """订单拉通-采购侧：**一张采购订单一行**（看板内直接给采购订单列表，不再只让跳采购明细页）。
    金额未税。linked_sales_order：该采购单关联的销售单号（拉通）。"""
    today = as_of or date.today()
    po, pl = FPurchaseOrder, FPurchaseLine
    amt = func.sum(_purchase_ex_tax_expr())
    base = (
        select(
            po.id, po.order_no, po.order_date, po.purchaser, po.source_type, po.data_status,
            po.linked_sales_order_no, po.supplier_id,
            func.count(func.distinct(pl.part_id)).label("part_count"),
            func.sum(pl.qty).label("total_qty"), amt.label("total_ex_tax"),
        )
        .join(pl, pl.order_id == po.id)
    )
    if status == "全部":
        pass
    elif status:
        base = base.where(po.data_status == status)
    else:
        base = active_orders(base, po)
    if date_from:
        base = base.where(po.order_date >= date_from)
    upper = date_to if status == "全部" else min(date_to, today) if date_to else today
    if upper:
        base = base.where(po.order_date <= upper)
    if order_no and order_no.strip():
        base = base.where(po.order_no == order_no.strip())
    if q and q.strip():
        kw = f"%{q.strip()}%"   # 单号直匹 或 含匹配型号
        sub = (select(pl.order_id).join(DimPart, pl.part_id == DimPart.id)
               .where(or_(DimPart.pn_std.ilike(kw), pl.description.ilike(kw), pl.brand.ilike(kw))))
        base = base.where(or_(po.order_no.ilike(kw), po.id.in_(sub)))
    if source_type:
        base = base.where(po.source_type == source_type)
    if purchaser:
        base = base.where(po.purchaser.ilike(f"%{purchaser.strip()}%"))
    for sub in _orders_containing_part(FPurchaseLine, part_id, pool_group_id):
        base = base.where(po.id.in_(sub))
    base = base.group_by(po.id, po.order_no, po.order_date, po.purchaser, po.source_type,
                         po.data_status, po.linked_sales_order_no, po.supplier_id)
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    # 结构性泄漏防护：采购成本被脱敏的角色不得按 amount(未税采购额) 排序——行序泄漏采购额排名。
    cost_restricted = security.is_field_hidden(user_ctx, "total_ex_tax")
    ranking_restricted = sort == "amount" and cost_restricted
    effective_sort = "order_date" if ranking_restricted else sort
    if ranking_restricted:
        sort = "order_date"
    sort_expr = {"order_date": po.order_date, "amount": amt, "part_count": func.count(func.distinct(pl.part_id))}.get(sort, po.order_date)
    direction = sort_expr.desc().nullslast() if order == "desc" else sort_expr.asc().nullslast()
    stmt = base.order_by(direction, po.id.desc()).limit(page_size).offset((page - 1) * page_size)
    items = []
    for r in db.execute(stmt):
        items.append({
            "order_id": r.id, "order_no": r.order_no,
            "order_date": r.order_date.isoformat() if r.order_date else None,
            "occurred_date": r.order_date.isoformat() if r.order_date else None,
            "is_future": bool(r.order_date and r.order_date > today),
            "purchaser": r.purchaser, "source_type": r.source_type, "data_status": r.data_status,
            "linked_sales_order": r.linked_sales_order_no,
            "part_count": r.part_count, "pn_count": r.part_count,
            "total_qty": _f(r.total_qty), "total_quantity": _f(r.total_qty),
            "total_ex_tax": _f(r.total_ex_tax), "total_amount": _f(r.total_ex_tax),
        })
    manual_restricted = security.is_field_hidden(user_ctx, "purchase_ceiling_ex_tax")
    stats_upper = min(date_to, today) if date_to else today
    parts_map = _purchase_parts(db, [i["order_id"] for i in items],
                                date_from, stats_upper, manual_restricted)
    for it in items:
        parts = parts_map.get(it["order_id"], [])
        it["parts"] = parts
        it["pn_preview"] = list(dict.fromkeys(p["pn_std"] for p in parts if p["pn_std"]))[:3]
    return {"contract_version": 2, "total": total, "page": page, "page_size": page_size,
            "as_of": today.isoformat(),
            "effective_sort": effective_sort, "ranking_restricted": ranking_restricted,
            "cost_restricted": cost_restricted,
            "manual_reference_restricted": manual_restricted, "items": items}


def _order_health(db: Session, date_from: date | None, date_to: date | None, today: date) -> dict:
    """销售+采购订单按状态计数 + 未来日期单数（数据异常）。跨两个头表求和。
    复审 P1-5：未来单**只**计入 orders_future（数据异常），不再同时计入正常状态计数——
    否则老板会看到同一张单既属正常经营又属异常。状态计数一律加 order_date<=today 门槛。"""
    out = {"orders_active": 0, "orders_in_progress": 0, "orders_cancelled": 0, "orders_future": 0}
    for OM in (FSalesOrder, FPurchaseOrder):
        not_future = OM.order_date <= today
        stmt = select(
            func.count().filter(and_(OM.data_status == config.ACTIVE_STATUS, not_future)).label("active"),
            func.count().filter(and_(OM.data_status == "进行中", not_future)).label("in_progress"),
            func.count().filter(and_(OM.data_status.in_(_CANCELLED_STATUS), not_future)).label("cancelled"),
            func.count().filter(OM.order_date > today).label("future"),
        )
        if date_from:
            stmt = stmt.where(OM.order_date >= date_from)
        if date_to:
            stmt = stmt.where(OM.order_date <= date_to)
        r = db.execute(stmt).one()
        out["orders_active"] += r.active or 0
        out["orders_in_progress"] += r.in_progress or 0
        out["orders_cancelled"] += r.cancelled or 0
        out["orders_future"] += r.future or 0
    return out
