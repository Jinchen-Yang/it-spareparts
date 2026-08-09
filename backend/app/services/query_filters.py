"""跨 service 共享的查询口径 helper（单一真值源，防口径散落漂移）。

架构体检 2026-06-29 主线 A：`if config.ACTIVE_STATUS_ONLY: stmt.where(data_status=='已生效')`
此前在 profit / part_overview / purchase_analysis / inventory 等散落 14 处、各写一遍，
将来改"已生效"措辞或调开关要搜遍全仓。收敛到此处。
"""
import re

from sqlalchemy import exists, or_, select

from app import config

# 关键词分词：只按空白/逗号切（保留 6Gb/s、3.5-inch、7.2K 这类含内部分隔符的规格词原形——
# resolver 的 PN token 化会剥掉 ./-/ 导致再也匹配不上描述原文，此处刻意不复用那套）。
_TERM_SPLIT = re.compile(r"[\s,;，；]+")
# 单个 CJK 字符是有意义的检索词（"三"=三星、"联"=联想/联通），不能与单个拉丁字母/数字（噪声）
# 一样丢弃。丢弃单 CJK 字会让"搜三"退化为空词表 → 调用方零过滤全表返回（审计 P1）。
_CJK = re.compile(r"[㐀-鿿豈-﫿぀-ヿ]")


def _keep_token(t: str) -> bool:
    return len(t) >= 2 or bool(_CJK.search(t))


def keyword_terms(q: str | None, max_terms: int = 12) -> list[str]:
    """查询串 → ILIKE 词表：分词 + 转义通配符（PG 的 LIKE 默认反斜杠转义）。

    调用方语义＝"全部词都命中"（词序无关）：整段标准描述、或 '8TB 7.2K SATA' 子集组合均可召回。
    单词查询返回单元素列表，行为与整段子串一致。丢弃单个拉丁字母/数字（噪声）但保留单 CJK 字。
    """
    if not (q and q.strip()):
        return []
    out: list[str] = []
    for t in _TERM_SPLIT.split(q.strip()):
        if not _keep_token(t):
            continue
        out.append(_esc(t))
        if len(out) >= max_terms:
            break
    return out


def _esc(t: str) -> str:
    return t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ── 规格词变体归一（甲方 2026-07-03：模糊度太低——6Gbps 搜不到 6Gb/s、3.5寸 搜不到 3.5-inch）──
# 与标准化引擎的写法对齐：容量 8T↔8TB / 速率 6Gbps↔6Gb/s / 转速 7200rpm↔7.2K / 尺寸 寸↔inch。
# 确定性规则，不上向量：失败案例全是词形差异，规则可 100% 覆盖且结果可解释（match_reason）。
_CAP_RX = re.compile(r"^(\d+(?:\.\d+)?)(TB?|GB?)$", re.I)
_SPEED_RX = re.compile(r"^(\d+(?:\.\d+)?)(?:GBPS|GB/S)$", re.I)
_K_RX = re.compile(r"^(\d+(?:\.\d+)?)K(?:RPM)?$", re.I)
_RPM_RX = re.compile(r"^(\d{4,5})(?:RPM|转)$", re.I)
_INCH_RX = re.compile(r"^(\d(?:\.\d)?)(?:-?INCH|-?IN|寸|英寸)$", re.I)


def _variants(t: str) -> list[str]:
    m = _CAP_RX.match(t)
    if m:
        n, u = m.group(1), m.group(2).upper()[0]          # 8TB/8T → 两种写法都收
        return [f"{n}{u}B", f"{n}{u}"]
    m = _SPEED_RX.match(t)
    if m:
        return [f"{m.group(1)}Gb/s", f"{m.group(1)}Gbps"]
    m = _K_RX.match(t)
    if m:
        n = m.group(1)
        out = [f"{n}K"]
        try:
            out.append(str(int(float(n) * 1000)))          # 7.2K → 7200
        except ValueError:
            pass
        return out
    m = _RPM_RX.match(t)
    if m:
        n = m.group(1)
        out = [n]
        if n.endswith("00"):
            out.append(f"{float(n) / 1000:g}K")            # 7200rpm → 7.2K
        return out
    m = _INCH_RX.match(t)
    if m:
        n = m.group(1)
        return [f"{n}-inch", f"{n}inch", f"{n}寸", f"{n}英寸"]
    return [t]


def keyword_term_groups(q: str | None, max_terms: int = 12) -> list[list[str]]:
    """查询串 → 变体词组表：每个词展开为等价写法（**原词，未转义**），组内任一命中即算该词命中。
    丢弃单个拉丁字母/数字（噪声）但保留单 CJK 字（"三"=三星，有意义）。
    匹配一律走 col_matches_any（左词界正则），故此处不做 LIKE 转义。"""
    if not (q and q.strip()):
        return []
    out: list[list[str]] = []
    for t in _TERM_SPLIT.split(q.strip()):
        if not _keep_token(t):
            continue
        out.append(list(dict.fromkeys(_variants(t))))
        if len(out) >= max_terms:
            break
    return out


def keyword_groups_or_substr(q: str | None, max_terms: int = 12) -> list[list[str]]:
    """同 keyword_term_groups，但当查询非空却全是被丢弃的短 token（如单个拉丁字母/数字 '8'/'a'）
    导致无词组时，回退为「整串」一个词组——避免调用方 `for g in groups` 空循环＝零过滤＝
    全表返回（审计 P1：分词化搜索必须对任意非空查询都过滤）。"""
    groups = keyword_term_groups(q, max_terms)
    if groups:
        return groups
    if q and q.strip():
        return [[q.strip()]]
    return []


def col_matches_any(column, variants: list[str]):
    """列匹配「一个查询词的任一等价变体」：**左词界**大小写不敏感正则（PG ~*）。
    左词界＝词前为串首或非「数字/小数点」字符——防子串误命中：'6TB' 不再命中 '16TB'/'1.6TB'、
    '6Gb' 不再命中 '16Gb'。variants 为未转义原词（regex 元字符在此转义）。"""
    return or_(*[column.op("~*")(f"(^|[^0-9.]){re.escape(v)}") for v in variants])


def active_orders(stmt, order_model):
    """按稳定版订单口径过滤；维保墓碑只在正式切换后生效。

    order_model 需有 data_status 列（FPurchaseOrder / FSalesOrder /
    FMaintenanceOrder）。仅用于
    "无条件按生效过滤"的站点；带 status 入参的条件过滤（如 recent_purchases）不适用。
    """
    if config.ACTIVE_STATUS_ONLY:
        stmt = stmt.where(order_model.data_status == config.ACTIVE_STATUS)
    # Beta 删除先作为同库影子事实存在。只有独立的生产口径切换开关打开后，
    # 原稳定版成本、库存、项目和导出读模型才统一消费墓碑。
    if config.get_settings().maintenance_cutover_enabled:
        from sqlalchemy import inspect

        from app.models.maintenance import (
            FMaintenanceOrder,
            MaintenanceDemandTombstone,
        )

        inspected = inspect(order_model, raiseerr=False)
        mapper = getattr(inspected, "mapper", None)
        if mapper is not None and mapper.class_ is FMaintenanceOrder:
            stmt = stmt.where(
                ~exists(
                    select(1).where(
                        MaintenanceDemandTombstone.source_order_id
                        == order_model.raw_order_id,
                        MaintenanceDemandTombstone.restored_at.is_(None),
                    )
                )
            )
    return stmt


def active_beta_maintenance_orders(stmt, order_model):
    """Apply the Beta-only WBDD tombstone boundary.

    A Beta deletion is a shadow fact until the explicit maintenance cutover.
    Stable production cost, inventory and export readers therefore continue to
    use :func:`active_orders`, while Beta assignment/warehouse/workspace readers
    opt in here.  This keeps both interfaces on one database without allowing a
    Beta action to rewrite the stable view.
    """

    from app.models.maintenance import MaintenanceDemandTombstone

    return active_orders(stmt, order_model).where(
        ~exists(
            select(1).where(
                MaintenanceDemandTombstone.source_order_id
                == order_model.raw_order_id,
                MaintenanceDemandTombstone.restored_at.is_(None),
            )
        )
    )
