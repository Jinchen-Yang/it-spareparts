"""型号全景查询服务（§9）。

业务查询默认按 ACTIVE_STATUS_ONLY 过滤 data_status='已生效'。

整改 P3：对外仍接受 pn_std 入参（兼容），但内部一律先解析为 part_id 再查事实表
（pn_std 文本是导入痕迹，合并后不重写；商品身份只看 part_id）。
查询已合并型号自动重定向到目标档案，并在返回中标注 redirected_from。
"""
import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app import config, security
from app.services import inventory as inventory_service
from app.services.query_filters import active_orders, col_matches_any, keyword_groups_or_substr
from app.models.dimensions import DimCustomer, DimPart, DimSupplier
from app.models.inquiry import FPartInquiry
from app.models.inventory import Inventory, PartSubstitute
from app.models.master_data import ProductSpec
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder

_MERGE_CHAIN_LIMIT = 10
_log = logging.getLogger(__name__)


def _d(x) -> float | None:
    return float(x) if isinstance(x, Decimal) else x


def _positive_priced(rows: list, price_idx: int) -> list:
    """只保留有真实成交价（单价>0）的行——¥0（赠送/换货/录入0价）不计入均价。
    硬件成交价本就波动大，不做离群裁剪，有售价即计入。"""
    return [r for r in rows if r[price_idx] and r[price_idx] > 0]


def resolve_part(db: Session, pn_std: str) -> tuple[DimPart | None, str | None]:
    """pn_std → (part, redirected_from)。命中已合并墓碑时沿链取目标档案。

    链深溢出（>_MERGE_CHAIN_LIMIT）返回 (None, None) 而非墓碑：正常路径压缩后
    链长恒≤1，溢出意味着并发合并或数据异常，此时返墓碑会让下游 part_id 查询
    全空，AI 误报"无历史"——再次重现 §4 防的失败模式。
    """
    part = db.scalar(select(DimPart).where(DimPart.pn_std == pn_std))
    if part is None and pn_std and pn_std.strip():
        # 大小写/首尾空白容错：canonical pn_std 约定大写，用户传小写/带空格也应能定位。
        # 精确命中优先走索引（上一句），未命中才回退 lower() 匹配（罕见，不拖慢常规路径）。
        part = db.scalar(
            select(DimPart).where(func.lower(DimPart.pn_std) == pn_std.strip().lower())
            .order_by(DimPart.id))          # 大小写变体极罕见，仍确定性取行（不静默取任意行）
    if part is None:
        return None, None
    redirected_from = None
    hops = 0
    while part.status == "merged" and part.merged_into_id is not None:
        if hops >= _MERGE_CHAIN_LIMIT:
            # %r 转义控制字符，防用户控制的 pn_std 注入伪造日志行
            _log.warning(
                "merge chain limit reached resolving pn_std=%r (last=%r, depth=%d)",
                pn_std, part.pn_std, hops)
            return None, None
        redirected_from = redirected_from or part.pn_std
        next_part = db.get(DimPart, part.merged_into_id)
        if next_part is None:
            # 链中节点缺失（断 FK / 数据异常）：当作 not-found 而非 500
            _log.warning(
                "merge chain broken resolving pn_std=%r at hop %d (orphan merged_into_id=%d on %r)",
                pn_std, hops, part.merged_into_id, part.pn_std)
            return None, None
        part = next_part
        hops += 1
    return part, redirected_from


def _spec_exists(key: str, value: str | None = None,
                 numeric_min=None, numeric_max=None):
    sub = select(ProductSpec.id).where(
        ProductSpec.part_id == DimPart.id, ProductSpec.spec_key == key)
    if value is not None:
        sub = sub.where(func.upper(ProductSpec.spec_value) == value.upper())
    if numeric_min is not None:
        sub = sub.where(ProductSpec.numeric_value >= numeric_min)
    if numeric_max is not None:
        sub = sub.where(ProductSpec.numeric_value <= numeric_max)
    return sub.exists()


def search_parts(db: Session, q: str | None, page: int, page_size: int,
                 user_ctx: security.UserContext | None = None,
                 part_type: str | None = None, interface: str | None = None,
                 capacity_min: float | None = None,
                 capacity_max: float | None = None,
                 category_major: str | None = None,
                 category_minor: str | None = None) -> dict:
    """文本检索 + 结构化规格过滤（整改 P2）+ 品类过滤（宋总 2026-07-05：全品类按分类查，不只硬盘）。"""
    stmt = select(DimPart).where(DimPart.status != "merged")
    if q and q.strip():
        # 分词模糊（大小写不敏感 + 规格变体，与型号查询/库存/采购同源）：词序无关、跨字段命中即可。
        for g in keyword_groups_or_substr(q):
            stmt = stmt.where(or_(
                col_matches_any(DimPart.pn_std, g),
                col_matches_any(DimPart.description, g),
            ))
    if category_major:
        stmt = stmt.where(DimPart.category_major == category_major)
    if category_minor:
        stmt = stmt.where(DimPart.category_minor == category_minor)
    if part_type:
        stmt = stmt.where(_spec_exists("part_type", value=part_type))
    if interface:
        stmt = stmt.where(_spec_exists("interface", value=interface))
    if capacity_min is not None or capacity_max is not None:
        stmt = stmt.where(_spec_exists("capacity", numeric_min=capacity_min,
                                       numeric_max=capacity_max))
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(
        stmt.order_by(DimPart.pn_std).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()

    specs_by_part: dict[int, dict] = {}
    if rows:
        for pid, key, val in db.execute(
            select(ProductSpec.part_id, ProductSpec.spec_key, ProductSpec.spec_value)
            .where(ProductSpec.part_id.in_([p.id for p in rows]))
        ).all():
            specs_by_part.setdefault(pid, {})[key] = val
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [{
            "id": p.id,   # part_id：池成员选择等写接口需要（resolver 分支无 id，取 id 请传 browse=true）
            "pn_std": p.pn_std, "description": p.description, "brand": p.brand,
            "category_major": p.category_major, "needs_review": p.needs_review,
            "is_excluded": p.is_excluded,   # 与 resolver 分支返回形状对齐
            "specs": specs_by_part.get(p.id, {}),
        } for p in rows],
    }


def _purchases_query(part_id: int, user_ctx: security.UserContext | None = None):
    # 显示完整采购历史(含维保等),source_type 让用户辨识"哪些计入成本"
    stmt = (
        select(FPurchaseOrder.order_no, FPurchaseOrder.order_date,
                DimSupplier.name_normalized, FPurchaseLine.qty, FPurchaseLine.unit_price,
                FPurchaseOrder.source_type, FPurchaseOrder.is_tax_inclusive)
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .join(DimSupplier, FPurchaseOrder.supplier_id == DimSupplier.id, isouter=True)
        .where(FPurchaseLine.part_id == part_id)
    )
    stmt = active_orders(stmt, FPurchaseOrder)
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)
    return stmt.order_by(FPurchaseOrder.order_date.desc().nullslast())


def _sales_query(part_id: int, user_ctx: security.UserContext | None = None):
    # 带 salesperson 以便 sales 角色行级匿名化（保留行情价、抹掉同事客户名）
    stmt = (
        select(FSalesOrder.order_no, FSalesOrder.order_date,
                DimCustomer.name_normalized, FSalesLine.qty, FSalesLine.unit_price,
                FSalesOrder.salesperson)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .join(DimCustomer, FSalesOrder.customer_id == DimCustomer.id, isouter=True)
        .where(FSalesLine.part_id == part_id)
    )
    stmt = active_orders(stmt, FSalesOrder)
    if user_ctx is not None:
        stmt = security.apply_data_scope(stmt, user_ctx)
    return stmt.order_by(FSalesOrder.order_date.desc().nullslast())


def _paginate(db: Session, base_stmt, page: int, page_size: int, mapper) -> dict:
    total = db.scalar(select(func.count()).select_from(base_stmt.subquery()))
    rows = db.execute(base_stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [mapper(r) for r in rows]}


def _purchase_row(r):
    return {"order_no": r[0], "order_date": r[1], "supplier": r[2],
            "qty": _d(r[3]), "unit_price": _d(r[4]), "source_type": r[5],
            "is_tax_inclusive": r[6]}   # 单价口径：含税单→含税、不含单→不含税（前端分列，零计算）


def _sales_row(r):
    return {"order_no": r[0], "order_date": r[1], "customer": r[2],
            "qty": _d(r[3]), "unit_price": _d(r[4]), "salesperson": r[5]}


def _empty_page(page: int, page_size: int) -> dict:
    return {"total": 0, "page": page, "page_size": page_size, "items": []}


def list_purchases(db: Session, pn_std: str, page: int, page_size: int,
                   user_ctx: security.UserContext | None = None) -> dict:
    part, redirected_from = resolve_part(db, pn_std)
    if part is None:
        out = _empty_page(page, page_size)
    else:
        out = _paginate(db, _purchases_query(part.id, user_ctx), page, page_size, _purchase_row)
    out["redirected_from"] = redirected_from   # 与模块 docstring 承诺一致：所有查询都暴露重定向
    return out


def list_sales(db: Session, pn_std: str, page: int, page_size: int,
               user_ctx: security.UserContext | None = None) -> dict:
    part, redirected_from = resolve_part(db, pn_std)
    # 受限销售（2026-06-13 收紧）：逐单成交明细端点锁死——不查、返回空，销售只能用聚合。
    if part is None or security.is_scoped_sales(user_ctx):
        out = _empty_page(page, page_size)
        if security.is_scoped_sales(user_ctx):
            out["restricted"] = True   # 区分"无数据"与"按权限不可见"
    else:
        out = _paginate(db, _sales_query(part.id, user_ctx), page, page_size, _sales_row)
        out["items"] = security.anonymize_sales_rows(out["items"], user_ctx)
    out["redirected_from"] = redirected_from
    return out


def _profit_summary(db: Session, part_id: int) -> dict:
    # 加权平均采购成本（active, unit_price>0,按成本口径过滤——维保等不入)
    pc = (
        select(func.sum(FPurchaseLine.qty * FPurchaseLine.unit_price), func.sum(FPurchaseLine.qty))
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.part_id == part_id, FPurchaseLine.unit_price > 0,
               FPurchaseOrder.source_type.in_(config.COST_PURCHASE_TYPES))
    )
    pc = active_orders(pc, FPurchaseOrder)
    amt, qty = db.execute(pc).one()
    avg_cost = (amt / qty) if amt and qty else None

    # 平均销售价（含税）：只要有真实成交价（单价>0）就计入——¥0（赠送/换货/录入0价）不算；
    # 累计售出量仍按全部成交计（含 ¥0）——那是"卖了多少个"，不是"卖多少钱"。
    ss = (
        select(FSalesLine.qty, FSalesLine.unit_price)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.part_id == part_id)
    )
    ss = active_orders(ss, FSalesOrder)
    srows = db.execute(ss).all()
    sqty = sum((r[0] for r in srows if r[0] is not None), Decimal(0))
    kept = _positive_priced(srows, 1)
    kqty = sum((r[0] for r in kept), Decimal(0))
    avg_sale = (sum((r[0] * r[1] for r in kept), Decimal(0)) / kqty) if kqty else None

    # 两种成本法的单位成本与毛利率（基于 recompute 落库的逐行成本，数量加权）
    cc = (
        select(
            func.sum(FSalesLine.cost_moving_avg * FSalesLine.qty),
            func.sum(FSalesLine.cost_fifo * FSalesLine.qty),
            func.sum(FSalesLine.qty).filter(FSalesLine.cost_moving_avg.is_not(None)),
            func.sum(FSalesLine.revenue_amount).filter(FSalesLine.cost_moving_avg.is_not(None)),
        )
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.part_id == part_id, FSalesLine.counts_revenue.is_(True))
    )
    cc = active_orders(cc, FSalesOrder)
    cma, cff, cqty, crev = db.execute(cc).one()
    avg_cost_moving = (cma / cqty) if cma and cqty else None
    avg_cost_fifo = (cff / cqty) if cff and cqty else None

    def _mgn(cost):
        return round(float((crev - cost) / crev), 4) if cost is not None and crev else None

    return {
        "avg_purchase_cost": _d(avg_cost.quantize(Decimal("0.01"))) if avg_cost is not None else None,
        "avg_sale_price": _d(avg_sale.quantize(Decimal("0.01"))) if avg_sale is not None else None,
        "avg_cost_moving": _d(avg_cost_moving.quantize(Decimal("0.01"))) if avg_cost_moving is not None else None,
        "avg_cost_fifo": _d(avg_cost_fifo.quantize(Decimal("0.01"))) if avg_cost_fifo is not None else None,
        "avg_margin_moving": _mgn(cma),
        "avg_margin_fifo": _mgn(cff),
        "total_qty_sold": _d(sqty) if sqty is not None else 0,
    }


def _inventory(db: Session, part_id: int) -> list[dict]:
    # 同一 part 同仓可有多行（不同源 pn 合并后），按行展示、part 级合计=SUM
    rows = db.execute(
        select(Inventory).where(Inventory.part_id == part_id)
        .order_by(Inventory.warehouse, Inventory.pn_std)
    ).scalars().all()
    out = []
    for inv in rows:
        display = inv.manual_qty if inv.is_qty_overridden and inv.manual_qty is not None else inv.source_qty
        out.append({
            # pn_std：合并后同 part 同仓可有多行（不同源 pn），带上源 pn 才能区分这些行
            "warehouse": inv.warehouse, "pn_std": inv.pn_std, "display_qty": _d(display),
            "source_qty": _d(inv.source_qty), "manual_qty": _d(inv.manual_qty),
            "unit_cost": _d(inv.unit_cost), "inventory_value": _d(inv.inventory_value),
        })
    return out


_SUB_DEPTH_MAX = 4     # 互替闭包 BFS 深度上限（星型组一跳到齐；链式最多 4 跳，防坏数据）
_SUB_GROUP_MAX = 60    # 组大小上限，防运行时爆炸


def _substitutes(db: Session, part_id: int | None) -> list[dict]:
    """已生效(status=active)替代关系 + 互替闭包 + 库存数（宋总 2026-07-03 两条诉求）：

    - 通用号成组自动互通：互替(both)边做 BFS 闭包——给 02311JRE 加 1~5 五个通用号后，
      查其中任一个都能看到组内其余号（间接成员标注「互替（间接）·经 X」）。
      单向替代不传递（方向语义无法安全组合），仅与查询件直连时展示。
    - 每个通用号带库存数：搜一个 PN 时通用号的库存情况一并可见。
    pending/rejected 不进入推荐（审核说明 §4.6）。
    """
    if part_id is None:
        return []
    info: dict[int, dict] = {}          # other_id -> {relation, source, substitute_type, via_id}
    visited = {part_id}
    frontier = [part_id]
    for _ in range(_SUB_DEPTH_MAX):
        if not frontier or len(info) >= _SUB_GROUP_MAX:
            break
        rows = db.execute(
            select(PartSubstitute).where(
                or_(PartSubstitute.part_id_a.in_(frontier),
                    PartSubstitute.part_id_b.in_(frontier)),
                PartSubstitute.status == "active",
            )
        ).scalars().all()
        fset = set(frontier)
        nxt: list[int] = []
        for s in rows:
            for me in ({s.part_id_a, s.part_id_b} & fset):
                other = s.part_id_b if s.part_id_a == me else s.part_id_a
                direct = me == part_id
                if s.direction != "both" and not direct:
                    continue                       # 单向不传递
                if other in visited:
                    continue
                if direct:
                    is_a = s.part_id_a == part_id
                    # direction 相对规范序：a_to_b = a 的需求可用 b 满足
                    if s.direction == "both":
                        relation = "互替"
                    elif (is_a and s.direction == "a_to_b") or (not is_a and s.direction == "b_to_a"):
                        relation = "可替代本型号"
                    else:
                        relation = "本型号可替代它"
                    via_id = None
                else:
                    relation = "互替（间接）"
                    via_id = me
                info[other] = {"relation": relation, "source": s.source,
                               "substitute_type": s.substitute_type, "via_id": via_id}
                visited.add(other)
                if s.direction == "both":
                    nxt.append(other)
                if len(info) >= _SUB_GROUP_MAX:
                    break
        frontier = nxt

    if not info:
        return []
    ids = list(info)
    parts = {p.id: p for p in db.execute(
        select(DimPart).where(DimPart.id.in_(ids + [part_id]))).scalars()}
    # 库存口径切换（2026-07-04）：通用号库存 = 锚定动态（快照期初+单据流水），与库存页一致
    dyn = inventory_service.dynamic_stock_map(db, ids)
    stock = {pid: rec["dynamic_qty"] for pid, rec in dyn.items()}
    out = []
    for oid, meta in info.items():
        other = parts.get(oid)
        if not other:
            continue
        via = parts.get(meta["via_id"])
        out.append({"pn_std": other.pn_std, "description": other.description,
                    "source": meta["source"], "relation": meta["relation"],
                    "substitute_type": meta["substitute_type"],
                    "via": via.pn_std if via else None,
                    "stock_qty": _d(stock.get(oid)) or 0.0})
    # 直连在前、间接在后，组内按 PN 稳定排序
    out.sort(key=lambda x: (x["via"] is not None, x["pn_std"]))
    return out


def _stock_dynamic(db: Session, part_id: int) -> dict:
    rec = inventory_service.dynamic_stock_map(db, [part_id]).get(part_id)
    if rec is None:
        return {"dynamic_qty": 0.0, "anchor_qty": 0.0, "anchor_date": None,
                "in_qty": 0.0, "out_sales": 0.0, "out_maint": 0.0}
    return {"dynamic_qty": _d(rec["dynamic_qty"]), "anchor_qty": _d(rec["anchor_qty"]),
            "anchor_date": rec["anchor_date"].isoformat() if rec["anchor_date"] else None,
            "in_qty": _d(rec["in_qty"]), "out_sales": _d(rec["out_sales"]),
            "out_maint": _d(rec["out_maint"])}


def _sales_velocity(db: Session, part_id: int) -> dict:
    """近 90 天销售速率（二期采购场景："进 50 个合理吗"需要知道卖多快）。"""
    since = date.today() - timedelta(days=90)
    stmt = (
        select(func.sum(FSalesLine.qty).filter(FSalesOrder.order_date >= since),
               func.max(FSalesOrder.order_date))
        .select_from(FSalesLine)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.part_id == part_id)
    )
    stmt = active_orders(stmt, FSalesOrder)
    qty90, last_date = db.execute(stmt).one()
    qty90 = qty90 or Decimal(0)
    return {
        "qty_sold_90d": _d(qty90),
        "monthly_avg_90d": _d((qty90 / 3).quantize(Decimal("0.1"))),
        "last_sale_date": last_date,
    }


def _weighted_recent_sale_price(db: Session, part_id: int) -> dict:
    """成交价参考（销售出价用）：近 REF_PRICE_MAX_N 条且 REF_PRICE_DAYS 天内的成交价，
    按名次线性加权平均——rows 按时间倒序，最近一条权重最高、依次递减到 1（越近越高），
    削掉单笔异常价的方差。只取计入营收的真实成交（counts_revenue，排除换货等）。
    不给"建议售价"——加价由销售自行把握，这里只给一个稳的参考价。"""
    since = date.today() - timedelta(days=config.REF_PRICE_DAYS)
    stmt = (
        select(FSalesOrder.order_date, FSalesLine.unit_price)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.part_id == part_id,
               FSalesLine.unit_price > 0,
               FSalesLine.counts_revenue.is_(True),
               FSalesOrder.order_date >= since)
    )
    stmt = active_orders(stmt, FSalesOrder)
    # 最近 N 条；稳定排序（同日按 line id 降序）防取样不确定性
    rows = db.execute(
        stmt.order_by(FSalesOrder.order_date.desc().nullslast(), FSalesLine.id.desc())
        .limit(config.REF_PRICE_MAX_N)
    ).all()
    if not rows:   # SQL 已过滤 unit_price>0（¥0 不计入），有售价即计入
        return {"ref_sale_price": None, "ref_sale_samples": 0,
                "ref_window_days": config.REF_PRICE_DAYS}
    # 线性按名次加权：rows 已按时间倒序（最近在前），最近一条权重 = 条数 k，依次递减到 1
    k = len(rows)
    num, den = Decimal(0), 0
    for i, (_order_date, price) in enumerate(rows):
        w = k - i
        num += Decimal(w) * price
        den += w
    ref = num / Decimal(den)
    return {
        "ref_sale_price": _d(ref.quantize(Decimal("0.01"))),
        "ref_sale_samples": k,
        "ref_window_days": config.REF_PRICE_DAYS,
    }


def quick_pricing(db: Session, pn_std: str) -> dict:
    """轻量定价摘要（整机拆解/批量询价场景）：近N天采购价窗口 / 近90天均售价 / 库存合计。

    比 get_overview 轻一个量级，供 lookup_prices_bulk 每行调用。
    整改 P3：先 resolve_part 解析 part_id（含 merged 链重定向）再按 part_id 过滤；
    若直接按 pn_std 文本过滤，已合并型号的历史会从 AI 定价答案中悄悄消失。
    返回 `pn_std` / `description` / `redirected_from` 让调用方（智能体）能告诉用户
    "你查的 X 已重定向到 Y，下面价格属于 Y"——否则会用合并目标的价格冒充入参 PN。
    采购价只取计入成本的采购类型（与成本口径一致，排除维保等）。
    """
    part, redirected_from = resolve_part(db, pn_std)
    if part is None:
        return {"pn_std": pn_std, "description": None, "redirected_from": None,
                "last_purchase_price": None, "last_purchase_date": None,
                "last_purchase_type": None, "recent_purchase_days": config.RECENT_PURCHASE_DAYS,
                "recent_purchase_avg": None, "recent_purchase_min": None,
                "recent_purchase_max": None, "recent_purchase_count": 0,
                "avg_sale_price_90d": None, "stock_total": 0,
                "ref_sale_price": None, "ref_sale_samples": 0,
                "ref_window_days": config.REF_PRICE_DAYS}
    pid = part.id
    lp = (
        select(FPurchaseOrder.order_date, FPurchaseLine.unit_price, FPurchaseOrder.source_type)
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.part_id == pid,
               FPurchaseLine.unit_price.is_not(None), FPurchaseLine.unit_price > 0,
               FPurchaseOrder.source_type.in_(config.COST_PURCHASE_TYPES))
    )
    lp = active_orders(lp, FPurchaseOrder)
    last = db.execute(lp.order_by(FPurchaseOrder.order_date.desc().nullslast()).limit(1)).first()

    # 近 N 天采购价窗口（客户要"最近15天采购价"）：均/低/高/笔数
    win_since = date.today() - timedelta(days=config.RECENT_PURCHASE_DAYS)
    rp = (
        select(func.avg(FPurchaseLine.unit_price), func.min(FPurchaseLine.unit_price),
               func.max(FPurchaseLine.unit_price), func.count())
        .join(FPurchaseOrder, FPurchaseLine.order_id == FPurchaseOrder.id)
        .where(FPurchaseLine.part_id == pid,
               FPurchaseLine.unit_price.is_not(None), FPurchaseLine.unit_price > 0,
               FPurchaseOrder.source_type.in_(config.COST_PURCHASE_TYPES),
               FPurchaseOrder.order_date >= win_since)
    )
    rp = active_orders(rp, FPurchaseOrder)
    r_avg, r_min, r_max, r_cnt = db.execute(rp).one()

    since = date.today() - timedelta(days=90)
    sp = (
        select(func.sum(FSalesLine.qty * FSalesLine.unit_price), func.sum(FSalesLine.qty))
        .select_from(FSalesLine)
        .join(FSalesOrder, FSalesLine.order_id == FSalesOrder.id)
        .where(FSalesLine.part_id == pid, FSalesLine.unit_price > 0,
               FSalesOrder.order_date >= since)
    )
    sp = active_orders(sp, FSalesOrder)
    samt, sqty = db.execute(sp).one()

    # 库存口径切换（2026-07-04）：锚定动态（快照期初+单据流水），与库存页/替代料一致
    _dyn = inventory_service.dynamic_stock_map(db, [pid]).get(pid)
    stock = _dyn["dynamic_qty"] if _dyn else Decimal(0)
    return {
        # 商品身份：用 resolve_part 解析后的 *规范* PN，而非入参 pn_std——
        # 入参可能是已合并墓碑，此处覆盖让智能体把价格挂在正确的商品上
        "pn_std": part.pn_std,
        "description": part.description,
        "redirected_from": redirected_from,
        "last_purchase_price": _d(last[1]) if last else None,
        "last_purchase_date": last[0] if last else None,
        "last_purchase_type": last[2] if last else None,
        # 近 N 天采购价窗口；窗口内无采购时各值为 None，回退看 last_purchase_*
        "recent_purchase_days": config.RECENT_PURCHASE_DAYS,
        "recent_purchase_avg": _d(r_avg.quantize(Decimal("0.01"))) if r_avg else None,
        "recent_purchase_min": _d(r_min) if r_min else None,
        "recent_purchase_max": _d(r_max) if r_max else None,
        "recent_purchase_count": r_cnt or 0,
        "avg_sale_price_90d": _d((samt / sqty).quantize(Decimal("0.01"))) if samt and sqty else None,
        "stock_total": _d(stock),
        # 近期加权成交价参考（销售出价用，越近权重越高）
        **_weighted_recent_sale_price(db, pid),
    }


def _inquiry_ref(db: Session, part_id: int, pn_std: str) -> dict:
    # part_id 可空（询价不强制建档）：优先 part_id，未回填的行按 pn_std 兜底
    cond = or_(FPartInquiry.part_id == part_id,
               (FPartInquiry.part_id.is_(None)) & (FPartInquiry.pn_std == pn_std))
    row = db.execute(
        select(func.min(FPartInquiry.money), func.max(FPartInquiry.money),
                func.count()).where(cond)
    ).one()
    last = db.scalar(
        select(FPartInquiry.money).where(cond)
        .order_by(FPartInquiry.apply_time.desc().nullslast()).limit(1)
    )
    return {"min_money": _d(row[0]), "max_money": _d(row[1]),
            "last_money": _d(last), "count": row[2]}


def get_overview(db: Session, pn_std: str,
                 user_ctx: security.UserContext | None = None) -> dict | None:
    part, redirected_from = resolve_part(db, pn_std)
    if part is None:
        return None
    return {
        "part": {
            "pn_std": part.pn_std, "description": part.description, "brand": part.brand,
            "category_major": part.category_major, "category_minor": part.category_minor,
            "unit": part.unit, "needs_review": part.needs_review,
            "machine_or_part": part.machine_or_part, "locked_fields": part.locked_fields or [],
            "redirected_from": redirected_from,
        },
        "purchases_recent": _paginate(db, _purchases_query(part.id, user_ctx), 1, 20, _purchase_row)["items"],
        # 受限销售（2026-06-13 收紧）：逐单成交明细完全不可见，短路为 []（不查、不加载
        # 同事数据入内存）；销售只看聚合（平均售价 + 近期加权成交参考价 sale_price_ref）。
        # AI 助手 get_part_overview 工具走同一函数，自动覆盖。其余角色保留近 20 单（去 salesperson）。
        "sales_recent": [] if security.is_scoped_sales(user_ctx) else security.anonymize_sales_rows(
            _paginate(db, _sales_query(part.id, user_ctx), 1, 20, _sales_row)["items"], user_ctx),
        # 区分"逐单明细按权限隐藏"与"本就没有成交"：前端据此显示"按权限不可见"提示而非空表。
        # 与后端唯一门控 is_scoped_sales 对齐（不再让前端用 canCost 等无关键自行猜测）。
        "sales_recent_restricted": security.is_scoped_sales(user_ctx),
        "inventory": _inventory(db, part.id),
        "substitutes": _substitutes(db, part.id),
        # 锚定动态库存（型号级主口径）：快照期初 + 快照日后单据流水；分仓行(inventory)作参考
        "stock_dynamic": _stock_dynamic(db, part.id),
        "profit_summary": _profit_summary(db, part.id),
        "inquiry_ref": _inquiry_ref(db, part.id, part.pn_std),
        "sales_velocity": _sales_velocity(db, part.id),
        "sale_price_ref": _weighted_recent_sale_price(db, part.id),
    }
