"""销售单已对账快照（D-40）：内容哈希与「取每单最新」查询。

PR-1（本模块）只提供 schema 层配套的纯函数 / 查询函数；快照写入
（ON CONFLICT DO NOTHING）在 PR-2 导入与回填中接入，接口展示在 PR-3b。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sales import FSalesReconciliationSnapshot

_MONEY_QUANTUM = Decimal("0.01")

# 导入来源枚举（与 ck_sales_recon_snapshot_source CHECK 约束保持一致）
SOURCE_IMPORT = "import"
SOURCE_BACKFILL = "backfill"


def _canonical_amount(value: Decimal | int | float | None) -> str | None:
    """金额规范化：None → None；数值 → 量化到 0.01 的十进制字符串。

    两级量化现实（#395 检察官质询的说明，2026-10-18）：真实链路上金额先经
    ``cleaner.parse_money``（HALF_EVEN，decimal 默认上下文）量化到 0.01 落库；
    本函数收到的输入已是两位小数，这里的 ROUND_HALF_UP 再量化是**幂等空操作**。
    对未预量化的直接调用（测试/回填旁路），HALF_UP 与 PostgreSQL numeric(14,2)
    的 half-away-from-zero 舍入一致——两种取向下「哈希输入 ↔ 落库值」都同形，
    不改变实现（改舍入模式会动摇既有 content_hash 的可比性），幂等性由
    test_canonical_amount_idempotent_after_parse_money 看守。

    int/float 先过 str 再进 Decimal，避免二进制浮点尾数混进哈希。
    """
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    quantized = value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    if quantized == 0:
        # PG numeric 把 -0.00 归一成 0.00 存储；哈希与落库保持同形，
        # 否则符号零差异会让 content_hash 误报"内容变化"
        quantized = abs(quantized)
    return str(quantized)


def _canonical_time(value: datetime | None) -> str | None:
    """时间规范化：None → None；datetime → 统一 UTC 的 ISO 8601 字符串。

    带时区的先折算到 UTC；naive 视为 UTC（调用方负责先完成氚云北京时间
    的时区归一，本函数不猜偏移）。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def snapshot_content_hash(
    *,
    raw_order_id: str,
    source_modified_at: datetime | None,
    order_amount: Decimal | int | float | None,
    reconciled_amount: Decimal | int | float | None,
    pending_amount: Decimal | int | float | None,
    return_reconciled_amount: Decimal | int | float | None,
) -> str:
    """对 (raw_order_id, source_modified_at, 四金额) 规范化后取 sha256（64 位 hex）。

    规范化规则（导入与回填共用，重复文件 / 重跑得到相同哈希）：
    - raw_order_id：原样参与（清洗由调用方完成）；
    - source_modified_at：统一 UTC ISO 8601（_canonical_time），None 记 null；
    - 四个金额：量化到 0.01 的字符串（_canonical_amount），None 记 null——
      null 与 "0.00" 在 JSON 数组里天然可区分，空单元格绝不等价于 0。

    序列化为 JSON 数组（定长、分隔符无歧义），对 UTF-8 字节取 sha256。
    """
    canonical = json.dumps(
        [
            raw_order_id,
            _canonical_time(source_modified_at),
            _canonical_amount(order_amount),
            _canonical_amount(reconciled_amount),
            _canonical_amount(pending_amount),
            _canonical_amount(return_reconciled_amount),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_row_from_head(
    *, raw_order_id: str, head: Mapping[str, Any],
    batch_id: int, source: str, sales_order_id: int | None,
) -> dict[str, Any]:
    """TransformResult.orders 的单头观测 → 快照行字典（#396 S5 去重）。

    导入（etl/loader）与历史原件回填（scripts/backfill_sales_reconciliation）
    唯一的行构造事实源：金额四列映射（order_amount ← head 的 amount_inc_tax）、
    content_hash 与落库值同形。只负责**行内容**；插入策略（ON CONFLICT DO
    NOTHING / 先筛 existing）与 sales_order_id 解析时机由调用方自选——导入路径
    订单已在本事务写入，回填路径订单可能缺席（允许 None）。
    """
    amounts = {
        "order_amount": head.get("amount_inc_tax"),
        "reconciled_amount": head.get("reconciled_amount"),
        "pending_amount": head.get("pending_amount"),
        "return_reconciled_amount": head.get("return_reconciled_amount"),
    }
    return {
        "raw_order_id": raw_order_id,
        "order_no": head.get("order_no"),
        "sales_order_id": sales_order_id,
        "import_batch_id": batch_id,
        "source": source,
        "source_modified_at": head.get("source_modified_at"),
        **amounts,
        "content_hash": snapshot_content_hash(
            raw_order_id=raw_order_id,
            source_modified_at=head.get("source_modified_at"),
            **amounts),
    }


def snapshot_rows_from_orders(
    order_heads: Mapping[str, Mapping[str, Any]], *,
    batch_id: int, source: str,
    oid_map: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """整批单头 → 快照行列表（snapshot_row_from_head 的批量形态）。"""
    return [
        snapshot_row_from_head(
            raw_order_id=raw_id, head=head, batch_id=batch_id, source=source,
            sales_order_id=(oid_map or {}).get(raw_id))
        for raw_id, head in order_heads.items()
    ]


def latest_snapshots_by_raw_order_id(
    session: Session, raw_order_ids: Sequence[str]
) -> dict[str, FSalesReconciliationSnapshot]:
    """每个氚云数据ID的最新一条快照（D-40 §3）。

    排序键 source_modified_at DESC NULLS LAST、import_batch_id DESC、id DESC：
    无修改时间的旧文件永远让位给有修改时间的行；修改时间相同（或都为空）时
    批次号较大者胜——并发/回填顺序下行 id 不等于批次先后，不能当次序依据；
    (raw_order_id, import_batch_id) 唯一约束下 id 仅为最终兜底（理论不可达）。
    """
    if not raw_order_ids:
        return {}
    stmt = (
        select(FSalesReconciliationSnapshot)
        .where(FSalesReconciliationSnapshot.raw_order_id.in_(list(raw_order_ids)))
        .distinct(FSalesReconciliationSnapshot.raw_order_id)
        .order_by(
            FSalesReconciliationSnapshot.raw_order_id,
            FSalesReconciliationSnapshot.source_modified_at.desc().nulls_last(),
            FSalesReconciliationSnapshot.import_batch_id.desc(),
            FSalesReconciliationSnapshot.id.desc(),
        )
    )
    return {snap.raw_order_id: snap for snap in session.scalars(stmt)}
