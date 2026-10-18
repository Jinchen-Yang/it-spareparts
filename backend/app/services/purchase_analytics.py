"""采购分析（客户需求 Round1，D-39：`docs/decisions/0017-采购分析筛选与三口径.md`）。

日期区间 × 采购类型/采购员/销售/客户/分类 多选筛选，三口径金额（含税 / 不含税 / 实付）
一次全部返回 + 分组下钻。只读，不写任何业务状态；不处理异常大额单（id=149683 等原样保留）。

口径要点（D-39 及 2026-10-17 补充决策）：
- 采购类型 = ``f_purchase_order.source_type_raw`` 原值，不做映射；空 → ``(空)``。
- 分类 = ``dim_part.category_major``（挂在明细行上），空 → 「未分类」；按分类筛选/分组时
  把订单拆成明细行，只汇总匹配行；行实付 = ``line_amount`` 之和。
- 税率规则（Q6）：单头标了税率（≠0）就用该税率；没标或 0 按 13%；「是否含税」标记不
  覆盖已标税率，只决定哪一侧是原始录入值（含税单的含税额为原值，不含税 = 含税 ÷ (1+税率)，
  非含税单反之）。实付 = ``amount_inc_tax`` 原值（不按分类筛选时）。
- 名义侧（金臣 2026-10-18 裁定，D-39 补充）：未标税率（NULL/0）时只有**按 13% 推算出来
  的那一侧**是名义值，录入侧是真实值——不含税单 → ``nominal_side="inc"``（含税额为名义
  推算），含税单 → ``nominal_side="ex"``（不含税额为名义推算），``is_tax_inclusive`` 非
  true（False/NULL）一律按不含税单处理；税率已标 → ``nominal_side=None``，两侧都真实。
  ``nominal_inc_share`` = 未标税率不含税单的推算含税额 ÷ 含税合计；
  ``nominal_ex_share`` = 未标税率含税单的推算不含税额 ÷ 不含税合计（分母 0/NULL → null）。
- **单头批次行**：所有行级统计（行数、分类分组、行级三口径、订单明细行）只计
  ``f_purchase_line.import_batch_id = f_purchase_order.import_batch_id`` 的行——库里 11 张
  已生效单残留旧批次明细行（D-39 补充决策 2026-10-17），全行求和会比单头多算。
- 关联销售单按 ``order_no`` 先去重（同一 order_no 多张销售单取 id 最小一条，库中无唯一
  约束）再 LEFT JOIN，防一对多扩行；无关联销售单的采购单（如批量采购）在销售/客户维度
  归「（无关联销售单）」组，不丢。
- 关联去重的执行形态（性能 2026-10-17，生产 EXPLAIN：全表 DISTINCT ON 外排 5.2MB
  ≈ 0.9–1.0s，orders 全年 2.7s）：排序键全部在采购单侧，所以订单列表先在采购单上
  筛选/分页，页内（≤ page_size 个 order_no）再补关联销售员/客户；只有销售人员/客户
  筛选或按销售/客户分组下钻必须在分页前关联时，DISTINCT ON 的输入先被「当前窗口
  采购单引用的 order_no」半连接限定（options 同法）——同一去重规则（id 最小一条），
  只对被引用子集排序；也不用 LATERAL 逐单探测（复审实测其在 po×明细行连接后按
  行数放大，客户下钻退化）。

索引（不建新表/物化视图）：``ix_po_status_date(data_status, order_date)`` 驱动状态+日期
窗口；``ix_pl_order(order_id, id)`` 驱动按单行聚合；``ix_po_linked`` / ``ix_so_order_no``
驱动关联去重；``dim_part`` 主键驱动分类。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import String, and_, case, func, or_, select
from sqlalchemy.orm import Session

from app import config, security, tax_policy
from app.business_time import business_today
from app.models import DimCustomer, DimPart, FPurchaseLine, FPurchaseOrder, FSalesOrder

DEFAULT_TAX_RATE = Decimal("0.13")
NO_LINKED_LABEL = "（无关联销售单）"
UNCATEGORIZED_LABEL = "未分类"
EMPTY_LABEL = "(空)"

GROUP_BY_KEYS = ("source_type", "purchaser", "salesperson", "customer", "category", "month", "day")
SORT_KEYS = ("order_date", "amount_inc", "amount_ex", "amount_paid")
MAX_PAGE_SIZE = 200


class FilterValidationError(ValueError):
    """筛选参数非法（date_from > date_to 等）→ API 层转 422。"""


# ---------------------------------------------------------------- 税率三口径表达式

def _effective_rate(tax_rate) -> Decimal:
    """Q6 税率的 Python 侧：标了（≠0）用标的，否则 13%（与 _rate_expr 同口径）。"""
    if tax_rate is None:
        return DEFAULT_TAX_RATE
    r = Decimal(str(tax_rate))
    return r if r != 0 else DEFAULT_TAX_RATE


def _rate_expr(po=FPurchaseOrder):
    """Q6 税率：标了（≠0）用标的，否则 13%。"""
    return func.coalesce(func.nullif(po.tax_rate, 0), DEFAULT_TAX_RATE)


def _nominal_side_expr(po=FPurchaseOrder):
    """名义侧 SQL 表达式（金臣 2026-10-18 裁定）——**行级口径的结构性规则**：
    未标税率（NULL/0）时推算侧才是名义值——含税单（is_tax_inclusive=true）
    推算的是不含税侧 → "ex"；其余（false/NULL，按不含税单处理）推算的是
    含税侧 → "inc"；税率已标 → NULL。行金额的归属侧只由单据标记决定，
    不受单头金额列缺口影响，行级聚合一律用本表达式。"""
    unmarked = func.coalesce(po.tax_rate, 0) == 0
    return case(
        (and_(unmarked, po.is_tax_inclusive.is_(True)), "ex"),
        (unmarked, "inc"),
    )


def _order_nominal_side_expr(po=FPurchaseOrder):
    """单头口径的名义侧：在结构性规则（_nominal_side_expr）之上叠加历史缺口
    方向——权威侧金额列为空、回退另一录入列时（_order_amount_exprs 的
    coalesce 回退），实际被推算的是**权威侧**：不含税单缺不含税额、只录入
    含税额 → 含税额是录入真值、不含税侧才是推算值 → "ex"（镜像：含税单
    缺含税额 → "inc"）。两侧金额列皆空 → 无从谈名义 → NULL。"""
    unmarked = func.coalesce(po.tax_rate, 0) == 0
    return case(
        (and_(unmarked, po.is_tax_inclusive.is_(True)),
         case((po.amount_inc_tax.isnot(None), "ex"),
              (po.amount_ex_tax.isnot(None), "inc"))),
        (unmarked,
         case((po.amount_ex_tax.isnot(None), "inc"),
              (po.amount_inc_tax.isnot(None), "ex"))),
    )


def _order_amount_exprs(po=FPurchaseOrder):
    """单头三口径 SQL 表达式（每单先 round 到分再聚合，与 cancellation_stats 同款）。

    返回 (amount_ex, amount_inc, amount_paid, nominal_side)。原始侧按 is_tax_inclusive
    选定，另一侧按有效税率换算；权威侧为空时回退另一显式列（历史缺口，不混加口径）。
    nominal_side = 未标税率时**实际被推算**的那一侧（"inc"/"ex"），已标税率 → NULL
    （语义见 _order_nominal_side_expr——历史缺口单的推算方向会翻转；行级口径
    用结构性规则 _nominal_side_expr）。
    """
    rate = _rate_expr(po)
    factor = Decimal(1) + rate
    src_ex = func.round(po.amount_ex_tax, 2)
    src_inc = func.round(po.amount_inc_tax, 2)
    inc_from_ex = func.round(src_ex * factor, 2)
    ex_from_inc = func.round(src_inc / factor, 2)
    amount_ex = case(
        (po.is_tax_inclusive.is_(True), func.coalesce(ex_from_inc, src_ex)),
        else_=func.coalesce(src_ex, ex_from_inc),
    )
    amount_inc = case(
        (po.is_tax_inclusive.is_(True), func.coalesce(src_inc, inc_from_ex)),
        else_=func.coalesce(inc_from_ex, src_inc),
    )
    amount_paid = po.amount_inc_tax  # 实付 = 「采购金额」原值，不换算
    return amount_ex, amount_inc, amount_paid, _order_nominal_side_expr(po)


def _line_amount_exprs(po=FPurchaseOrder, pl=FPurchaseLine):
    """明细行级含税/不含税表达式：行金额跟随单头原始口径侧，另一侧按单头有效税率换算。"""
    rate = _rate_expr(po)
    factor = Decimal(1) + rate
    amt = pl.line_amount
    line_inc = case(
        (po.is_tax_inclusive.is_(True), func.round(amt, 2)),
        else_=func.round(amt * factor, 2),
    )
    line_ex = case(
        (po.is_tax_inclusive.is_(True), func.round(amt / factor, 2)),
        else_=func.round(amt, 2),
    )
    return line_ex, line_inc


def order_amounts_reference(amount_ex, amount_inc, tax_rate, is_inc) \
        -> tuple[Decimal | None, Decimal | None]:
    """Q6 规则的 Python 参照实现，返回 ``(不含税, 含税)``。

    与上方 SQL CASE 逐行等价（测试对拍）；未标税率（None/0）时与既有
    ``purchase_analysis._order_amounts`` 的固定 13% 口径一致。
    """
    rate = _effective_rate(tax_rate)
    factor = Decimal(1) + rate
    ex = tax_policy.round_money(amount_ex) if amount_ex is not None else None
    inc = tax_policy.round_money(amount_inc) if amount_inc is not None else None
    inc_from_ex = tax_policy.round_money(ex * factor) if ex is not None else None
    ex_from_inc = tax_policy.round_money(inc / factor) if inc is not None else None
    if is_inc is True:
        return (ex_from_inc if inc is not None else ex), (inc if inc is not None else inc_from_ex)
    return (ex if ex is not None else ex_from_inc), (inc_from_ex if ex is not None else inc)


def nominal_side_reference(tax_rate, is_inc) -> str | None:
    """名义侧的 Python 参照实现（与 _nominal_side_expr 逐行对拍）——行级口径
    的结构性规则。

    未标税率（None/0）：含税单（is_inc=True）→ "ex"（不含税是推算侧），
    其余（False/None，按不含税单处理）→ "inc"；已标税率 → None。
    """
    if tax_rate is None or Decimal(str(tax_rate)) == 0:
        return "ex" if is_inc is True else "inc"
    return None


def order_nominal_side_reference(tax_rate, is_inc, amount_ex, amount_inc) -> str | None:
    """单头口径名义侧的 Python 参照实现（与 _order_nominal_side_expr 逐行对拍）。

    结构性规则（nominal_side_reference）之上叠加历史缺口方向：权威侧金额列
    为空、只录入另一侧时，实际被推算的是权威侧（如不含税单缺不含税额 →
    "ex"）；两侧金额列皆空 → None。
    """
    if tax_rate is not None and Decimal(str(tax_rate)) != 0:
        return None
    if amount_ex is None and amount_inc is None:
        return None
    if is_inc is True:
        return "ex" if amount_inc is not None else "inc"
    return "inc" if amount_ex is not None else "ex"


def line_amounts_reference(line_amount, tax_rate, is_inc) -> tuple[Decimal | None, Decimal | None]:
    """行级口径的 Python 参照实现，返回 ``(不含税, 含税)``（与 _line_amount_exprs 对拍）。"""
    if line_amount is None:
        return None, None
    line_amount = Decimal(str(line_amount))
    rate = _effective_rate(tax_rate)
    factor = Decimal(1) + rate
    if is_inc is True:
        return tax_policy.round_money(line_amount / factor), tax_policy.round_money(line_amount)
    return tax_policy.round_money(line_amount), tax_policy.round_money(line_amount * factor)


# ---------------------------------------------------------------- 共用查询构件

def _sales_link_bounded(po_where):
    """关联销售单去重子查询（有界 DISTINCT ON）：``f_sales_order.order_no`` 无唯一
    约束，同一 order_no 多张销售单取 id 最小的一条（确定性），防一对多扩行；无匹配 →
    LEFT JOIN 补 NULL。去重规则与旧版全表 ``DISTINCT ON (order_no) … ORDER BY
    order_no, id`` 逐字同语义：IN 按 order_no **值**过滤、不筛行，被引用 order_no
    的去重输出仍是全部同号行中 id 最小的（等价性测试对拍）。输入限定为 po 侧条件
    （已生效+日期+类型/采购员筛选）命中的采购单引用的 order_no——不对全部 11.6 万
    销售单外排去重（生产 EXPLAIN ≈0.9–1.0s/语句），也不用 LATERAL 逐单探测
    （复审实测其在 po×明细行连接后按行数放大，客户下钻退化）。po_where 少含几条
    po 侧条件也不影响正确性：IN 集合只需覆盖「语句实际会连的 order_no」。
    关联是身份查找，不过滤销售单状态。"""
    po, so = FPurchaseOrder, FSalesOrder
    nos = select(po.linked_sales_order_no).where(
        *po_where, po.linked_sales_order_no.is_not(None))
    return (
        select(
            so.order_no.label("order_no"),
            so.salesperson.label("salesperson"),
            so.customer_id.label("customer_id"),
        )
        .where(so.order_no.in_(nos))
        .distinct(so.order_no)
        .order_by(so.order_no, so.id)
    ).subquery()


def _link_join(stmt, po, link):
    """把有界去重关联（+客户名）挂到语句上；link 为 None 时原样返回（无销售/客户
    参与，主语句不碰 f_sales_order，见 orders/summary 的 needs_link 判定）。"""
    if link is None:
        return stmt
    return (stmt.outerjoin(link, po.linked_sales_order_no == link.c.order_no)
            .outerjoin(DimCustomer, link.c.customer_id == DimCustomer.id))


def _page_sales_link(db: Session, order_nos: list[str]) -> dict[str, tuple]:
    """页内关联销售单去重（orders 无销售/客户筛选时二段补值用）：对 ≤ page_size 个
    order_no 做与 _sales_link_bounded 同一规则的 DISTINCT ON（id 最小一条），IN
    命中 ``ix_so_order_no``，毫秒级。返回 order_no → (salesperson, customer_id)。"""
    if not order_nos:
        return {}
    so = FSalesOrder
    rows = db.execute(
        select(so.order_no, so.salesperson, so.customer_id)
        .where(so.order_no.in_(order_nos))
        .distinct(so.order_no)
        .order_by(so.order_no, so.id)
    ).all()
    return {r.order_no: (r.salesperson, r.customer_id) for r in rows}


def _batch_line_join(po=FPurchaseOrder, pl=FPurchaseLine):
    """明细行关联条件：**只计单头所属批次的行**（D-39 补充决策 2026-10-17）。"""
    return and_(pl.order_id == po.id, pl.import_batch_id == po.import_batch_id)


def _norm_categories(categories: list[str] | None) -> list[str] | None:
    """分类参数入口归一：剔除空串/纯空白元素（前端空选集常见形态），全空视为未筛选。
    保证 line_mode 的判定与 _category_match 的过滤真值对齐——空串不会把口径静默
    翻到行级（对抗复审 2026-10-17 确认项）。"""
    if not categories:
        return None
    return [c for c in categories if c and c.strip()] or None


def _category_match(categories: list[str] | None):
    """分类筛选条件；与 _category_dim 同一 btrim 真值（Excel 导入首尾空格常见，
    ' 电器件 ' 必须既进「电器件」组也能被 category=['电器件'] 筛到）。
    「未分类」哨兵匹配 NULL/空串/纯空白，其余按归一后值精确 IN。"""
    if not categories:
        return None
    norm = [c.strip() for c in categories if c and c.strip()]
    conds = []
    if norm:
        conds.append(func.btrim(DimPart.category_major).in_(norm))
    if any(c == UNCATEGORIZED_LABEL for c in categories):
        conds.append(func.nullif(func.btrim(DimPart.category_major), "").is_(None))
    if not conds:
        return None
    return or_(*conds)


def _category_dim(dp=DimPart):
    return func.coalesce(func.nullif(func.btrim(dp.category_major), ""), UNCATEGORIZED_LABEL)


def _group_dim(group_by: str, po=FPurchaseOrder, link=None, dp=DimPart):
    """分组维度表达式（一律文本，供分组与 group_key 下钻过滤共用同一真值）。"""
    if group_by == "source_type":
        return func.coalesce(func.nullif(po.source_type_raw, ""), EMPTY_LABEL)
    if group_by == "purchaser":
        return func.coalesce(func.nullif(po.purchaser, ""), EMPTY_LABEL)
    if group_by == "salesperson":
        return case(
            (link.c.order_no.is_(None), NO_LINKED_LABEL),
            (link.c.salesperson.is_(None), EMPTY_LABEL),
            else_=link.c.salesperson,
        )
    if group_by == "customer":
        return case(
            (link.c.order_no.is_(None), NO_LINKED_LABEL),
            else_=func.coalesce(func.cast(link.c.customer_id, String), EMPTY_LABEL),
        )
    if group_by == "category":
        return _category_dim(dp)
    if group_by == "month":
        return func.coalesce(func.to_char(po.order_date, "YYYY-MM"), EMPTY_LABEL)
    if group_by == "day":
        return func.coalesce(func.to_char(po.order_date, "YYYY-MM-DD"), EMPTY_LABEL)
    raise FilterValidationError(f"不支持的分组维度：{group_by}")


def _validate_common(date_from, date_to):
    if date_from and date_to and date_from > date_to:
        raise FilterValidationError("date_from 不能晚于 date_to（闭区间）")


def _base_where(po, link, *, date_from, date_to, source_type, purchaser,
                salesperson, customer_id):
    """已生效 + 全部筛选条件（多选一律精确匹配；日期闭区间，不传 = 全部）。"""
    w = [po.data_status == config.ACTIVE_STATUS]
    if date_from:
        w.append(po.order_date >= date_from)
    if date_to:
        w.append(po.order_date <= date_to)
    if source_type:
        w.append(po.source_type_raw.in_(source_type))
    if purchaser:
        w.append(po.purchaser.in_(purchaser))
    if salesperson:
        w.append(link.c.salesperson.in_(salesperson))
    if customer_id:
        w.append(link.c.customer_id.in_(customer_id))
    return w


def _f(x) -> float | None:
    return float(x) if x is not None else None


def _round2(x) -> Decimal | None:
    return tax_policy.round_money(x) if x is not None else None


def _share(nominal, total) -> float | None:
    """名义占比 = 名义侧金额 ÷ 同侧合计；分母 0/NULL（无数据或合计为零）→ None。"""
    if not total or nominal is None:
        return None
    return round(float(Decimal(nominal) / Decimal(total)), 4)


def _filters_echo(*, date_from, date_to, source_type, purchaser, salesperson,
                  customer_id, category, group_by=None, group_key=None) -> dict:
    return {
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "source_type": source_type or [],
        "purchaser": purchaser or [],
        "salesperson": salesperson or [],
        "customer_id": customer_id or [],
        "category": category or [],
        "group_by": group_by,
        "group_key": group_key,
    }


def _is_int_str(s) -> bool:
    try:
        int(s)
        return True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- options

def options(db: Session, user_ctx=None, *, customer_q: str = "",
            customer_page: int = 1, customer_page_size: int = 200,
            selected_customer_ids: list[int] | None = None) -> dict:
    """已生效采购筛选候选：类型/采购员/关联销售人员/客户/分类。

    客户用 ID 去重、名称/id 排序；分页搜索由 API 边界校验。
    """
    po = FPurchaseOrder
    active = po.data_status == config.ACTIVE_STATUS
    source_types = [r for r in db.execute(
        select(func.distinct(po.source_type_raw)).where(
            active, po.source_type_raw.is_not(None), po.source_type_raw != ""
        ).order_by(po.source_type_raw)).scalars()]
    purchasers = [r for r in db.execute(
        select(func.distinct(po.purchaser)).where(
            active, po.purchaser.is_not(None), po.purchaser != ""
        ).order_by(po.purchaser)).scalars()]
    # 销售人员取值：DISTINCT ON 先按 order_no 去重（同一去重规则，id 最小一条），
    # 输入限定为「已生效采购单引用的 order_no」半连接命中的销售单（性能 2026-10-17：
    # 旧版全表 DISTINCT ON 对 11.6 万销售单外排 5.2MB ≈ 1.0s；结果集合逐字不变）
    linked_nos = select(po.linked_sales_order_no).where(
        active, po.linked_sales_order_no.is_not(None))
    link = (
        select(FSalesOrder.order_no.label("order_no"),
               FSalesOrder.salesperson.label("salesperson"),
               FSalesOrder.customer_id.label("customer_id"))
        .where(FSalesOrder.order_no.in_(linked_nos))
        .distinct(FSalesOrder.order_no)
        .order_by(FSalesOrder.order_no, FSalesOrder.id)
    ).subquery()
    salespersons = [r for r in db.execute(
        select(func.distinct(link.c.salesperson))
        .where(link.c.salesperson.is_not(None), link.c.salesperson != "")
        .order_by(link.c.salesperson)).scalars()]
    # 客户直接查身份/名称，不执行全历史金额汇总。客户不可见时不查、不泄露数量。
    customers = []
    selected_customers = []
    customers_has_more = False
    if user_ctx is None or not security.is_field_hidden(user_ctx, "customer_id"):
        customer_query = (select(DimCustomer.id, DimCustomer.name_raw)
                          .join(link, link.c.customer_id == DimCustomer.id)
                          .distinct())
        if selected_customer_ids:
            selected_rows = db.execute(customer_query.where(
                DimCustomer.id.in_(selected_customer_ids))
                .order_by(DimCustomer.name_raw, DimCustomer.id)).all()
            selected_customers = [{"customer_id": r.id, "customer": r.name_raw}
                                  for r in selected_rows]
        if customer_q.strip():
            customer_query = customer_query.where(
                DimCustomer.name_raw.contains(customer_q.strip(), autoescape=True))
        rows = db.execute(customer_query.order_by(DimCustomer.name_raw, DimCustomer.id)
                          .offset((customer_page - 1) * customer_page_size)
                          .limit(customer_page_size + 1)).all()
        customers_has_more = len(rows) > customer_page_size
        customers = [{"customer_id": r.id, "customer": r.name_raw}
                     for r in rows[:customer_page_size]]
    pl = FPurchaseLine
    dp = DimPart
    cat_rows = db.execute(
        select(func.distinct(_category_dim(dp)))
        .select_from(po)
        .join(pl, _batch_line_join(po, pl))
        .join(dp, pl.part_id == dp.id)
        .where(active)
    ).scalars().all()
    categories = sorted(c for c in cat_rows if c != UNCATEGORIZED_LABEL)
    if UNCATEGORIZED_LABEL in cat_rows:
        categories.append(UNCATEGORIZED_LABEL)
    data = {
        "selected_customers": selected_customers,
        "customers": customers,
        "customers_has_more": customers_has_more,
        "source_types": source_types,
        "purchasers": purchasers,
        "salespersons": salespersons,
        "categories": categories,
        "uncategorized_label": UNCATEGORIZED_LABEL,
        "no_linked_label": NO_LINKED_LABEL,
    }
    if user_ctx is not None:
        data = security.apply_data_scope(data, user_ctx)
    return data


# ---------------------------------------------------------------- summary

def summary(db: Session, user_ctx=None, *, date_from: date | None = None,
            date_to: date | None = None, source_type: list[str] | None = None,
            purchaser: list[str] | None = None, salesperson: list[str] | None = None,
            customer_id: list[int] | None = None, category: list[str] | None = None,
            group_by: str = "source_type") -> dict:
    """KPI + 分组汇总。分类筛选（或按分类分组）→ 行级口径（拆行只计匹配行）；
    其余 → 单头口径（实付取单头「采购金额」原值）。三套金额一次全部返回。

    行级口径下主查询逐行展开，行数直接 ``count(pl.id)``；单头口径下若把行 LEFT JOIN
    进主查询，单头金额会被行数放大，因此行数始终用独立语句按同一筛选/分组统计。
    """
    _validate_common(date_from, date_to)
    if group_by not in GROUP_BY_KEYS:
        raise FilterValidationError(f"不支持的分组维度：{group_by}")
    po, pl, dp = FPurchaseOrder, FPurchaseLine, DimPart
    # 关联销售单按「语句是否引用」分别装配：筛选条件（销售人员/客户）引用 → KPI/
    # 行数/分组全部语句都要连；仅分组维是销售/客户时只有分组语句（含分组行数）需要
    # 连，KPI 与总行数语句不引用关联列——该外连接是死重（去重后按 order_no 1:1
    # 不扩行不筛行），不连结果相同（等价性测试对拍），且免算关联（性能 2026-10-17）。
    # 去重子查询的 IN 输入只用 po 侧条件（销售/客户条件挂在 link 上，构造时不可用）
    po_where = _base_where(po, None, date_from=date_from, date_to=date_to,
                           source_type=source_type, purchaser=purchaser,
                           salesperson=None, customer_id=None)
    link = _sales_link_bounded(po_where) if (salesperson or customer_id) else None
    group_link = link if link is not None else (
        _sales_link_bounded(po_where) if group_by in ("salesperson", "customer") else None)
    category = _norm_categories(category)
    line_mode = bool(category) or group_by == "category"
    cat = _category_match(category)
    where = _base_where(po, link, date_from=date_from, date_to=date_to,
                        source_type=source_type, purchaser=purchaser,
                        salesperson=salesperson, customer_id=customer_id)
    if line_mode and cat is not None:
        where.append(cat)

    if line_mode:
        ex_e, inc_e = _line_amount_exprs(po, pl)
        paid_e = pl.line_amount
        side = _nominal_side_expr(po)
        measures = [
            func.count(func.distinct(po.id)).label("orders"),
            func.count(pl.id).label("lines"),
            func.sum(inc_e).label("amount_inc"),
            func.sum(ex_e).label("amount_ex"),
            func.sum(paid_e).label("amount_paid"),
            func.sum(case((side == "inc", inc_e), else_=0)).label("nominal_inc"),
            func.sum(case((side == "ex", ex_e), else_=0)).label("nominal_ex"),
        ]
    else:
        ex_e, inc_e, paid_e, side = _order_amount_exprs(po)
        measures = [
            func.count(func.distinct(po.id)).label("orders"),
            func.sum(inc_e).label("amount_inc"),
            func.sum(ex_e).label("amount_ex"),
            func.sum(paid_e).label("amount_paid"),
            func.sum(case((side == "inc", inc_e), else_=0)).label("nominal_inc"),
            func.sum(case((side == "ex", ex_e), else_=0)).label("nominal_ex"),
        ]

    def _apply_from(stmt, link_variant):
        """统一 FROM 链：行级口径逐行展开（单头批次行 + 分类维），否则纯单头。"""
        if line_mode:
            stmt = (stmt.select_from(po)
                    .join(pl, _batch_line_join(po, pl))
                    .join(dp, pl.part_id == dp.id))
        else:
            stmt = stmt.select_from(po)
        return _link_join(stmt, po, link_variant)

    def _lines_stmt(dim, link_variant):
        """行数独立统计（仅单头口径调用；此时无分类筛选，无需 join dim_part）。"""
        stmt = _link_join(
            select(
                *([dim.label("key")] if dim is not None else []),
                func.count(pl.id).label("lines"),
            ).select_from(po).join(pl, _batch_line_join(po, pl)),
            po, link_variant).where(*where)
        if dim is not None:
            stmt = stmt.group_by(dim)
        return stmt

    kpi_row = db.execute(_apply_from(select(*measures), link).where(*where)).one()
    kpi_lines = kpi_row.lines if line_mode else (db.execute(_lines_stmt(None, link)).scalar() or 0)

    dim = _group_dim(group_by, po, group_link, dp)
    # 分组排序的结构性保护（pool.py 复审三轮先例）：默认按实付合计降序，但金额组
    # 被遮蔽时组序本身就是隐藏金额排名的侧信道——apply_field_visibility 只把
    # amount_* 置空、不改组序。此时改按订单数降序 + 键升序（计数不脱敏，可用）。
    groups_ranking_restricted = security.is_field_hidden(user_ctx, "amount_paid")
    if groups_ranking_restricted:
        grp_order = (func.count(func.distinct(po.id)).desc(), dim.asc().nullslast())
    else:
        grp_order = (func.sum(paid_e).desc().nullslast(), dim)
    grp_rows = db.execute(
        _apply_from(select(dim.label("key"), *measures), group_link).where(*where)
        .group_by(dim).order_by(*grp_order)
    ).all()
    lines_by_key: dict[str, int] = {}
    if not line_mode:
        lines_by_key = {r.key: r.lines for r in db.execute(_lines_stmt(dim, group_link)).all()}

    customer_names = {}
    if group_by == "customer":
        ids = [int(r.key) for r in grp_rows
               if r.key not in (NO_LINKED_LABEL, EMPTY_LABEL) and _is_int_str(r.key)]
        if ids:
            customer_names = dict(db.execute(
                select(DimCustomer.id, DimCustomer.name_normalized)
                .where(DimCustomer.id.in_(ids))).all())

    groups = []
    for r in grp_rows:
        item = {
            "key": r.key,
            "label": r.key,
            "orders": r.orders,
            "lines": r.lines if line_mode else lines_by_key.get(r.key, 0),
            "amount_inc": _f(_round2(r.amount_inc)),
            "amount_ex": _f(_round2(r.amount_ex)),
            "amount_paid": _f(_round2(r.amount_paid)),
            # 名义占比与 KPI 同分子分母口径（金臣 2026-10-18 裁定）
            "nominal_inc_share": _share(r.nominal_inc, r.amount_inc),
            "nominal_ex_share": _share(r.nominal_ex, r.amount_ex),
        }
        if group_by == "customer":
            # 客户名只放登记过的脱敏键（customer），label 不承载客户名
            item["label"] = NO_LINKED_LABEL if r.key == NO_LINKED_LABEL else None
            item["customer"] = customer_names.get(int(r.key)) if _is_int_str(r.key) else None
        groups.append(item)

    return {
        "basis_mode": "line" if line_mode else "order",
        "filters_echo": _filters_echo(
            date_from=date_from, date_to=date_to, source_type=source_type,
            purchaser=purchaser, salesperson=salesperson, customer_id=customer_id,
            category=category, group_by=group_by),
        "as_of": business_today().isoformat(),
        "kpi": {
            "orders": kpi_row.orders,
            "lines": kpi_lines,
            "amount_inc": _f(_round2(kpi_row.amount_inc)),
            "amount_ex": _f(_round2(kpi_row.amount_ex)),
            "amount_paid": _f(_round2(kpi_row.amount_paid)),
            "nominal_inc_share": _share(kpi_row.nominal_inc, kpi_row.amount_inc),
            "nominal_ex_share": _share(kpi_row.nominal_ex, kpi_row.amount_ex),
        },
        "groups": groups,
        "groups_ranking_restricted": groups_ranking_restricted,
    }


# ---------------------------------------------------------------- orders 下钻

def orders(db: Session, user_ctx=None, *, date_from: date | None = None,
           date_to: date | None = None, source_type: list[str] | None = None,
           purchaser: list[str] | None = None, salesperson: list[str] | None = None,
           customer_id: list[int] | None = None, category: list[str] | None = None,
           group_by: str | None = None, group_key: str | None = None,
           sort: str = "order_date", order: str = "desc",
           page: int = 1, page_size: int = 50) -> dict:
    """分组下钻订单列表（分页 + 排序，同额/同日按 id 倒序稳定翻页）。

    group_by + group_key：把上一层分组值折成对应维度精确过滤（哨兵值
    「（无关联销售单）」「(空)」「未分类」与 month/day 文本键同一真值）；
    category 分组折成分类筛选（保持行级口径与上层分组一致）。

    关联销售单（性能 2026-10-17）：排序键全在采购单侧，无销售/客户参与时主语句
    （count + 分页）不连 f_sales_order，页内再补关联值（_page_sales_link）；
    销售/客户筛选或按销售/客户下钻必须在分页前过滤，关联去重的输入被当前窗口
    采购单引用的 order_no 半连接限定（_sales_link_bounded，与全表 DISTINCT ON
    同一去重规则）。
    两种形态结果逐字一致（等价性测试对拍）。
    """
    _validate_common(date_from, date_to)
    if sort not in SORT_KEYS:
        raise FilterValidationError(f"不支持的排序键：{sort}")
    if order not in ("asc", "desc"):
        raise FilterValidationError(f"不支持的排序方向：{order}")
    if group_key is not None and not group_by:
        raise FilterValidationError("group_key 需与 group_by 同传")
    if group_by is not None and group_by not in GROUP_BY_KEYS:
        raise FilterValidationError(f"不支持的分组维度：{group_by}")
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    # 结构性排序保护（pool.py 复审三轮先例）：被脱敏的金额不能当排序键运行——
    # 行序本身是隐藏金额排名的侧信道，apply_field_visibility 只置空值不改行序。
    # amount_* 同属 purchase_cost 组，一查即全遮。
    requested_sort = sort
    ranking_restricted = sort != "order_date" and security.is_field_hidden(
        user_ctx, "amount_paid")
    if ranking_restricted:
        sort = "order_date"

    po, pl, dp = FPurchaseOrder, FPurchaseLine, DimPart
    category = _norm_categories(category)
    needs_link = bool(salesperson or customer_id) or (
        bool(group_key) and group_by in ("salesperson", "customer"))
    # 去重子查询的 IN 输入只用 po 侧条件（销售/客户条件挂在 link 上，构造时不可用；
    # 分类是行级条件也不在此——IN 集合是超集即可，不影响正确性）
    po_where = _base_where(po, None, date_from=date_from, date_to=date_to,
                           source_type=source_type, purchaser=purchaser,
                           salesperson=None, customer_id=None)
    link = _sales_link_bounded(po_where) if needs_link else None
    # 分类分组（带或不带 group_key）一律行级口径，与 summary 同真值（对抗复审
    # 2026-10-17 确认项：无 group_key 时单头口径会与同参数 KPI 对不上账）
    line_mode = bool(category) or group_by == "category"
    cat = _category_match(category)
    where = _base_where(po, link, date_from=date_from, date_to=date_to,
                        source_type=source_type, purchaser=purchaser,
                        salesperson=salesperson, customer_id=customer_id)
    if cat is not None:
        where.append(cat)
    # 下钻键与其余维度同一路径精确匹配（AND 交集）——不折进分类多选 OR 集合，
    # 否则「筛选电器件+机械件后下钻电器件组」会混进机械件行（对抗复审确认项）
    if group_key and group_by:
        where.append(_group_dim(group_by, po, link, dp) == group_key)

    po_cols = [po.id, po.order_no, po.order_date, po.source_type_raw, po.purchaser]
    group_cols = list(po_cols)
    if link is None:
        # 页内补值需要关联键（补值映射按 linked_sales_order_no 查）
        po_cols.append(po.linked_sales_order_no)
        group_cols.append(po.linked_sales_order_no)
    else:
        po_cols += [link.c.salesperson, link.c.customer_id,
                    DimCustomer.name_normalized.label("customer_name")]
        group_cols += [link.c.salesperson, link.c.customer_id,
                       DimCustomer.name_normalized]
    if line_mode:
        ex_e, inc_e = _line_amount_exprs(po, pl)
        paid_e = pl.line_amount
        side = _nominal_side_expr(po)
        base = _link_join(
            select(*po_cols, side.label("nominal_side"), func.count(pl.id).label("lines"),
                   func.sum(inc_e).label("amount_inc"), func.sum(ex_e).label("amount_ex"),
                   func.sum(paid_e).label("amount_paid"))
            .select_from(po)
            .join(pl, _batch_line_join(po, pl))
            .join(dp, pl.part_id == dp.id),
            po, link).where(*where).group_by(*group_cols)
    else:
        ex_e, inc_e, paid_e, side = _order_amount_exprs(po)
        base = _link_join(
            select(*po_cols, side.label("nominal_side"), func.count(pl.id).label("lines"),
                   inc_e.label("amount_inc"), ex_e.label("amount_ex"), paid_e.label("amount_paid"))
            .select_from(po)
            .outerjoin(pl, _batch_line_join(po, pl)),
            po, link).where(*where).group_by(*group_cols)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    if line_mode:
        # 行级口径下金额是聚合列（sum over 匹配行）：排序必须排聚合表达式本身，
        # 排裸行表达式会被 PG 以 GROUP BY 拒绝
        sort_expr = {
            "order_date": po.order_date,
            "amount_inc": func.sum(inc_e),
            "amount_ex": func.sum(ex_e),
            "amount_paid": func.sum(paid_e),
        }[sort]
    else:
        sort_expr = {
            "order_date": po.order_date,
            "amount_inc": inc_e,
            "amount_ex": ex_e,
            "amount_paid": paid_e,
        }[sort]
    direction = sort_expr.desc().nullslast() if order == "desc" else sort_expr.asc().nullslast()
    stmt = base.order_by(direction, po.id.desc()).limit(page_size).offset((page - 1) * page_size)
    rows = db.execute(stmt).all()
    # 无销售/客户参与：页内补关联销售员/客户名（≤ page_size 个 order_no 的 DISTINCT ON
    # 命中 ix_so_order_no，毫秒级；与主语句里有界去重的取值同规则）
    if link is None:
        # 只滤 NULL 不过滤空串：linked='' 与 order_no='' 的等值关联和旧外连接同真值
        sales_map = _page_sales_link(
            db, [r.linked_sales_order_no for r in rows
                 if r.linked_sales_order_no is not None])
        cust_ids = sorted({cid for _sp, cid in sales_map.values() if cid is not None})
        cust_names = (dict(db.execute(
            select(DimCustomer.id, DimCustomer.name_normalized)
            .where(DimCustomer.id.in_(cust_ids))).all()) if cust_ids else {})
    items = []
    for r in rows:
        if link is not None:
            sp, cid, cname = r.salesperson, r.customer_id, r.customer_name
        else:
            sp, cid = sales_map.get(r.linked_sales_order_no, (None, None))
            cname = cust_names.get(cid)
        items.append({
            "order_id": r.id, "order_no": r.order_no,
            "order_date": r.order_date.isoformat() if r.order_date else None,
            "source_type": r.source_type_raw, "purchaser": r.purchaser,
            "salesperson": sp, "customer_id": cid,
            "customer": cname, "lines": r.lines,
            # 名义侧跟随单头税率标记（金臣 2026-10-18 裁定），与金额同组遮蔽
            "nominal_side": r.nominal_side,
            "amount_inc": _f(_round2(r.amount_inc)),
            "amount_ex": _f(_round2(r.amount_ex)),
            "amount_paid": _f(_round2(r.amount_paid)),
        })
    return {
        "total": total, "page": page, "page_size": page_size,
        # sort 回显请求值，effective_sort 回实际生效值（pool_price_analysis /
        # dashboard.part_ranking 先例：金额遮蔽回退时二者不同）
        "sort": requested_sort, "order": order,
        "effective_sort": sort, "ranking_restricted": ranking_restricted,
        "basis_mode": "line" if line_mode else "order",
        "filters_echo": _filters_echo(
            date_from=date_from, date_to=date_to, source_type=source_type,
            purchaser=purchaser, salesperson=salesperson, customer_id=customer_id,
            category=category, group_by=group_by, group_key=group_key),
        "as_of": business_today().isoformat(),
        "items": items,
    }


# ---------------------------------------------------------------- 订单明细

def order_detail(db: Session, order_id: int, user_ctx=None, *,
                 category: list[str] | None = None) -> dict | None:
    """采购模块自己的订单明细（不依赖 page_boss_board）：单头三口径 + **单头批次行**。

    残留旧批次行不出现在这里（D-39 补充决策）；订单不存在或不是已生效 → None。
    category 传入时展示行按分类过滤，并附 ``category_matched`` 行级汇总（行实付 =
    匹配行 line_amount 之和，与列表/分组同一行级口径）。
    """
    po = db.get(FPurchaseOrder, order_id)
    if po is None or po.data_status != config.ACTIVE_STATUS:
        return None
    category = _norm_categories(category)
    ex, inc = order_amounts_reference(
        po.amount_ex_tax, po.amount_inc_tax, po.tax_rate, po.is_tax_inclusive)

    link_row = None
    # 只滤 NULL 不过滤空串：与 orders() 页内补值同一真值（linked='' 等值命中 order_no=''）
    if po.linked_sales_order_no is not None:
        # 单点查找：按 order_no 直取 id 最小一条（同一去重规则，等价性测试对拍），
        # 不再借道全表 DISTINCT ON 子查询（性能 2026-10-17）
        so = FSalesOrder
        link_row = db.execute(
            select(so.salesperson, so.customer_id, DimCustomer.name_normalized)
            .select_from(so)
            .outerjoin(DimCustomer, so.customer_id == DimCustomer.id)
            .where(so.order_no == po.linked_sales_order_no)
            .order_by(so.id)
            .limit(1)
        ).first()

    pl, dp = FPurchaseLine, DimPart
    cat = _category_match(category)
    stmt = (
        select(pl.id, pl.line_no, pl.part_id, dp.pn_std, pl.description, pl.brand,
               pl.qty, pl.unit_price, pl.line_amount, _category_dim(dp).label("category"))
        .select_from(pl)
        .join(dp, pl.part_id == dp.id)
        .where(pl.order_id == po.id, pl.import_batch_id == po.import_batch_id)
    )
    if cat is not None:
        stmt = stmt.where(cat)
    stmt = stmt.order_by(pl.line_no.asc().nullslast(), pl.id)
    lines = []
    matched_paid = matched_inc = matched_ex = Decimal(0)
    for r in db.execute(stmt):
        l_ex, l_inc = line_amounts_reference(r.line_amount, po.tax_rate, po.is_tax_inclusive)
        if r.line_amount is not None:
            matched_paid += r.line_amount
            if l_inc is not None:
                matched_inc += l_inc
            if l_ex is not None:
                matched_ex += l_ex
        lines.append({
            "line_id": r.id, "line_no": r.line_no, "part_id": r.part_id,
            "pn_std": r.pn_std, "description": r.description, "brand": r.brand,
            "qty": _f(r.qty), "unit_price": _f(r.unit_price),
            "line_amount": _f(r.line_amount),
            "category": r.category,
            "line_amount_inc": _f(l_inc),
            "line_amount_ex": _f(l_ex),
        })
    data = {
        "order_id": po.id, "order_no": po.order_no,
        "order_date": po.order_date.isoformat() if po.order_date else None,
        "source_type": po.source_type_raw, "purchaser": po.purchaser,
        "data_status": po.data_status,
        "is_tax_inclusive": po.is_tax_inclusive,
        "tax_rate": _f(po.tax_rate),
        "tax_rate_effective": _f(_effective_rate(po.tax_rate)),
        # 名义侧（金臣 2026-10-18 裁定）：未标税率时只有推算侧是名义值
        "nominal_side": order_nominal_side_reference(
            po.tax_rate, po.is_tax_inclusive, po.amount_ex_tax, po.amount_inc_tax),
        "amount_ex": _f(ex), "amount_inc": _f(inc),
        "amount_paid": _f(po.amount_inc_tax),
        "linked_sales_order_no": po.linked_sales_order_no,
        "salesperson": link_row.salesperson if link_row else None,
        "customer_id": link_row.customer_id if link_row else None,
        "customer": link_row.name_normalized if link_row else None,
        "lines_count": len(lines),
        "lines": lines,
    }
    if cat is not None:
        data["category_matched"] = {
            "lines": len(lines),
            "amount_inc": _f(_round2(matched_inc)),
            "amount_ex": _f(_round2(matched_ex)),
            "amount_paid": _f(_round2(matched_paid)),
        }
    return data
