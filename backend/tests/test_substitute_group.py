"""通用号成组互通 + 库存数（宋总 2026-07-03）：互替闭包 BFS、单向不传递、每号带库存合计。"""
from decimal import Decimal

from app.models.dimensions import DimPart
from app.models.inventory import Inventory, PartSubstitute
from app.services import part_overview


def _link(db, parts, a, b, direction="both"):
    ia, ib = sorted([parts[a].id, parts[b].id])
    if direction == "both":
        rel = "both"
    else:  # one_way：b 可替代 a（与 substitute.add_substitute 的换算一致）
        rel = "a_to_b" if ia == parts[a].id else "b_to_a"
    db.add(PartSubstitute(part_id_a=ia, part_id_b=ib, direction=rel,
                          status="active", source="manual"))


def test_substitute_group_closure_and_stock(db):
    parts = {}
    for pn in ["G-CENTER", "G-SUB1", "G-SUB2", "G-SUB3", "G-ONEWAY"]:
        p = DimPart(pn_std=pn)
        db.add(p)
        parts[pn] = p
    db.flush()
    # 星型：中心 ↔ SUB1/2/3（互替）；ONEWAY 单向可替代中心
    _link(db, parts, "G-CENTER", "G-SUB1")
    _link(db, parts, "G-CENTER", "G-SUB2")
    _link(db, parts, "G-CENTER", "G-SUB3")
    _link(db, parts, "G-CENTER", "G-ONEWAY", "one_way")
    db.add(Inventory(raw_inventory_id="GINV1", part_id=parts["G-SUB1"].id, pn_std="G-SUB1",
                     warehouse="北京成品仓", source_qty=Decimal("7")))
    db.add(Inventory(raw_inventory_id="GINV2", part_id=parts["G-SUB1"].id, pn_std="G-SUB1",
                     warehouse="上海成品仓", source_qty=Decimal("3")))
    db.commit()

    # 查 spoke（宋总用例：输入 2 → 看到 02311JRE + 其余 1/3/4/5）：
    # 直连=中心；其余 spokes 经中心间接互替；单向边不传递（ONEWAY 不可见）
    m = {s["pn_std"]: s for s in part_overview._substitutes(db, parts["G-SUB2"].id)}
    assert set(m) == {"G-CENTER", "G-SUB1", "G-SUB3"}
    assert m["G-CENTER"]["via"] is None and m["G-CENTER"]["relation"] == "互替"
    assert m["G-SUB1"]["via"] == "G-CENTER" and m["G-SUB1"]["relation"] == "互替（间接）"
    assert m["G-SUB1"]["stock_qty"] == 10.0      # 两仓合计
    assert m["G-SUB3"]["stock_qty"] == 0.0       # 无库存显式 0
    # 直连在前、间接在后
    order = [s["pn_std"] for s in part_overview._substitutes(db, parts["G-SUB2"].id)]
    assert order[0] == "G-CENTER"

    # 查中心：全部直连，单向关系可见且方向语义正确
    c = {s["pn_std"]: s for s in part_overview._substitutes(db, parts["G-CENTER"].id)}
    assert set(c) == {"G-SUB1", "G-SUB2", "G-SUB3", "G-ONEWAY"}
    assert c["G-ONEWAY"]["relation"] == "可替代本型号" and c["G-ONEWAY"]["via"] is None
