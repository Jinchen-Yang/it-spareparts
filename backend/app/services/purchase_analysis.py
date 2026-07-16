"""采购分析面板（早会/周会用）：在采购记录之上做聚合分析。

服务对象不是"看趋势大盘"，而是"逐型号查 + 决策"——把窗口内每个型号的采购
次数 / 总量 / 逐日分布 / 价格区间·最近·趋势 / 按来源(渠道)拆分 算出来，按采购
次数排序，让采购在早会上一眼挑出"频发应急件 → 转批量谈价"。

口径：
- 与全站一致 ACTIVE_STATUS_ONLY 过滤已生效；型号展示走 dim_part canonical pn_std（part_id 主口径）。
- 含税/未税两套价都返回，前端切换显示；不含税单的"含税价"按用户口径留空（不按 13% 估）。
- 默认排除"指定采购"（大额批量补库，库里一定有），聚焦销售订单/维保需求的应急采购。
- 来源渠道取自 dim_supplier.source_channel（导入时派生，可人工修正）；缺失记"未分类"。
"""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import config, security
from app.services.query_filters import active_orders
from app.models import DimPart, DimSupplier, FPurchaseLine, FPurchaseOrder
from app.services import pool_metrics
from app.services.purchase_query import apply_keyword

_CENT = Decimal("0.01")


def _line_prices(unit_price, is_inc):
    """单行的 (未税价, 含税价)——零计算，只镜像 Excel 原值（甲方口径：系统不算税率）。

    含税单→只有含税价、未税价留空；不含税单→只有未税价、含税价留空；
    未标注(None，改造前老数据)→按含税列放（沿用旧「单价(含税)」默认）。
    另一侧一律留 None → 前端显示 "--"，绝不用税率反推另一侧。
    """
    if unit_price is None:
        return None, None
    if is_inc is False:                          # 不含税单：单价即未税价
        return unit_price, None
    return None, unit_price                       # 含税单 / 未标注：单价即含税价


def _window_lines(db: Session, user_ctx, since: date, until: date,
                  exclude_designated: bool, q: str | None,
                  supplier: str | None, purchaser: str | None,
                  part_id: int | None = None):
    """拉窗口内采购明细行（含订单头/供应商维度），供聚合与下钻共用。"""
    stmt = (
        select(
            FPurchaseLine.id.label("line_id"),
            FPurchaseLine.part_id,
            DimPart.pn_std, DimPart.needs_review,
            FPurchaseLine.description, FPurchaseLine.brand,
            FPurchaseOrder.id.label("order_id"),
            FPurchaseOrder.order_no, FPurchaseOrder.order_date,
            FPurchaseOrder.purchaser, FPurchaseOrder.source_type,
            FPurchaseOrder.is_tax_inclusive, FPurchaseOrder.tax_rate,
            FPurchaseOrder.amount_ex_tax, FPurchaseOrder.amount_inc_tax,  # 订单级真实含税/不含税总额（Excel原值，供KPI双总额，零计算）
            DimSupplier.name_normalized.label("supplier"),
            DimSupplier.source_channel,
            FPurchaseLine.qty, FPurchaseLine.unit_price, FPurchaseLine.line_amount,
        )
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .join(DimPart, FPurchaseLine.part_id == DimPart.id)
        .join(DimSupplier, FPurchaseOrder.supplier_id == DimSupplier.id, isouter=True)
        .where(FPurchaseOrder.order_date >= since, FPurchaseOrder.order_date <= until)
    )
    stmt = active_orders(stmt, FPurchaseOrder)
    if exclude_designated and config.ANALYSIS_EXCLUDE_SOURCE_TYPES:
        stmt = stmt.where(or_(
            FPurchaseOrder.source_type.is_(None),
            FPurchaseOrder.source_type.notin_(config.ANALYSIS_EXCLUDE_SOURCE_TYPES),
        ))
    if part_id is not None:
        stmt = stmt.where(FPurchaseLine.part_id == part_id)
    stmt = apply_keyword(stmt, q)                 # part_id 主口径关键词召回（与 recent 同一真值源）
    if supplier and supplier.strip():
        stmt = stmt.where(DimSupplier.name_normalized.ilike(f"%{supplier.strip()}%"))
    if purchaser and purchaser.strip():
        stmt = stmt.where(FPurchaseOrder.purchaser.ilike(f"%{purchaser.strip()}%"))
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)
    # 确定性排序：同日/同单平局时 最近价/趋势/谈价 不再随 DB 行序漂移（复用本仓"稳定排序"教训）。
    # 并发 LIMIT 防爆：超大窗口不把全表拉进内存（超限由 analysis 标 over_limit）。
    stmt = stmt.order_by(FPurchaseOrder.order_date, FPurchaseOrder.id, FPurchaseLine.id) \
               .limit(config.ANALYSIS_MAX_LINES)
    return db.execute(stmt).mappings().all()


def _f(x):
    return float(x) if x is not None else None


def _trend(orders_sorted) -> str:
    """按时间排序的（每单代表价 未税）列表 → 价格趋势。"""
    prices = [p for _, p in orders_sorted if p is not None]
    if len(prices) < 2:
        return "new" if prices else "flat"
    last, prev = prices[-1], prices[-2]
    if last > prev:
        return "up"
    if last < prev:
        return "down"
    return "flat"


def analysis(db: Session, user_ctx: security.UserContext | None = None, *,
             days: int | None = None, exclude_designated: bool = True,
             freq_threshold: int | None = None, q: str | None = None,
             supplier: str | None = None, purchaser: str | None = None,
             top: int | None = None, as_of: date | None = None) -> dict:
    days = max(1, min(int(days or config.ANALYSIS_DEFAULT_DAYS), config.ANALYSIS_MAX_DAYS))
    ft = int(freq_threshold or config.ANALYSIS_FREQ_THRESHOLD)
    top = int(top or config.ANALYSIS_TOP_N)
    until = as_of or date.today()
    since = until - timedelta(days=days - 1)
    want_daily = days <= config.ANALYSIS_DAILY_MAX_DAYS

    rows = _window_lines(db, user_ctx, since, until, exclude_designated,
                         q, supplier, purchaser)

    # 逐型号聚合
    parts: dict[int, dict] = {}
    all_orders: set = set()
    orders_by_source: dict[str, set] = defaultdict(set)
    comp: dict[str, dict] = defaultdict(lambda: {"amount": Decimal(0), "orders": set(), "lines": 0})
    # 订单级真实含税/不含税总额（每单只计一次；含税总额=Σamount_inc_tax、不含税=Σamount_ex_tax，零计算）
    order_amt: dict = {}   # oid -> (ex, inc, channel)

    for r in rows:
        pid = r["part_id"]
        p = parts.get(pid)
        if p is None:
            p = parts[pid] = {
                "part_id": pid, "pn_std": r["pn_std"], "needs_review": r["needs_review"],
                "description": r["description"], "brand": r["brand"],
                "orders": set(), "total_qty": Decimal(0),
                "ex_list": [], "inc_list": [],          # (price, qty) 用于 min/max/加权均
                "order_px": {},                         # order_id -> (order_date, ex 价均, inc 价或None, 累计)
                "daily": [Decimal(0)] * days if want_daily else None,
                "source_types": set(),
                "channels": defaultdict(lambda: {"orders": set(), "qty": Decimal(0),
                                                 "amount": Decimal(0), "ex_last": None,
                                                 "inc_last": None, "last_date": None}),
            }
        qty = r["qty"] or Decimal(0)
        ex, inc = _line_prices(r["unit_price"], r["is_tax_inclusive"])
        amt = r["line_amount"] or Decimal(0)
        oid = r["order_id"]
        ch = r["source_channel"] or config.SOURCE_CHANNEL_UNKNOWN

        p["orders"].add(oid)
        p["total_qty"] += qty
        if ex is not None:
            p["ex_list"].append((ex, qty))
        if inc is not None:
            p["inc_list"].append((inc, qty))
        if r["source_type"]:
            p["source_types"].add(r["source_type"])
        # 每单代表价（同单多行取首个非空 ex）——趋势按"采购发生"粒度
        op = p["order_px"].get(oid)
        if op is None:
            p["order_px"][oid] = (r["order_date"], ex, inc)
        if want_daily and r["order_date"] is not None:
            idx = (r["order_date"] - since).days
            if 0 <= idx < days:
                p["daily"][idx] += qty
        # 渠道拆分
        c = p["channels"][ch]
        c["orders"].add(oid)
        c["qty"] += qty
        c["amount"] += amt
        if c["last_date"] is None or (r["order_date"] and r["order_date"] >= c["last_date"]):
            c["last_date"], c["ex_last"], c["inc_last"] = r["order_date"], ex, inc

        all_orders.add(oid)
        if r["source_type"]:
            orders_by_source[r["source_type"]].add(oid)
        co = comp[ch]
        co["amount"] += amt
        co["orders"].add(oid)
        co["lines"] += 1
        if oid not in order_amt:            # 每单只登记一次订单级真实金额
            order_amt[oid] = (r["amount_ex_tax"], r["amount_inc_tax"], ch)

    def _wavg(pairs):
        tot_q = sum((q for _, q in pairs), Decimal(0))
        if tot_q == 0:
            vals = [v for v, _ in pairs]
            return (sum(vals) / len(vals)) if vals else None
        return sum((v * q for v, q in pairs), Decimal(0)) / tot_q

    out_rows = []
    for p in parts.values():
        ex_vals = [v for v, _ in p["ex_list"]]
        inc_vals = [v for v, _ in p["inc_list"]]
        orders_sorted = [v for _, v in sorted(
            p["order_px"].items(), key=lambda kv: (kv[1][0] or date.min, kv[0]))]
        ex_sorted = [(d, ex) for d, ex, _ in orders_sorted]
        buy_times = len(p["orders"])
        is_freq = buy_times >= ft
        trend = _trend(ex_sorted)
        if is_freq:
            advice = "批量补库"
        elif trend == "up":
            advice = "谈价"
        elif buy_times == 1:
            advice = "偶发"
        else:
            advice = "普通"
        channels = sorted(
            ({"channel": ch, "times": len(c["orders"]), "qty": _f(c["qty"]),
              "amount": _f(c["amount"].quantize(_CENT)),
              "price_ex_last": _f(c["ex_last"].quantize(_CENT)) if c["ex_last"] is not None else None,
              "price_inc_last": _f(c["inc_last"].quantize(_CENT)) if c["inc_last"] is not None else None}
             for ch, c in p["channels"].items()),
            key=lambda x: (-x["times"], -(x["qty"] or 0)))
        out_rows.append({
            "part_id": p["part_id"], "pn_std": p["pn_std"], "needs_review": p["needs_review"],
            "description": p["description"], "brand": p["brand"],
            "buy_times": buy_times, "total_qty": _f(p["total_qty"]),
            "daily": [_f(x) for x in p["daily"]] if p["daily"] is not None else None,
            "price_ex_min": _f(min(ex_vals).quantize(_CENT)) if ex_vals else None,
            "price_ex_max": _f(max(ex_vals).quantize(_CENT)) if ex_vals else None,
            "price_ex_avg": _f(_wavg(p["ex_list"]).quantize(_CENT)) if ex_vals else None,
            "price_ex_last": _f(ex_sorted[-1][1].quantize(_CENT)) if ex_sorted and ex_sorted[-1][1] is not None else None,
            "price_inc_min": _f(min(inc_vals).quantize(_CENT)) if inc_vals else None,
            "price_inc_max": _f(max(inc_vals).quantize(_CENT)) if inc_vals else None,
            "price_inc_avg": _f(_wavg(p["inc_list"]).quantize(_CENT)) if inc_vals else None,
            "price_inc_last": _f(orders_sorted[-1][2].quantize(_CENT)) if orders_sorted and orders_sorted[-1][2] is not None else None,
            "price_trend": trend, "source_types": sorted(p["source_types"]),
            "is_frequent": is_freq, "advice": advice, "channels": channels,
        })

    pool_map = pool_metrics.active_pool_map(db, [row["part_id"] for row in out_rows])
    for row in out_rows:
        identity = pool_map.get(row["part_id"])
        row["pool_group_id"] = identity["group_id"] if identity else None
        row["pool_name"] = identity["pool_name"] if identity else None
    out_rows.sort(key=lambda x: (-x["buy_times"], -(x["total_qty"] or 0)))
    total_amount = sum((co["amount"] for co in comp.values()), Decimal(0))
    # 订单级真实双总额（零计算）：含税=Σamount_inc_tax、不含税=Σamount_ex_tax；再按渠道拆一份。
    # 某侧缺失时用另一侧的真实值兜底（老单常只有不含税金额、无采购金额；不含税单两额本就相等）——
    # 这不是税率换算，只是"没有这侧真实值就用那侧真实值"，保证两侧覆盖同一批订单、含税≥不含税。
    comp_amt: dict[str, dict] = defaultdict(lambda: {"ex": Decimal(0), "inc": Decimal(0)})
    total_inc = total_ex = Decimal(0)
    for a_ex, a_inc, a_ch in order_amt.values():
        ex = a_ex if a_ex is not None else a_inc
        inc = a_inc if a_inc is not None else a_ex
        if ex is not None:
            total_ex += ex
            comp_amt[a_ch]["ex"] += ex
        if inc is not None:
            total_inc += inc
            comp_amt[a_ch]["inc"] += inc
    kpi = {
        "total_amount": _f(total_amount.quantize(_CENT)),
        "total_amount_inc": _f(total_inc.quantize(_CENT)),
        "total_amount_ex": _f(total_ex.quantize(_CENT)),
        "order_count": len(all_orders),
        "order_count_by_source": {k: len(v) for k, v in orders_by_source.items()},
        "part_count": len(parts),
        "frequent_count": sum(1 for r in out_rows if r["is_frequent"]),
        "shown": min(len(out_rows), top),
        "truncated": max(0, len(out_rows) - top),
        "over_limit": len(rows) >= config.ANALYSIS_MAX_LINES,
    }
    source_composition = sorted(
        ({"channel": ch, "amount": _f(co["amount"].quantize(_CENT)),
          "amount_inc": _f(comp_amt[ch]["inc"].quantize(_CENT)),
          "amount_ex": _f(comp_amt[ch]["ex"].quantize(_CENT)),
          "order_count": len(co["orders"]), "line_count": co["lines"]}
         for ch, co in comp.items()),
        key=lambda x: -(x["amount"] or 0))
    return {
        "window": {"days": days, "since": since.isoformat(), "until": until.isoformat(),
                   "freq_threshold": ft, "exclude_designated": exclude_designated,
                   "daily": want_daily},
        "kpi": kpi, "source_composition": source_composition,
        "rows": out_rows[:top],
    }


def part_purchases(db: Session, user_ctx: security.UserContext | None = None, *,
                   part_id: int, days: int | None = None,
                   exclude_designated: bool = True, as_of: date | None = None) -> dict:
    """单型号逐笔采购（面板行展开 → 横向比价：谁采的/哪家/含税未税/多少钱）。

    exclude_designated 默认 True，与主排行一致——否则会出现"行显示 N 次、展开却更多笔"。
    """
    days = max(1, min(int(days or config.ANALYSIS_DEFAULT_DAYS), config.ANALYSIS_MAX_DAYS))
    until = as_of or date.today()
    since = until - timedelta(days=days - 1)
    rows = _window_lines(db, user_ctx, since, until, exclude_designated=exclude_designated,
                         q=None, supplier=None, purchaser=None, part_id=part_id)
    items = []
    for r in rows:
        ex, inc = _line_prices(r["unit_price"], r["is_tax_inclusive"])
        items.append({
            "order_date": r["order_date"].isoformat() if r["order_date"] else None,
            "order_no": r["order_no"], "purchaser": r["purchaser"],
            "supplier": r["supplier"], "source_channel": r["source_channel"] or config.SOURCE_CHANNEL_UNKNOWN,
            "source_type": r["source_type"], "qty": _f(r["qty"]),
            "is_tax_inclusive": r["is_tax_inclusive"],
            "unit_price": _f(r["unit_price"]),
            "price_ex": _f(ex.quantize(_CENT)) if ex is not None else None,
            "price_inc": _f(inc.quantize(_CENT)) if inc is not None else None,
        })
    items.sort(key=lambda x: (x["order_date"] or "", x["order_no"] or ""), reverse=True)
    return {"part_id": part_id, "days": days, "items": items}


_CANCEL_GRANULARITY = {"month", "quarter", "year"}


def _is_cancelled_status(s: str) -> bool:
    """取消/作废统一判定（宋总诉求：统计采购未成功的单）。"""
    return ("取消" in s) or ("作废" in s)


def _period_label(dt, gran: str) -> str:
    if gran == "year":
        return f"{dt.year}"
    if gran == "quarter":
        return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"
    return f"{dt.year}-{dt.month:02d}"


def cancellation_stats(db: Session, user_ctx: security.UserContext | None = None, *,
                       granularity: str = "month", days: int | None = None) -> dict:
    """按期间(月/季/年)×状态 聚合采购单数与金额，统计取消/作废（采购未成功）的单。

    与全站「业务查询过滤已生效」相反——这里**刻意不过滤状态**，取消/作废单本身就是统计对象。
    取消单只在此类统计里被数，不进入任何成本/库存/利润计算（那些口径仍只认已生效）。
    """
    gran = granularity if granularity in _CANCEL_GRANULARITY else "month"
    period = func.date_trunc(gran, FPurchaseOrder.order_date)
    amount = func.coalesce(FPurchaseOrder.amount_inc_tax, FPurchaseOrder.amount_ex_tax)
    stmt = (
        select(period.label("period"), FPurchaseOrder.data_status.label("status"),
               func.count().label("cnt"),
               func.coalesce(func.sum(amount), 0).label("amt"))
        .where(FPurchaseOrder.order_date.is_not(None))
    )
    if days:
        stmt = stmt.where(
            FPurchaseOrder.order_date >= date.today() - timedelta(days=int(days)))
    stmt = stmt.group_by(period, FPurchaseOrder.data_status).order_by(period)

    periods: dict[str, dict] = {}
    statuses: set[str] = set()
    for period_dt, status, cnt, amt in db.execute(stmt).all():
        if period_dt is None:
            continue
        lbl = _period_label(period_dt, gran)
        st = status or "(空)"
        statuses.add(st)
        p = periods.setdefault(lbl, {
            "period": lbl, "total": 0, "cancelled": 0,
            "cancelled_amount": Decimal(0), "by_status": {}})
        p["by_status"][st] = {"count": cnt, "amount": _f((amt or Decimal(0)).quantize(_CENT))}
        p["total"] += cnt
        if _is_cancelled_status(st):
            p["cancelled"] += cnt
            p["cancelled_amount"] += (amt or Decimal(0))

    rows = []
    for p in periods.values():
        p["cancel_rate"] = round(100.0 * p["cancelled"] / p["total"], 2) if p["total"] else 0.0
        p["cancelled_amount"] = _f(p["cancelled_amount"].quantize(_CENT))
        rows.append(p)
    rows.sort(key=lambda r: r["period"], reverse=True)   # 最近期间在前

    total = sum(r["total"] for r in rows)
    cancelled = sum(r["cancelled"] for r in rows)
    cancelled_amt = sum(r["cancelled_amount"] for r in rows)
    return {
        "granularity": gran,
        "statuses": sorted(statuses),
        "rows": rows,
        "summary": {
            "total": total, "cancelled": cancelled,
            "cancel_rate": round(100.0 * cancelled / total, 2) if total else 0.0,
            "cancelled_amount": round(cancelled_amt, 2),
        },
    }
