"""通用号池降本分析：可靠标杆(去重订单≥2的最低加权均价)、双端品牌溢价、
两级节省(理论上限/供应层面上限，均待核实)、客户跨品牌集中度。"""
from datetime import date

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.dimensions import DimPart
from app.models.inventory import PartSubstitute
from app.models.system import SysImportBatch
from app.services import pool, profit
from tests import factories as f

AS_OF = date(2026, 6, 1)


def _part(db, pn, brand):
    p = DimPart(pn_std=pn, brand=brand)
    db.add(p); db.flush()
    return p.id


def _edge(db, a, b):
    lo, hi = (a, b) if a < b else (b, a)
    db.add(PartSubstitute(part_id_a=lo, part_id_b=hi, status="active",
                          direction="both", substitute_type="same_spec"))


@pytest.fixture()
def seeded(db):
    x = _part(db, "PN-X", "BrandX")   # 性价比标杆：ex 100，2 张不同采购单→可靠
    y = _part(db, "PN-Y", "BrandY")   # 品牌溢价：ex 150（+50%）
    z = _part(db, "PN-Z", "BrandZ")   # ex 120（+20%，恰达阈值）
    _edge(db, x, y); _edge(db, y, z)
    db.flush()
    pool.rebuild(db)

    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="hpool")
    db.add(b); db.flush()
    # PN-X 分两张单采购（去重订单=2→标杆可靠；同单两行不算稳定，复审 P1-6）
    porders = {
        "P1": f.purchase_head("P1", on=date(2026, 1, 5), is_tax_inclusive=True),
        "P2": f.purchase_head("P2", on=date(2026, 1, 10), is_tax_inclusive=True),
    }
    plines = [
        f.purchase_line("P1", "PLX1", "PN-X", qty="5", price="113"),   # ex 100
        f.purchase_line("P2", "PLX2", "PN-X", qty="5", price="113"),   # 第二张单
        f.purchase_line("P1", "PLY", "PN-Y", qty="5", price="169.5"),  # ex 150
        f.purchase_line("P1", "PLZ", "PN-Z", qty="5", price="135.6"),  # ex 120
    ]
    loader.load(db, f.purchase_result(porders, plines), b.id, date(2026, 6, 1))
    sorders = {"S1": f.sales_head("S1", on=date(2026, 2, 1))}
    slines = [
        f.sales_line("S1", "SLY", "PN-Y", qty="10", price="300"),   # 买贵的品牌 10 个
        f.sales_line("S1", "SLZ", "PN-Z", qty="5", price="300"),
        f.sales_line("S1", "SLX", "PN-X", qty="2", price="300"),
    ]
    loader.load(db, f.sales_result(sorders, slines), b.id, date(2026, 6, 1))
    db.commit()
    profit.recompute(db)
    gid = db.execute(select(pool.PartPoolMember.group_id)
                     .where(pool.PartPoolMember.part_id == x)).scalar()
    return {"gid": gid, "x": x, "y": y, "z": z}


def test_benchmark_reliable_needs_distinct_orders(db, seeded):
    d = pool.analyze(db, seeded["gid"], as_of=AS_OF)
    assert d["benchmark"]["cost_part_id"] == seeded["x"]
    assert d["benchmark"]["cost_ex_tax"] == 100.0
    assert d["benchmark"]["low_confidence"] is False   # PN-X 2 张不同采购单
    assert d["benchmark"]["supply_ok"] is True


def test_dual_end_brand_premium(db, seeded):
    d = pool.analyze(db, seeded["gid"], as_of=AS_OF)
    by = {m["part_id"]: m for m in d["members"]}
    y = by[seeded["y"]]
    assert y["purchase_price"]["wavg"] == 150.0
    assert y["purchase_premium_pct"] == 0.5
    assert y["brand_premium_purchase"] is True
    z = by[seeded["z"]]
    assert z["purchase_premium_pct"] == 0.2
    assert z["brand_premium_purchase"] is True
    x = by[seeded["x"]]
    assert x["brand_premium_purchase"] is False


def test_savings_no_executable_only_pending(db, seeded):
    """复审 P0-3：绝无"可执行"金额；只有理论上限 + 供应层面上限，每条待核实。"""
    d = pool.analyze(db, seeded["gid"], as_of=AS_OF)
    s = d["savings"]
    assert s["theoretical_max"] == 600.0             # Y:500 + Z:100
    assert s["supply_available_upper"] == 600.0      # 标杆供应可得
    assert s["executable"] is None                   # 无可执行金额
    assert all(o["verification_status"] == "待核实" for o in s["opportunities"])
    assert "executable" not in s["opportunities"][0]  # 不再有布尔"可执行"
    assert s["opportunities"][0]["from_part_id"] == seeded["y"]


def test_customer_cross_brand(db, seeded):
    d = pool.analyze(db, seeded["gid"], as_of=AS_OF)
    cb = d["customer_cross_brand"]
    assert cb["restricted"] is False
    assert cb["multi_brand_customers"] == 1
    assert cb["customers"][0]["brand_count"] == 3
