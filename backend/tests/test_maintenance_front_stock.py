"""维保前置库账本服务测试（B1）。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.maintenance_front_stock import (
    MaintenanceFrontStock,
    MaintenanceFrontStockLedger,
)
from app.models.maintenance_project import MaintenanceProject
from app.services import maintenance_front_stock as front_stock


@pytest.fixture()
def parts(db):
    part_a = DimPart(pn_std="FS-A-001", description="测试备件A")
    part_b = DimPart(pn_std="FS-B-001", description="测试备件B")
    db.add_all([part_a, part_b])
    db.flush()
    return {"a": part_a.id, "b": part_b.id}


@pytest.fixture()
def project(db):
    p = MaintenanceProject(
        project_id="fs-project-1",
        project_code="前置库测试项目",
        display_name="前置库测试项目",
        lifecycle_status="ongoing",
        is_active=True,
    )
    db.add(p)
    db.flush()
    return p.project_id


def _move(db, *, part_id, kind, source_ref, qty, warehouse="", **kw):
    return front_stock.apply_movement(
        db,
        project_id="fs-project-1",
        part_id=part_id,
        kind=kind,
        source_type=kw.pop("source_type", "f_maintenance_line"),
        source_ref=source_ref,
        qty=Decimal(qty),
        warehouse_name=warehouse,
        operated_by=kw.pop("operated_by", "合成测试员"),
        **kw,
    )


def test_shipment_in_creates_stock_and_ledger(db, parts, project):
    ledger = _move(db, part_id=parts["a"], kind="shipment_in", source_ref="WBDD-LINE-1", qty="3")
    db.commit()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 3.0
    assert stock.last_inbound_at is not None
    assert float(ledger.qty_after) == 3.0
    assert float(ledger.qty_change) == 3.0


def test_movement_idempotent_by_source(db, parts, project):
    first = _move(db, part_id=parts["a"], kind="shipment_in", source_ref="WBDD-LINE-1", qty="3")
    second = _move(db, part_id=parts["a"], kind="shipment_in", source_ref="WBDD-LINE-1", qty="3")
    assert first.ledger_id == second.ledger_id
    db.commit()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 3.0


def test_warehouse_name_splits_identity(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="2", warehouse="现场小库甲")
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-2", qty="5", warehouse="现场小库乙")
    db.commit()
    stocks = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalars().all()
    assert len(stocks) == 2
    by_wh = {s.warehouse_name: float(s.qty) for s in stocks}
    assert by_wh == {"现场小库甲": 2.0, "现场小库乙": 5.0}


def test_return_out_reduces_and_preserves_inbound_age(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="5")
    _move(db, part_id=parts["a"], kind="return_out", source_ref="RET-1", qty="2")
    db.commit()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 3.0
    assert stock.last_inbound_at is not None  # 出账不清库龄锚点


def test_salvage_out_reduces(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="4")
    _move(db, part_id=parts["a"], kind="salvage_out", source_ref="SV-1", qty="1")
    db.commit()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 3.0


def test_negative_balance_rejected(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="2")
    db.commit()
    with pytest.raises(front_stock.FrontStockNegativeBalance):
        _move(db, part_id=parts["a"], kind="return_out", source_ref="RET-9", qty="5")
    db.rollback()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 2.0


def test_balance_rows_with_age(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="3",
          unit_cost_ex_tax=Decimal("100.00"), unit_cost_inc_tax=Decimal("113.00"))
    _move(db, part_id=parts["b"], kind="shipment_in", source_ref="L-2", qty="1")
    db.commit()
    rows = front_stock.balance_rows(db, "fs-project-1")
    assert len(rows) == 2
    row_a = next(r for r in rows if r["pn"] == "FS-A-001")
    assert row_a["qty"] == 3.0
    assert row_a["value_ex_tax"] == 300.0
    assert row_a["age_days"] == 0  # 刚入账
    assert row_a["unit_cost_inc_tax"] == 113.0
    row_b = next(r for r in rows if r["pn"] == "FS-B-001")
    assert row_b["value_ex_tax"] is None


def test_ledger_entries_ordered(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="3")
    _move(db, part_id=parts["a"], kind="return_out", source_ref="RET-1", qty="1")
    db.commit()
    entries = front_stock.ledger_entries(db, "fs-project-1")
    assert len(entries) == 2
    assert entries[0]["kind"] == "return_out"  # 倒序：最新在前
    assert entries[1]["qty_after"] == 3.0


def test_invalid_kind_rejected(db, parts, project):
    with pytest.raises(front_stock.FrontStockInvalidMovement):
        _move(db, part_id=parts["a"], kind="use_out", source_ref="X-1", qty="1")


def test_zero_or_negative_qty_rejected(db, parts, project):
    with pytest.raises(front_stock.FrontStockInvalidMovement):
        _move(db, part_id=parts["a"], kind="shipment_in", source_ref="X-1", qty="0")
    with pytest.raises(front_stock.FrontStockInvalidMovement):
        _move(db, part_id=parts["a"], kind="shipment_in", source_ref="X-2", qty="-3")


def test_same_source_ref_different_payload_rejected(db, parts, project):
    """同一来源事件以不同内容（PN/数量）重放 → payload 冲突失败关闭。"""
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="ORDER-LINE-9", qty="2")
    db.commit()
    with pytest.raises(front_stock.FrontStockPayloadConflict):
        _move(db, part_id=parts["b"], kind="shipment_in", source_ref="ORDER-LINE-9", qty="7")
    db.rollback()
    rows = front_stock.balance_rows(db, "fs-project-1")
    assert {r["pn"]: r["qty"] for r in rows} == {"FS-A-001": 2.0}


def test_version_bumps_on_each_movement(db, parts, project):
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="3")
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-2", qty="2")
    db.commit()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert stock.version == 3  # 创建 1 + 两笔入账
    count = db.execute(
        select(MaintenanceFrontStockLedger).where(
            MaintenanceFrontStockLedger.project_id == "fs-project-1"
        )
    ).scalars().all()
    assert len(count) == 2


def test_unknown_cost_inbound_clears_stale_cost(db, parts, project):
    """未知成本批次入账后不得用旧单价冒充新批成本。"""
    front_stock.apply_movement(
        db,
        project_id="fs-project-1",
        part_id=parts["a"],
        kind="shipment_in",
        source_type="f_maintenance_line",
        source_ref="known-cost-line",
        qty=Decimal("2"),
        unit_cost_ex_tax=Decimal("100.00"),
        unit_cost_inc_tax=Decimal("113.00"),
        operated_by="合成测试员",
    )
    front_stock.apply_movement(
        db,
        project_id="fs-project-1",
        part_id=parts["a"],
        kind="shipment_in",
        source_type="ckd_shipment_line",
        source_ref="unknown-cost-line",
        qty=Decimal("3"),
        unit_cost_ex_tax=None,
        unit_cost_inc_tax=None,
        operated_by="合成测试员",
    )
    db.commit()
    rows = front_stock.balance_rows(db, "fs-project-1")
    assert rows[0]["qty"] == 5.0
    assert rows[0]["unit_cost_ex_tax"] is None
    assert rows[0]["value_ex_tax"] is None


def test_balance_rows_stale_90d_marks_unconsumed(db, parts, project):
    """超 90 天未领用：有结存但近 90 天无现场领用记录（已确认领用单）。"""
    from datetime import date, timedelta

    from app.models.maintenance_project_operations import (
        MaintenanceSiteIssue,
        MaintenanceSiteIssueLine,
    )

    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="L-1", qty="3")
    _move(db, part_id=parts["b"], kind="shipment_in", source_ref="L-2", qty="1")
    issue = MaintenanceSiteIssue(
        issue_id="fs-issue-1",
        project_id="fs-project-1",
        issue_no="FS-ISSUE-0001",
        issue_date=date.today(),
        raw_status="已确认",
        status_mapping_state="mapped",
        normalized_status="confirmed",
        status_mapping_version="synthetic-map-v1",
        source="direct_api",
        version=1,
    )
    db.add(issue)
    db.flush()
    db.add(
        MaintenanceSiteIssueLine(
            issue_line_id="fs-issue-line-1",
            issue_id="fs-issue-1",
            line_no=1,
            part_id=parts["a"],
            pn="FS-A-001",
            quantity=Decimal("2"),
            algorithm_version="synthetic-algo-v1",
        )
    )
    db.commit()
    rows = {r["pn"]: r for r in front_stock.balance_rows(db, "fs-project-1")}
    row_a = rows["FS-A-001"]
    assert row_a["last_consumed_at"] is not None
    assert row_a["days_since_last_consumption"] is not None
    assert row_a["stale_90d"] is False
    row_b = rows["FS-B-001"]
    assert row_b["last_consumed_at"] is None
    assert row_b["days_since_last_consumption"] is None
    assert row_b["stale_90d"] is False  # 新入库未领用不算超期（round-5 Blocker 3）


def test_long_source_ref_preserved_full_length_and_replayed(db, parts, project):
    """129–256 字符引用不再截断：全长存储 + 同长重放幂等（round-4 Blocker 6）。"""
    long_ref = "WBDD-LINE-" + "x" * 240
    assert 128 < len(long_ref) <= 256
    first = _move(db, part_id=parts["a"], kind="shipment_in", source_ref=long_ref, qty="2")
    db.commit()
    replay = _move(db, part_id=parts["a"], kind="shipment_in", source_ref=long_ref, qty="2")
    assert first.ledger_id == replay.ledger_id
    db.commit()
    row = db.execute(
        select(MaintenanceFrontStockLedger).where(
            MaintenanceFrontStockLedger.source_ref == long_ref
        )
    ).scalar_one()
    assert row.source_ref == long_ref
    assert float(row.qty_change) == 2.0
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 2.0  # 重放不重复入账


def test_long_source_refs_sharing_prefix_do_not_collide(db, parts, project):
    """共享前 128 字符的不同引用互相独立（round-4 Blocker 6 反例）。"""
    ref_a = "p" * 128 + "-AAA"
    ref_b = "p" * 128 + "-BBB"
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref=ref_a, qty="1")
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref=ref_b, qty="1")
    db.commit()
    rows = db.execute(
        select(MaintenanceFrontStockLedger).where(
            MaintenanceFrontStockLedger.source_ref.in_([ref_a, ref_b])
        )
    ).scalars().all()
    assert {row.source_ref for row in rows} == {ref_a, ref_b}
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 2.0



def test_single_side_cost_inbound_clears_both_sides(db, parts, project):
    """ex-only / inc-only 入账必须整行置 unknown（缺成本不按 0，round-4 Blocker 12）。"""
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="BOTH-1", qty="1",
          unit_cost_ex_tax=Decimal("100.00"), unit_cost_inc_tax=Decimal("113.00"))
    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="EX-ONLY-1", qty="1",
          unit_cost_ex_tax=Decimal("90.00"), unit_cost_inc_tax=None)
    db.commit()
    row = front_stock.balance_rows(db, "fs-project-1")[0]
    assert row["unit_cost_ex_tax"] is None
    assert row["unit_cost_inc_tax"] is None
    assert row["value_ex_tax"] is None
    assert row["value_inc_tax"] is None


def test_front_stock_api_reports_incomplete_on_single_side_cost(db, parts, project):
    """ex-only 经 service→API 展示为 value_completeness=incomplete。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import auth
    from app.api import maintenance_front_stock
    from app.auth import hash_password
    from app.models.system import SysUser

    _move(db, part_id=parts["a"], kind="shipment_in", source_ref="API-EX-1", qty="1",
          unit_cost_ex_tax=Decimal("90.00"), unit_cost_inc_tax=None)
    db.commit()
    db.add(
        SysUser(
            username="front_stock_api_admin",
            role="admin",
            display_name="前置库API管理员",
            password_hash=hash_password("synthetic-password-123"),
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    app.include_router(maintenance_front_stock.router, prefix="/api")
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "front_stock_api_admin", "password": "synthetic-password-123"},
    )
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    payload = client.get(
        "/api/maintenance/projects/stable/fs-project-1/front-stock"
    ).json()
    assert payload["value_completeness"] == "incomplete"
    assert payload["total_value_ex_tax"] is None
    assert payload["total_value_inc_tax"] is None
    assert payload["stale_90d_count"] == 0  # 新入库未领用不算超期


def test_concurrent_same_source_two_sessions_single_ledger(db, parts, project):
    """两个真实 Session 并发写同来源：恰好一条流水、结存只入一次（round-5 Blocker 9）。"""
    from threading import Barrier, Thread

    from app.db import SessionLocal

    db.commit()  # 让并发 Session 可见项目/备件种子行（主 Session 此前仅 flush）
    errors: list[Exception] = []
    barrier = Barrier(2)

    def worker() -> None:
        session = SessionLocal()
        try:
            barrier.wait()
            front_stock.apply_movement(
                session,
                project_id="fs-project-1",
                part_id=parts["a"],
                kind="shipment_in",
                source_type="f_maintenance_line",
                source_ref="CONCURRENT-SRC-1",
                qty=Decimal("3"),
                warehouse_name="",
                operated_by="并发测试员",
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(exc)
        finally:
            session.close()

    threads = [Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, [str(e) for e in errors]

    ledgers = db.execute(
        select(MaintenanceFrontStockLedger).where(
            MaintenanceFrontStockLedger.source_ref == "CONCURRENT-SRC-1"
        )
    ).scalars().all()
    assert len(ledgers) == 1
    db.expire_all()
    stock = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1"
        )
    ).scalar_one()
    assert float(stock.qty) == 3.0


def test_concurrent_stock_creation_two_sessions_single_row(db, parts, project):
    """两个 Session 并发创建同一 (project, part, warehouse) 结存行：恰一行。"""
    from threading import Barrier, Thread

    from app.db import SessionLocal

    db.commit()  # 让并发 Session 可见项目/备件种子行（主 Session 此前仅 flush）
    errors: list[Exception] = []
    barrier = Barrier(2)

    def worker() -> None:
        session = SessionLocal()
        try:
            barrier.wait()
            front_stock.apply_movement(
                session,
                project_id="fs-project-1",
                part_id=parts["b"],
                kind="shipment_in",
                source_type="f_maintenance_line",
                source_ref=f"NEW-STOCK-{id(barrier)}",
                qty=Decimal("1"),
                warehouse_name="",
                operated_by="并发测试员",
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append(exc)
        finally:
            session.close()

    threads = [Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, [str(e) for e in errors]
    stocks = db.execute(
        select(MaintenanceFrontStock).where(
            MaintenanceFrontStock.project_id == "fs-project-1",
            MaintenanceFrontStock.part_id == parts["b"],
        )
    ).scalars().all()
    assert len(stocks) == 1


def test_stale_90d_only_after_inbound_age_exceeds_window(db, parts, project):
    """入库超过 90 天且从未领用 → 超期；入库未满 90 天 → 不超期。"""
    from datetime import timedelta

    from app.services.maintenance_front_stock import apply_movement

    old = datetime.now(timezone.utc) - timedelta(days=120)
    apply_movement(
        db,
        project_id="fs-project-1",
        part_id=parts["b"],
        kind="shipment_in",
        source_type="f_maintenance_line",
        source_ref="STALE-OLD-1",
        qty=Decimal("1"),
        warehouse_name="",
        occurred_at=old,
        operated_by="合成测试员",
    )
    db.commit()
    rows = {r["pn"]: r for r in front_stock.balance_rows(db, "fs-project-1")}
    assert rows["FS-B-001"]["stale_90d"] is True
    assert rows["FS-B-001"]["age_days"] >= 90
