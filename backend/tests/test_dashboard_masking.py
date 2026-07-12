"""复审 P0-1：老板看板/池分析的新派生成本键必须过字段脱敏——
自定义角色 page_boss_board=True 但 data_purchase_cost=False 时，采购成本相关字段一律遮成 null。"""
from datetime import date

import pytest
from sqlalchemy import select

from app import permissions, security
from app.etl import loader
from app.models.dimensions import DimPart
from app.models.inventory import PartSubstitute
from app.models.system import SysImportBatch
from app.services import dashboard, pool, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


def _ctx(**over):
    """自定义权限：默认全开，再按 over 关某些 data_*。"""
    perms = permissions._full()
    perms.update(over)
    return security.UserContext(user_id="u", role="custom", permissions=perms, is_authenticated=True)


@pytest.fixture()
def seeded(db):
    x = DimPart(pn_std="PN-X", brand="BX"); y = DimPart(pn_std="PN-Y", brand="BY")
    db.add_all([x, y]); db.flush()
    db.add(PartSubstitute(part_id_a=min(x.id, y.id), part_id_b=max(x.id, y.id),
                          status="active", direction="both", substitute_type="same_spec"))
    db.flush()
    pool.rebuild(db)
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hmask")
    db.add(b); db.flush()
    po = {"P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
          "P2": f.purchase_head("P2", on=date(2026, 1, 9), is_tax_inclusive=True)}
    pl = [f.purchase_line("P1", "PLX1", "PN-X", qty="5", price="113"),
          f.purchase_line("P2", "PLX2", "PN-X", qty="5", price="113"),
          f.purchase_line("P1", "PLY", "PN-Y", qty="5", price="226")]
    loader.load(db, f.purchase_result(po, pl), b.id, date(2026, 6, 1))
    so = {"S1": f.sales_head("S1", on=date(2026, 2, 1))}
    sl = [f.sales_line("S1", "SLY", "PN-Y", qty="10", price="400"),
          f.sales_line("S1", "SLX", "PN-X", qty="2", price="400")]
    loader.load(db, f.sales_result(so, sl), b.id, date(2026, 6, 1))
    db.commit(); profit.recompute(db)
    gid = db.execute(select(pool.PartPoolMember.group_id)).scalar()
    return gid


def test_kpi_masks_purchase_and_profit(db, seeded):
    ctx = _ctx(data_purchase_cost=False, data_profit=False)
    k = security.apply_field_visibility(dashboard.kpi(db, None, None, as_of=AS_OF, user_ctx=ctx), ctx)
    assert k["purchase_ex_tax"] is None        # 采购成本遮
    assert k["gross_profit"] is None           # 毛利遮（data_profit=False）
    # 销售额不是成本，仍可见
    assert k["sales_ex_tax"] is not None


def test_ranking_masks_purchase_price(db, seeded):
    ctx = _ctx(data_purchase_cost=False)
    r = security.apply_field_visibility(
        dashboard.part_ranking(db, None, None, as_of=AS_OF, user_ctx=ctx), ctx)
    for row in r["profitable"] + r["loss"]:
        assert row["purchase_price"] is None   # 采购价统计容器遮
        assert row["revenue"] is not None      # 营收仍可见


def test_pool_masks_cost_benchmark_savings(db, seeded):
    ctx = _ctx(data_purchase_cost=False)
    d = security.apply_field_visibility(pool.analyze(db, seeded, as_of=AS_OF, user_ctx=ctx), ctx)
    assert d["benchmark"] is None              # 含成本标杆 → 整块遮
    assert d["savings"] is None                # 含节省额 → 整块遮
    for m in d["members"]:
        assert m["purchase_price"] is None     # 成员采购成本遮
        assert m["purchase_premium_pct"] is None
    assert d["demand"] is not None             # 需求量非成本，可见


def test_boss_sees_everything(db, seeded):
    """老板 data_purchase_cost=True → 完全不遮。"""
    ctx = _ctx()
    d = security.apply_field_visibility(pool.analyze(db, seeded, as_of=AS_OF, user_ctx=ctx), ctx)
    assert d["benchmark"] is not None and d["savings"] is not None
