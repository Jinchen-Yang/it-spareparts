"""返还录入容量守卫测试（ITSP-1，D-31 第 1/3/4 款）。

验收对照（交接文档 ITSP-1）：
1. N=10、R=10：新增录入下拉不出现该单；直接 API 提交 → 422，事务无半条写入。
2. R=9：qty=1 允许；qty=2 拒绝。
3. 历史 R>N：数据不截断；禁止再增加（超额文案）。
4. 候选过滤与事务内最终校验同时存在。
5. 不发布「已返/未返」状态（本文件不断言状态字样）。
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_doc_import import MaintenanceRkdReturnLine
from app.models.system import SysImportBatch

from tests.test_maintenance_return_receipts_api import _client, _wbdd
from tests.test_site_issue_v2_api import _project

_PART = 9701
_PN = "PNSYN-CAP"


def _seed_part(db) -> None:
    db.add(DimPart(id=_PART, pn_std=_PN, description="容量守卫测试件"))
    db.commit()


def _demand_line(db, order: FMaintenanceOrder, qty: int) -> None:
    batch = SysImportBatch(filename="cap.xlsx", file_type="maintenance",
                           file_hash=uuid4().hex, status="success")
    db.add(batch)
    db.flush()
    db.add(FMaintenanceLine(
        raw_line_id=f"raw-{uuid4()}", order_id=order.id, part_id=_PART,
        pn_std=_PN, qty=Decimal(str(qty)), is_active=True,
        import_batch_id=batch.id,
    ))
    db.commit()


def _candidates(client, project_id: str) -> list[dict]:
    resp = client.get(
        f"/api/maintenance/projects/stable/{project_id}/return-receipt-demands")
    assert resp.status_code == 200, resp.text
    return resp.json()["rows"]


def _register(client, project_id: str, *, qty: int, wbdd: str | None = None):
    return client.post(
        f"/api/maintenance/projects/stable/{project_id}/return-receipts",
        json={"pn": _PN, "qty": qty, **({"wbdd_no": wbdd} if wbdd else {})},
    )


def _patch(client, receipt_id: str, version: int, *, qty: int | None = None,
           wbdd_no: str | None = None, keep_wbdd: bool = False):
    body: dict = {"version": version, "reason": "容量守卫测试"}
    if qty is not None:
        body["qty"] = qty
    if wbdd_no is not None:
        body["wbdd_no"] = wbdd_no
    elif not keep_wbdd:
        body["wbdd_no"] = None
    return client.patch(f"/api/maintenance/return-receipts/{receipt_id}", json=body)


def test_full_order_blocks_register_and_disappears_from_candidates(db):
    _seed_part(db)
    project = _project(db, project_id="cap-p1")
    order = _wbdd(db, project=project)
    _demand_line(db, order, qty=10)

    assert _register(_client(db, username="cap_a"), project.project_id,
                     qty=6, wbdd=order.order_no).status_code == 201
    rows = _candidates(_client(db, username="cap_b"), project.project_id)
    assert [r["order_no"] for r in rows] == [order.order_no]      # R=6 < 10 仍在
    assert rows[0]["remaining_qty"] == "4.000"                    # 富化剩余可录

    client = _client(db, username="cap_c")
    assert _register(client, project.project_id,
                     qty=4, wbdd=order.order_no).status_code == 201  # R=10 满
    # 满额单从下拉消失
    rows = _candidates(client, project.project_id)
    assert rows == []
    # 直接 API 提交 → 422，且无半条写入
    resp = _register(client, project.project_id, qty=1, wbdd=order.order_no)
    assert resp.status_code == 422, resp.text
    assert "剩余可录入 0" in resp.json()["detail"]
    active = db.execute(select(MaintenanceRkdReturnLine).where(
        MaintenanceRkdReturnLine.source_order_id == order.raw_order_id,
        MaintenanceRkdReturnLine.line_status == "active",
    )).scalars().all()
    assert sum(Decimal(str(r.qty)) for r in active) == Decimal("10")  # 无半条写入


def test_partial_capacity_allows_exact_qty_rejects_over(db):
    _seed_part(db)
    project = _project(db, project_id="cap-p2")
    order = _wbdd(db, project=project)
    _demand_line(db, order, qty=10)
    client = _client(db, username="cap_partial")
    assert _register(client, project.project_id,
                     qty=9, wbdd=order.order_no).status_code == 201
    resp = _register(client, project.project_id, qty=2, wbdd=order.order_no)
    assert resp.status_code == 422
    assert "剩余可录入 1" in resp.json()["detail"]
    assert _register(client, project.project_id,
                     qty=1, wbdd=order.order_no).status_code == 201


def test_register_without_demand_link_unguarded(db):
    """D-31 第 2 款：未关联实收独立保留，不占任何需求单容量。"""
    _seed_part(db)
    project = _project(db, project_id="cap-p3")
    order = _wbdd(db, project=project)
    _demand_line(db, order, qty=10)
    client = _client(db, username="cap_free")
    for _ in range(3):
        assert _register(client, project.project_id, qty=10).status_code == 201
    # 未关联录入后再关联需求单，容量守卫照常生效（未关联不占 N/R）
    resp = _register(client, project.project_id, qty=11, wbdd=order.order_no)
    assert resp.status_code == 422
    assert "剩余可录入 10" in resp.json()["detail"]


def test_historical_over_receive_preserved_and_blocked(db):
    _seed_part(db)
    project = _project(db, project_id="cap-p4")
    order = _wbdd(db, project=project)
    _demand_line(db, order, qty=5)
    # 历史超额：直接种子一条 qty=8 的收货（模拟导入超额，D-31 第 3 款场景）
    db.add(MaintenanceRkdReturnLine(
        rkd_line_id=str(uuid4()), batch_id=None, head_row_id=None,
        project_id=project.project_id, source_order_id=order.raw_order_id,
        source="manual", head_no="历史导入", source_ref=f"hist-{uuid4()}",
        part_id=_PART, pn=_PN, qty=8, line_status="active", version=1,
        created_by="t",
    ))
    db.commit()
    client = _client(db, username="cap_over")
    resp = _register(client, project.project_id, qty=1, wbdd=order.order_no)
    assert resp.status_code == 422
    assert "已超额" in resp.json()["detail"]
    assert "历史超额保留" in resp.json()["detail"]
    # 历史数据不截断
    row = db.execute(select(MaintenanceRkdReturnLine).where(
        MaintenanceRkdReturnLine.source_order_id == order.raw_order_id)).scalars().one()
    assert Decimal(str(row.qty)) == Decimal("8")


def test_update_qty_and_relink_respect_capacity(db):
    _seed_part(db)
    project = _project(db, project_id="cap-p5")
    order_a = _wbdd(db, project=project, order_no="WBDD-20260911-000A")
    order_b = _wbdd(db, project=project, order_no="WBDD-20260911-000B")
    _demand_line(db, order_a, qty=10)
    _demand_line(db, order_b, qty=5)
    client = _client(db, username="cap_upd")
    created = _register(client, project.project_id,
                        qty=9, wbdd=order_a.order_no).json()
    version = created["version"]
    # 同单加量超容量 → 422
    resp = _patch(client, created["receipt_id"], version, qty=11, keep_wbdd=True)
    assert resp.status_code == 422
    # 同单加量至容量边界 → 200
    resp = _patch(client, created["receipt_id"], created["version"], qty=10, keep_wbdd=True)
    assert resp.status_code == 200, resp.text
    version = resp.json()["version"]
    # 换关联到 B（N=5，qty=10 超） → 422；改 qty=4 → 200
    resp = _patch(client, created["receipt_id"], version,
                  qty=10, wbdd_no=order_b.order_no)
    assert resp.status_code == 422
    resp = _patch(client, created["receipt_id"], version,
                  qty=4, wbdd_no=order_b.order_no)
    assert resp.status_code == 200, resp.text
