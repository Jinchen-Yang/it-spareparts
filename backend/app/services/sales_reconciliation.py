"""销售单已对账快照（D-40）：内容哈希与「取每单最新」查询。

PR-1（本模块）只提供 schema 层配套的纯函数 / 查询函数；快照写入
（ON CONFLICT DO NOTHING）在 PR-2 导入与回填中接入，接口展示在 PR-3b。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sales import FSalesReconciliationSnapshot

_MONEY_QUANTUM = Decimal("0.01")

# 导入来源枚举（与 ck_sales_recon_snapshot_source CHECK 约束保持一致）
SOURCE_IMPORT = "import"
SOURCE_BACKFILL = "backfill"


def _canonical_amount(value: Decimal | int | float | None) -> str | None:
    """金额规范化：None → None；数值 → 量化到 0.01 的十进制字符串。

    量化取 ROUND_HALF_UP（half away from zero），与 PostgreSQL numeric(14,2)
    的四舍五入一致，保证哈希输入与落库值永远同形。int/float 先过 str 再进
    Decimal，避免二进制浮点尾数混进哈希。
    """
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    quantized = value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    if quantized == 0:
        # PG numeric 把 -0.00 归一成 0.00 存储；哈希与落库保持同形，
        # 否则同一内容因符号零差异绕过 (raw_order_id, content_hash) 去重
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


def latest_snapshots_by_raw_order_id(
    session: Session, raw_order_ids: Sequence[str]
) -> dict[str, FSalesReconciliationSnapshot]:
    """每个氚云数据ID的最新一条快照（D-40 §3：修改时间较新者居首；拍板文本写
    "其次批次号较大者"，实现以行 id DESC 等价替代——追加式写入下 id 与导入
    批次同向单调，且并列时后写入者胜语义不变）。

    排序键 source_modified_at DESC NULLS LAST、id DESC：无修改时间的旧文件
    永远让位给有修改时间的行；同为 NULL 时后插入（id 大）者胜。
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
            FSalesReconciliationSnapshot.id.desc(),
        )
    )
    return {snap.raw_order_id: snap for snap in session.scalars(stmt)}
