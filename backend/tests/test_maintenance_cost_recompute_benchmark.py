"""生产量级维保成本重算基准。

默认跳过，显式运行：
RUN_MAINT_BENCHMARK=1 uv run --frozen --extra dev pytest -q -s \
  tests/test_maintenance_cost_recompute_benchmark.py

发布前按当前生产数量级运行：
RUN_MAINT_BENCHMARK=1 MAINT_BENCHMARK_LINES=40000 \
  uv run --frozen --extra dev pytest -q -s \
  tests/test_maintenance_cost_recompute_benchmark.py
"""

import os
import resource
import time
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.inventory import PartPool, PartPoolMember
from app.models.system import SysImportBatch
from app.services import maintenance_cost
from tests import factories as f


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MAINT_BENCHMARK") != "1",
    reason="set RUN_MAINT_BENCHMARK=1 to run the production-scale benchmark",
)

_LINES = int(os.getenv("MAINT_BENCHMARK_LINES", "4000"))
_UNRESOLVED_LINES = _LINES // 5
_MANUAL_POOLS = max(1, _UNRESOLVED_LINES // 20)
_POOL_SIZE = _UNRESOLVED_LINES // _MANUAL_POOLS
_MAX_RECOMPUTE_SECONDS = max(5.0, _LINES / 2_000)
_MAX_PEAK_RSS_KIB = 1536 * 1024


def test_production_scale_recompute_has_fixed_queries_and_bounded_runtime(db):
    """可配置生产数量级下，重算查询数、耗时和进程峰值内存均有硬门禁。"""
    assert _LINES >= 100
    assert _UNRESOLVED_LINES % _MANUAL_POOLS == 0
    batch = SysImportBatch(
        filename="maintenance-recompute-benchmark.xlsx",
        file_type="maintenance",
        file_hash="maintenance-recompute-benchmark",
    )
    db.add(batch)
    db.flush()

    old_lines = [
        f.purchase_line(
            "P-OLD",
            f"PL-OLD-{index}",
            f"PN-BENCH-{index:05d}",
            qty="1",
            price=str(50 + index % 20),
        )
        for index in range(_UNRESOLVED_LINES)
    ]
    recent_lines = [
        f.purchase_line(
            "P-RECENT",
            f"PL-RECENT-{index}",
            f"PN-BENCH-{index:05d}",
            qty="1",
            price=str(100 + index % 20),
        )
        for index in range(_UNRESOLVED_LINES, _LINES)
    ]
    loader.load(
        db,
        f.purchase_result(
            {
                "P-OLD": f.purchase_head(
                    "P-OLD",
                    # Exactly three natural months before the maintenance
                    # order: eligible for the pool fallback but outside the
                    # direct/window/month layers.
                    on=date(2025, 12, 10),
                    tax_rate=Decimal("0"),
                ),
                "P-RECENT": f.purchase_head(
                    "P-RECENT",
                    on=date(2026, 3, 8),
                    tax_rate=Decimal("0"),
                ),
            },
            [*old_lines, *recent_lines],
        ),
        batch.id,
        date(2026, 7, 28),
    )
    loader.load(
        db,
        f.maintenance_result(
            {"M1": f.maintenance_head("M1", on=date(2026, 3, 10))},
            [
                f.maintenance_line(
                    "M1",
                    f"ML-{index}",
                    f"PN-BENCH-{index:05d}",
                    qty="1",
                )
                for index in range(_LINES)
            ],
        ),
        batch.id,
        date(2026, 7, 28),
    )
    db.flush()

    part_ids = {
        pn: part_id
        for pn, part_id in db.execute(
            select(DimPart.pn_std, DimPart.id).where(
                DimPart.pn_std.in_(
                    [f"PN-BENCH-{index:05d}" for index in range(_UNRESOLVED_LINES)]
                )
            )
        )
    }
    for pool_index in range(_MANUAL_POOLS):
        group_id = 20_000 + pool_index
        first = pool_index * _POOL_SIZE
        db.add(
            PartPool(
                group_id=group_id,
                name=f"基准人工池-{pool_index}",
                status="active",
                source="manual",
                version=1,
                member_count=_POOL_SIZE,
            )
        )
        db.add_all(
            [
                PartPoolMember(
                    group_id=group_id,
                    part_id=part_ids[f"PN-BENCH-{index:05d}"],
                )
                for index in range(first, first + _POOL_SIZE)
            ]
        )
    db.commit()

    select_count = 0

    def capture(_conn, _cursor, statement, _params, _context, _many):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    started = time.perf_counter()
    try:
        stats = maintenance_cost.recompute(db)
    finally:
        elapsed = time.perf_counter() - started
        event.remove(engine, "before_cursor_execute", capture)

    peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        "MAINT_RECOMPUTE_BENCHMARK "
        f"lines={_LINES} unresolved={_UNRESOLVED_LINES} "
        f"manual_pools={_MANUAL_POOLS} elapsed_s={elapsed:.3f} "
        f"select_queries={select_count} peak_rss_kib={peak_rss_kib}"
    )

    assert stats["lines_in_scope"] == _LINES
    assert stats["window"] == _LINES - _UNRESOLVED_LINES
    assert stats["pool_purchase"] == _UNRESOLVED_LINES
    assert stats["none"] == 0
    # advisory lock + 采购池 + 销售池 + 维保行 + 人工池 + 历史采购 + 历史销售。
    assert select_count == 7
    assert elapsed < _MAX_RECOMPUTE_SECONDS
    assert peak_rss_kib < _MAX_PEAK_RSS_KIB
