"""跨 service 共享的查询口径 helper（单一真值源，防口径散落漂移）。

架构体检 2026-06-29 主线 A：`if config.ACTIVE_STATUS_ONLY: stmt.where(data_status=='已生效')`
此前在 profit / part_overview / purchase_analysis / inventory 等散落 14 处、各写一遍，
将来改"已生效"措辞或调开关要搜遍全仓。收敛到此处。
"""
import re

from app import config

# 关键词分词：只按空白/逗号切（保留 6Gb/s、3.5-inch、7.2K 这类含内部分隔符的规格词原形——
# resolver 的 PN token 化会剥掉 ./-/ 导致再也匹配不上描述原文，此处刻意不复用那套）。
_TERM_SPLIT = re.compile(r"[\s,;，；]+")


def keyword_terms(q: str | None, max_terms: int = 12) -> list[str]:
    """查询串 → ILIKE 词表：分词 + 转义通配符（PG 的 LIKE 默认反斜杠转义）。

    调用方语义＝"全部词都命中"（词序无关）：整段标准描述、或 '8TB 7.2K SATA' 子集组合均可召回。
    单词查询返回单元素列表，行为与整段子串一致。
    """
    if not (q and q.strip()):
        return []
    out: list[str] = []
    for t in _TERM_SPLIT.split(q.strip()):
        if len(t) < 2:
            continue
        out.append(t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))
        if len(out) >= max_terms:
            break
    return out


def active_orders(stmt, order_model):
    """按"已生效"过滤订单查询（受 config.ACTIVE_STATUS_ONLY 开关控制；关则不过滤）。

    order_model 需有 data_status 列（FPurchaseOrder / FSalesOrder）。仅用于
    "无条件按生效过滤"的站点；带 status 入参的条件过滤（如 recent_purchases）不适用。
    """
    if config.ACTIVE_STATUS_ONLY:
        return stmt.where(order_model.data_status == config.ACTIVE_STATUS)
    return stmt
