"""互通 PN 池的**只读**池化分析（老板看板池清单/池详情）。

2026-07-13（互通PN池价格分析 Slice 1）起池是人工维护的唯一真值：
- 自动重算 rebuild() 已删除——替代关系（part_substitute）变化**不会**再改池；
  池、成员、约束价的唯一写入路径是 services/pool_catalog。
- 本模块只保留 list_pools / analyze 两个只读入口（旧"潜在节省"语义，
  Slice 4 经营看板改版时迁移到价格纪律摘要）。
"""
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app import config, security
from app.models.dimensions import DimCustomer, DimPart
from app.models.inventory import PartPool, PartPoolMember, PartSubstitute
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.services.pricing import (
    purchase_ex_tax_expr as _purchase_ex_tax_expr,
    purchase_ex_unit as _purchase_ex_unit,
    sale_ex_unit as _sale_ex_unit,
)
from app.services.query_filters import active_orders

_MIN_RELIABLE_SAMPLES = 2   # 加权均价样本≥2 才作降本标杆（避免一次异常低价，甲方修正②）


def _r(x, n=2):
    return round(float(x), n) if x is not None else None


def _purchase_member_stats(db, part_ids, date_from, upper):
    """每成员采购统计（未税）+ 供应稳定性（采购次数/供应商数/最近采购日；库存 8 月前不作条件）。"""
    ex = _purchase_ex_unit()
    stmt = (
        select(
            FPurchaseLine.part_id,
            (func.sum(_purchase_ex_tax_expr()) / func.nullif(func.sum(FPurchaseLine.qty), 0)).label("wavg"),
            func.percentile_cont(0.5).within_group(ex).label("median"),
            func.min(ex).label("pmin"), func.max(ex).label("pmax"),
            func.count().label("samples"),
            func.count(func.distinct(FPurchaseOrder.id)).label("orders"),
            func.count(func.distinct(FPurchaseOrder.supplier_id)).label("suppliers"),
            func.max(FPurchaseOrder.order_date).label("last_date"),
        )
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.part_id.in_(part_ids),
               FPurchaseLine.unit_price.is_not(None), FPurchaseLine.unit_price > 0,
               FPurchaseLine.qty.is_not(None), FPurchaseLine.qty > 0,
               FPurchaseOrder.source_type.in_(config.COST_PURCHASE_TYPES))
    )
    stmt = active_orders(stmt, FPurchaseOrder)
    if date_from:
        stmt = stmt.where(FPurchaseOrder.order_date >= date_from)
    stmt = stmt.where(FPurchaseOrder.order_date <= upper).group_by(FPurchaseLine.part_id)
    out = {}
    for r in db.execute(stmt):
        out[r.part_id] = {
            "wavg": _r(r.wavg), "median": _r(r.median), "min": _r(r.pmin), "max": _r(r.pmax),
            "samples": r.samples, "last_date": r.last_date.isoformat() if r.last_date else None,
            "supply": {"purchase_orders": r.orders, "suppliers": r.suppliers,
                       "last_purchase_date": r.last_date.isoformat() if r.last_date else None},
        }
    return out


def _sale_member_stats(db, part_ids, date_from, upper):
    """每成员销售统计（未税）+ 销量/营收/毛利。
    复审 P1-6：价格统计（均价/中位/最低/最高/样本/去重订单）**只算计营收且单价>0 的行**
    （¥0 赠送/换货、不计营收行不进价格分布）；销量/营收仍按计营收行全量（含 ¥0 成交量）。"""
    ex = _sale_ex_unit()
    sl = FSalesLine
    counts = sl.counts_revenue.is_(True)
    priced = and_(counts, sl.unit_price.is_not(None), sl.unit_price > 0)   # 价格分布口径
    costed = and_(counts, sl.cost_moving_avg.is_not(None))
    stmt = (
        select(
            sl.part_id,
            (func.sum(sl.revenue_amount).filter(priced)
             / func.nullif(func.sum(sl.qty).filter(priced), 0)).label("wavg"),
            func.percentile_cont(0.5).within_group(ex).filter(priced).label("median"),
            func.min(ex).filter(priced).label("smin"), func.max(ex).filter(priced).label("smax"),
            func.count().filter(priced).label("samples"),
            func.count(func.distinct(FSalesOrder.id)).filter(priced).label("orders"),
            func.sum(sl.qty).filter(counts).label("qty_sold"),
            func.sum(sl.revenue_amount).filter(counts).label("revenue"),
            func.sum(sl.revenue_amount).filter(costed).label("rev_costed"),
            func.sum(sl.gross_profit).filter(costed).label("gross_profit"),
        )
        .join(FSalesOrder, sl.order_id == FSalesOrder.id)
        .where(sl.part_id.in_(part_ids))
    )
    stmt = active_orders(stmt, FSalesOrder)
    if date_from:
        stmt = stmt.where(FSalesOrder.order_date >= date_from)
    stmt = stmt.where(FSalesOrder.order_date <= upper).group_by(sl.part_id)
    out = {}
    for r in db.execute(stmt):
        rc, gp = r.rev_costed, r.gross_profit
        out[r.part_id] = {
            "wavg": _r(r.wavg), "median": _r(r.median), "min": _r(r.smin), "max": _r(r.smax),
            "samples": r.samples, "orders": r.orders, "qty_sold": _r(r.qty_sold, 3), "revenue": _r(r.revenue),
            "gross_profit": _r(gp),
            "gross_margin": round(float(gp) / float(rc), 4) if gp is not None and rc else None,
        }
    return out


def _benchmark(members, key):
    """池内降本标杆：**去重订单数≥2** 的成员里最低加权均价（甲方② + 复审 P1-6：
    "可靠样本"必须是不同订单，同一订单两行不算供应稳定）。无可靠成员则取有均价里最低并标 low_confidence。
    返回 (基准值, part_id, low_confidence)。"""
    def _orders(m):
        s = m[key] or {}
        # 采购统计的去重订单在 supply.purchase_orders；销售统计在 orders
        return (s.get("orders")
                or (s.get("supply") or {}).get("purchase_orders")
                or 0)
    reliable = [m for m in members if m[key] and m[key]["wavg"] is not None and _orders(m) >= _MIN_RELIABLE_SAMPLES]
    pool_has = [m for m in members if m[key] and m[key]["wavg"] is not None]
    src = reliable or pool_has
    if not src:
        return None, None, False
    best = min(src, key=lambda m: m[key]["wavg"])
    return best[key]["wavg"], best["part_id"], not bool(reliable)


def analyze(db: Session, group_id: int, date_from: date | None = None, date_to: date | None = None,
            as_of: date | None = None, user_ctx: security.UserContext | None = None) -> dict | None:
    """单个通用号池的降本分析（只读）。甲方修正版：双端溢价、供应稳定性非库存、
    节省分理论上限/可执行机会、客户跨品牌集中度（老板可见）。不输出自动替换指令。"""
    pool = db.get(PartPool, group_id)
    if pool is None or pool.status != "active":
        # 归档池不是当前经营池（复审阻塞 2）：其成员可能已进新有效池，再分析会把同一
        # PN 双份计入节省/需求。归档档案的查看走管理接口 /api/pools?status=archived。
        return None
    today = as_of or date.today()
    upper = min(date_to, today) if date_to else today
    part_ids = list(db.execute(
        select(PartPoolMember.part_id).where(PartPoolMember.group_id == group_id)).scalars())
    parts = {p.id: p for p in db.execute(select(DimPart).where(DimPart.id.in_(part_ids))).scalars()}

    # 供应能力是当前能力，不是页面经营窗口：采购标杆价与供应证据固定看
    # [today-365天, today]。特别是 date_to 为历史日期时，不能把 today 的 floor
    # 与历史 upper 拼成空窗口。销量/营收(demand)仍严格按页面选择范围。
    supply_floor = today - timedelta(days=config.POOL_SUPPLY_RECENT_DAYS)
    pstats = _purchase_member_stats(db, part_ids, supply_floor, today)
    sstats = _sale_member_stats(db, part_ids, date_from, upper)

    members = []
    for pid in part_ids:
        dp = parts.get(pid)
        members.append({
            "part_id": pid, "pn_std": dp.pn_std if dp else None,
            "description": dp.description if dp else None, "brand": dp.brand if dp else None,
            "purchase_price": pstats.get(pid), "sale_price": sstats.get(pid),
        })

    cost_bench, cost_bench_pid, cost_lowconf = _benchmark(members, "purchase_price")
    sale_bench, sale_bench_pid, _ = _benchmark(members, "sale_price")
    warn = float(config.POOL_PREMIUM_WARN_PCT)

    # 每成员溢价（双端）+ 供应
    for m in members:
        pw = m["purchase_price"]["wavg"] if m["purchase_price"] else None
        sw = m["sale_price"]["wavg"] if m["sale_price"] else None
        m["purchase_premium_pct"] = round((pw - cost_bench) / cost_bench, 4) if pw is not None and cost_bench else None
        m["sale_premium_pct"] = round((sw - sale_bench) / sale_bench, 4) if sw is not None and sale_bench else None
        m["brand_premium_purchase"] = bool(m["purchase_premium_pct"] is not None and m["purchase_premium_pct"] >= warn)
        m["brand_premium_sale"] = bool(m["sale_premium_pct"] is not None and m["sale_premium_pct"] >= warn)

    # 两级节省（甲方⑤+复审 P0-3）：
    # - 理论上限 theoretical_max：假设全部可替换。
    # - 供应层面潜在上限 supply_available_upper：标杆供应可得的子集——**仍非"可执行"**。
    # 系统对兼容性/客户指定品牌/合同**无任何证据**，因此每条替换的核实状态一律"待核实"，
    # 绝不把未核实的金额算成"可执行"、绝不显示绿色"是"（复审：约1074万虚假可执行，误导极大）。
    # 供应可得（收紧，复审二轮 P1-5）：标杆型号须 ≥N 张去重采购单 + ≥1 家供应商 +
    # 最近采购在窗口内。仍只代表"标杆供应稳定"，不代表兼容/可替换（一律待核实）。
    bench_supply_ok = False
    if cost_bench_pid is not None and pstats.get(cost_bench_pid):
        s = pstats[cost_bench_pid]["supply"]
        last = s.get("last_purchase_date")
        recent = bool(last and (today - date.fromisoformat(last)).days <= config.POOL_SUPPLY_RECENT_DAYS)
        bench_supply_ok = bool(
            (s.get("purchase_orders") or 0) >= config.POOL_SUPPLY_MIN_ORDERS
            and (s.get("suppliers") or 0) >= config.POOL_SUPPLY_MIN_SUPPLIERS
            and recent)
    theoretical = supply_upper = 0.0
    opps = []
    for m in members:
        pw = m["purchase_price"]["wavg"] if m["purchase_price"] else None
        qty = m["sale_price"]["qty_sold"] if m["sale_price"] else None
        if pw is None or cost_bench is None or qty is None or pw <= cost_bench or qty <= 0:
            continue
        unit_saving = pw - cost_bench
        t = round(unit_saving * qty, 2)
        theoretical += t
        block = None
        if not bench_supply_ok:
            block = "标杆型号供应不稳（去重采购单<2、无供应商或近一年无采购）"
        elif (m["purchase_price"] or {}).get("samples", 0) < 1:
            block = "样本不足"
        supply_available = block is None      # 仅代表"标杆供应可得"，不代表可替换
        if supply_available:
            supply_upper += t
        opps.append({
            "from_part_id": m["part_id"], "from_pn": m["pn_std"], "from_brand": m["brand"],
            "to_part_id": cost_bench_pid, "to_pn": parts[cost_bench_pid].pn_std if cost_bench_pid in parts else None,
            "unit_saving": _r(unit_saving), "qty_sold": qty, "theoretical_saving": t,
            "supply_available": supply_available, "block_reason": block,
            # 兼容性/客户指定品牌/合同系统无证据 → 一律待核实，永不判定为"可执行"
            "verification_status": "待核实",
        })
    opps.sort(key=lambda o: o["theoretical_saving"], reverse=True)

    demand_qty = sum((m["sale_price"]["qty_sold"] or 0) for m in members if m["sale_price"])
    demand_rev = sum((m["sale_price"]["revenue"] or 0) for m in members if m["sale_price"])

    result = {
        "group_id": group_id, "member_count": pool.member_count,
        "needs_calibration": pool.needs_calibration, "oversized": pool.oversized,
        "window": {"date_from": date_from.isoformat() if date_from else None,
                   "date_to": date_to.isoformat() if date_to else None, "as_of": today.isoformat()},
        "supply_window": {"date_from": supply_floor.isoformat(),
                          "date_to": today.isoformat(), "as_of": today.isoformat()},
        "demand": {"total_qty": _r(demand_qty, 3), "total_revenue_ex_tax": _r(demand_rev),
                   "note": "跨品牌总需求只证明公司卖过这些品牌，不等于同一客户愿互换；见 customer_cross_brand"},
        "benchmark": {"cost_part_id": cost_bench_pid, "cost_ex_tax": _r(cost_bench),
                      "low_confidence": cost_lowconf, "supply_ok": bench_supply_ok,
                      "sale_part_id": sale_bench_pid, "sale_ex_tax": _r(sale_bench)},
        "members": members,
        "savings": {"theoretical_max": round(theoretical, 2),
                    "supply_available_upper": round(supply_upper, 2),
                    "executable": None,   # 无兼容性/指定品牌/合同证据 → 无可执行金额
                    "label": "潜在降本机会（只读）——所有替换均「待核实」兼容性/客户指定品牌/合同，"
                             "当前无可执行金额；金额仅为供应层面潜在上限",
                    "opportunities": opps},
        "customer_cross_brand": _customer_cross_brand(db, part_ids, date_from, upper, user_ctx),
    }
    return result


def _customer_cross_brand(db, part_ids, date_from, upper, user_ctx, top=10):
    """池内客户的跨品牌购买（甲方④：跨品牌销量≠客户认功能，要看同一客户是否买过≥2品牌+集中度）。
    复审 P1-6：不能只靠"端点是 boss 页"——自定义把看板页开给无客户可见性的角色时也要挡。
    受限销售、或无客户信息可见性(data_customer=False → customer_info 被脱敏)一律 restricted。"""
    if user_ctx is not None:
        if security.is_scoped_sales(user_ctx) or security.is_field_hidden(user_ctx, "customer"):
            return {"restricted": True, "customers": []}
    sl, so = FSalesLine, FSalesOrder
    # 品牌取自型号(DimPart.brand)：池成员本就是不同品牌的等价型号，客户跨品牌=买了不同成员
    stmt = (
        select(DimCustomer.name_normalized.label("cust"), DimPart.brand,
               func.sum(sl.qty).label("qty"))
        .join(so, sl.order_id == so.id)
        .join(DimPart, sl.part_id == DimPart.id)
        .join(DimCustomer, so.customer_id == DimCustomer.id, isouter=True)
        .where(sl.part_id.in_(part_ids), sl.counts_revenue.is_(True))
    )
    stmt = active_orders(stmt, so)
    if date_from:
        stmt = stmt.where(so.order_date >= date_from)
    stmt = stmt.where(so.order_date <= upper).group_by(DimCustomer.name_normalized, DimPart.brand)
    by_cust: dict[str, dict] = defaultdict(dict)
    for r in db.execute(stmt):
        if r.cust is None:
            continue
        by_cust[r.cust][r.brand or "(未标品牌)"] = float(r.qty or 0)
    rows = []
    for cust, brands in by_cust.items():
        total = sum(brands.values()) or 1
        top_share = max(brands.values()) / total
        rows.append({"customer": cust, "brand_count": len(brands),
                     "brands": {b: round(q, 3) for b, q in brands.items()},
                     "concentration": round(top_share, 4)})
    # 优先展示买过≥2品牌的客户（可引导替换的信号），再按需求量
    rows.sort(key=lambda x: (x["brand_count"] >= 2, sum(x["brands"].values())), reverse=True)
    return {"restricted": False, "multi_brand_customers": sum(1 for x in rows if x["brand_count"] >= 2),
            "customers": rows[:top]}


def _pool_list_item(p: PartPool, d: dict) -> dict:
    return {
        "group_id": p.group_id, "member_count": p.member_count,
        "needs_calibration": p.needs_calibration, "oversized": p.oversized,
        "demand_qty": d["demand"]["total_qty"], "demand_revenue_ex_tax": d["demand"]["total_revenue_ex_tax"],
        "theoretical_saving": d["savings"]["theoretical_max"],
        "supply_available_upper": d["savings"]["supply_available_upper"],
    }


def list_pools(db: Session, date_from: date | None = None, date_to: date | None = None,
               as_of: date | None = None, page: int = 1, page_size: int = 20,
               sort: str = "member_count", user_ctx: security.UserContext | None = None) -> dict:
    """池清单。两种排序口径：
    - sort="member_count"（默认）：按成员数降序，**先分页再逐池分析**（避免 N+1）。
    - sort="savings"（复审二轮 P1-4）：**全局**按理论节省额排名——先分析全部池再排序分页，
      否则"成员少但节省高"的池会永远藏在后页。池数量有限（生产 ~40），超 POOL_RANK_ANALYZE_CAP
      时退回成员数排序并置 ranking_capped=True（当前不触发）。
    只统计 status='active'（复审阻塞 2）：归档池成员可再入新有效池，混入清单/总数/
    排名会把同一 PN 双份计入；归档档案查询走管理接口。"""
    total = db.execute(select(func.count()).select_from(PartPool)
                       .where(PartPool.status == "active")).scalar() or 0
    ranking_restricted = sort == "savings" and security.is_field_hidden(user_ctx, "theoretical_saving")
    if ranking_restricted:
        # 不仅不返回金额，连“按节省额”的执行路径也不能运行；否则行序仍是
        # 隐藏金额的侧信道。下面只按成员数/稳定 group_id 取页。
        sort = "member_count"
    ranking_capped = False

    if sort == "savings":
        if total <= config.POOL_RANK_ANALYZE_CAP:
            all_pools = db.execute(
                select(PartPool).where(PartPool.status == "active")
                .order_by(PartPool.group_id.asc())).scalars().all()
            scored = [(p, analyze(db, p.group_id, date_from, date_to, as_of)) for p in all_pools]
            # 全局按节省额降序，再按 group_id 稳定破并列
            scored.sort(key=lambda pd: (-(pd[1]["savings"]["theoretical_max"] or 0), pd[0].group_id))
            page_slice = scored[(page - 1) * page_size: page * page_size]
            items = [_pool_list_item(p, d) for p, d in page_slice]
            return {"total": total, "page": page, "page_size": page_size,
                    "sort": "savings", "effective_sort": "savings",
                    "ranking_restricted": False, "ranking_capped": False, "items": items}
        ranking_capped = True   # 池数超上限，退回成员数排序（数据规模保护）

    page_pools = db.execute(
        select(PartPool).where(PartPool.status == "active")
        .order_by(PartPool.member_count.desc(), PartPool.group_id.asc())
        .limit(page_size).offset((page - 1) * page_size)
    ).scalars().all()
    # 成员数口径保持数据库排序；绝不能在当前页再按隐藏 theoretical_saving 排序。
    items = [_pool_list_item(p, analyze(db, p.group_id, date_from, date_to, as_of))
             for p in page_pools]
    return {"total": total, "page": page, "page_size": page_size,
            "sort": "member_count", "effective_sort": "member_count",
            "ranking_restricted": ranking_restricted, "ranking_capped": ranking_capped, "items": items}
