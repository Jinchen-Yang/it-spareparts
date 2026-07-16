"""DEV-04：采购分析/明细行批量装配有效池身份。"""
from datetime import date, timedelta

from sqlalchemy import event, select

from app.db import engine
from app.etl import loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import pool_catalog, purchase_analysis, purchase_query
from tests import factories as f


def _seed(db):
    batch = SysImportBatch(filename="purchase-pool.xlsx", file_type="purchase", file_hash="ppool")
    db.add(batch); db.flush()
    today = date.today()
    heads = {
        "PP-1": f.purchase_head("PP-1", on=today - timedelta(days=2)),
        "PP-2": f.purchase_head("PP-2", on=today - timedelta(days=1)),
        "PP-3": f.purchase_head("PP-3", on=today),
    }
    lines = [
        f.purchase_line("PP-1", "PPL-1", "POOL-PN-A"),
        f.purchase_line("PP-2", "PPL-2", "POOL-PN-B"),
        f.purchase_line("PP-3", "PPL-3", "NO-POOL-PN"),
    ]
    loader.load(db, f.purchase_result(heads, lines), batch.id, today)
    parts = {p.pn_std: p.id for p in db.execute(select(DimPart)).scalars()}
    created = pool_catalog.create_pool(
        db, name="采购参考池", member_part_ids=[parts["POOL-PN-A"], parts["POOL-PN-B"]],
        operated_by="seed")
    db.commit()
    return created, parts


def test_recent_and_analysis_rows_include_active_pool_identity(db):
    created, parts = _seed(db)
    recent = purchase_query.recent_purchases(db, days=30)
    by_pn = {row["pn_std"]: row for row in recent["items"]}
    for pn in ("POOL-PN-A", "POOL-PN-B"):
        assert by_pn[pn]["part_id"] == parts[pn]
        assert by_pn[pn]["pool_group_id"] == created["group_id"]
        assert by_pn[pn]["pool_name"] == "采购参考池"
    assert by_pn["NO-POOL-PN"]["pool_group_id"] is None
    assert by_pn["NO-POOL-PN"]["pool_name"] is None

    analysis = purchase_analysis.analysis(db, days=30, as_of=date.today())
    analyzed = {row["pn_std"]: row for row in analysis["rows"]}
    assert analyzed["POOL-PN-A"]["pool_group_id"] == created["group_id"]
    assert analyzed["POOL-PN-A"]["pool_name"] == "采购参考池"
    assert analyzed["NO-POOL-PN"]["pool_group_id"] is None


def test_archived_pool_is_not_exposed_and_recent_query_count_is_constant(db):
    created, _ = _seed(db)
    pool_catalog.archive_pool(
        db, group_id=created["group_id"], version=1, operated_by="seed")
    db.commit()
    out = purchase_query.recent_purchases(db, days=30)
    assert all(row["pool_group_id"] is None and row["pool_name"] is None
               for row in out["items"])

    counts = []
    for size in (1, 3):
        seen = 0

        def before_cursor(*_args):
            nonlocal seen
            seen += 1

        event.listen(engine, "before_cursor_execute", before_cursor)
        try:
            purchase_query.recent_purchases(db, days=30, page_size=size)
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor)
        counts.append(seen)
    assert counts[0] == counts[1] == 3  # count + page rows + 一次批量池映射
