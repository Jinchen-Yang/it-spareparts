"""K3 writer-side workbook revision invalidation 回归（PostgreSQL）。

契约：
- WBDD order/line 业务字段的实际 insert/update → 所有当前归属项目的
  workbook revision 在同一事务各 bump 一次（旧总表 stale）；
- import_batch_id/时间戳单独变化不算语义变化 → +0；
- upsert 前按解析到的 source order IDs probe 项目并排序预锁 state，写后
  只读复核——出现 probe 外项目 → fail closed 整批回滚，绝不在 order/line
  锁后再拿新 state 锁；
- maintenance_cost.recompute 对实际成本变化的归属项目同事务 bump（两阶段：
  内存完成取价与变化判定 → 先锁 state → 再写行）；
- sales fallback（boss board #51 XSDD 回退层）：销售事实的业务变化同样使
  归属项目旧总表 stale。
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.etl import loader
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.maintenance_project import MaintenanceProject
from app.models.maintenance_project_operations import MaintenanceProjectWorkbookState
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.purchase import FPurchaseLine
from app.models.system import SysAuditLog, SysImportBatch
from app.services import maintenance_cost
from app.services import maintenance_project_operations as ops
from app.services import maintenance_wbdd_import as wbdd
from tests import factories as f
from tests.wbdd_fixtures import COLUMNS_91, make_rows, write_workbook


def _batch(db, file_type="maintenance") -> int:
    b = SysImportBatch(
        filename="k3.xlsx", file_type=file_type,
        file_hash=uuid.uuid4().hex, status="success",
    )
    db.add(b)
    db.flush()
    return b.id


def _make_project(db, *, tag: str, source_order_id: str) -> MaintenanceProject:
    project = MaintenanceProject(
        project_id=str(uuid.uuid4()), project_code=f"K3-{tag}",
        display_name=f"K3项目{tag}", lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid.uuid4()), project_id=project.project_id,
        source_order_id=source_order_id, is_active=True,
        created_by="k3-test",
    ))
    db.commit()
    return project


def _state(db, project_id: str) -> MaintenanceProjectWorkbookState:
    db.expire_all()
    return db.execute(
        select(MaintenanceProjectWorkbookState).where(
            MaintenanceProjectWorkbookState.project_id == project_id
        )
    ).scalar_one()


def _wbdd_workbook(tmp_path, name: str, qty: str) -> str:
    rows = make_rows(orders=1, lines_per_order=1)
    rows[0]["需求明细.需求数量"] = qty
    return write_workbook(str(tmp_path / name), COLUMNS_91, rows)


def _import_wbdd(db, path: str, key: str) -> dict:
    report, replayed = wbdd.import_wbdd(
        db, file_path=path, original_name="wbdd.xlsx",
        operator="k3-tester", idempotency_key=key,
    )
    assert not replayed
    return report


# ---------- WBDD：qty 2→3 必须 +1；no-op 必须 +0；旧总表 stale；新 qty 保留 ----------

def test_wbdd_qty_change_bumps_once_noop_does_not(tmp_path, db):
    # 首导 qty=2（尚无挂靠，无项目可 bump）；随后挂靠项目并取基线 revision=0
    path_v1 = _wbdd_workbook(tmp_path, "v1.xlsx", "2")
    _import_wbdd(db, path_v1, key=f"k3-{uuid.uuid4()}")
    order = db.execute(
        select(FMaintenanceOrder).where(FMaintenanceOrder.order_no == "WBDD-20260001")
    ).scalar_one()
    project = _make_project(db, tag="QTY", source_order_id=order.raw_order_id)
    ops.get_or_create_workbook_state(db, project_id=project.project_id)
    db.commit()
    base = _state(db, project.project_id)
    assert base.revision == 0
    base_version = base.data_version

    # no-op 重导（同内容新 Idempotency-Key，upsert 幂等）：仅 import_batch_id
    # 变化 → 不算语义变化，revision +0
    _import_wbdd(db, path_v1, key=f"k3-{uuid.uuid4()}")
    assert _state(db, project.project_id).revision == 0

    # qty 2→3：语义变化 → 归属项目 revision 恰 +1（loader 与同事务 recompute
    # 都去重后各至多一次），data_version 变化（旧总表 stale），新 qty 保留
    path_v2 = _wbdd_workbook(tmp_path, "v2.xlsx", "3")
    report = _import_wbdd(db, path_v2, key=f"k3-{uuid.uuid4()}")
    state = _state(db, project.project_id)
    assert state.revision == 1, "qty 2→3 必须让归属项目 revision 恰好 +1"
    assert state.data_version != base_version, "旧总表必须 stale（data_version 变化）"
    assert report["workbook_projects_bumped"] == 1
    line = db.execute(
        select(FMaintenanceLine).where(FMaintenanceLine.order_id == order.id)
    ).scalar_one()
    assert line.qty == Decimal("3"), "新 qty 必须保留"


def test_wbdd_import_without_assignment_bumps_nobody(tmp_path, db):
    """无挂靠项目的单：导入正常完成，不创建任何 workbook state。"""
    path = _wbdd_workbook(tmp_path, "lonely.xlsx", "2")
    report = _import_wbdd(db, path, key=f"k3-{uuid.uuid4()}")
    assert report["workbook_projects_bumped"] == 0
    assert db.scalars(select(MaintenanceProjectWorkbookState)).all() == []


# ---------- 通用 maintenance import 路径（loader.load 直调，skip/upsert 双模式） ----------

def test_generic_maintenance_loader_bumps_on_semantic_change_only(db):
    orders = {"M-K3-G": f.maintenance_head("M-K3-G", on=date(2026, 3, 9))}
    lines = [f.maintenance_line("M-K3-G", "ML-K3-G1", "PN-K3-G", qty="2")]
    loader.load(db, f.maintenance_result(orders, lines), _batch(db),
                date(2026, 8, 1), mode="skip")
    db.commit()
    order = db.execute(
        select(FMaintenanceOrder).where(FMaintenanceOrder.raw_order_id == "M-K3-G")
    ).scalar_one()
    project = _make_project(db, tag="GEN", source_order_id=order.raw_order_id)
    ops.get_or_create_workbook_state(db, project_id=project.project_id)
    db.commit()

    # skip 模式重放：全部行已存在、ON CONFLICT DO NOTHING → 零语义写入 → +0
    loader.load(db, f.maintenance_result(orders, lines), _batch(db),
                date(2026, 8, 1), mode="skip")
    db.commit()
    assert _state(db, project.project_id).revision == 0

    # upsert 模式同值重导：仅 import_batch_id 刷新 → +0
    loader.load(db, f.maintenance_result(orders, lines), _batch(db),
                date(2026, 8, 1), mode="upsert")
    db.commit()
    assert _state(db, project.project_id).revision == 0

    # upsert 改量 2→5 → +1，新值保留
    lines_v2 = [f.maintenance_line("M-K3-G", "ML-K3-G1", "PN-K3-G", qty="5")]
    loader.load(db, f.maintenance_result(orders, lines_v2), _batch(db),
                date(2026, 8, 1), mode="upsert")
    db.commit()
    assert _state(db, project.project_id).revision == 1
    line = db.execute(
        select(FMaintenanceLine).where(FMaintenanceLine.raw_line_id == "ML-K3-G1")
    ).scalar_one()
    assert line.qty == Decimal("5")


def test_wbdd_noop_reimport_does_not_audit_derived_flag_flap(db):
    orders = {"M-K3-AUDIT": f.maintenance_head("M-K3-AUDIT", on=date(2026, 3, 9))}
    lines = [f.maintenance_line("M-K3-AUDIT", "ML-K3-AUDIT", "PN-K3-AUDIT", qty="2")]
    loader.load(
        db, f.maintenance_result(orders, lines), _batch(db), date(2026, 8, 1), mode="skip"
    )
    maintenance_cost.recompute(db)
    line = db.scalar(
        select(FMaintenanceLine).where(FMaintenanceLine.raw_line_id == "ML-K3-AUDIT")
    )
    assert "no_cost" in (line.anomaly_flags or [])

    loader.load(
        db,
        f.maintenance_result(orders, lines),
        _batch(db),
        date(2026, 8, 1),
        mode="upsert",
        operated_by="k3-audit",
        audit_overwrites=True,
    )
    maintenance_cost.recompute(db)
    assert db.scalars(
        select(SysAuditLog).where(SysAuditLog.entity_type == "import_overwrite")
    ).all() == []


def test_maintenance_line_reparent_invalidates_old_and_new_projects(db):
    orders = {
        "M-K3-RP-OLD": f.maintenance_head("M-K3-RP-OLD", on=date(2026, 3, 9)),
        "M-K3-RP-NEW": f.maintenance_head("M-K3-RP-NEW", on=date(2026, 3, 10)),
    }
    loader.load(
        db,
        f.maintenance_result(
            orders,
            [f.maintenance_line("M-K3-RP-OLD", "ML-K3-RP", "PN-K3-RP", qty="2")],
        ),
        _batch(db),
        date(2026, 8, 1),
        mode="skip",
    )
    db.commit()
    old_project = _make_project(db, tag="RP-OLD", source_order_id="M-K3-RP-OLD")
    new_project = _make_project(db, tag="RP-NEW", source_order_id="M-K3-RP-NEW")
    for project in (old_project, new_project):
        ops.get_or_create_workbook_state(db, project_id=project.project_id)
    db.commit()

    loader.load(
        db,
        f.maintenance_result(
            orders,
            [f.maintenance_line("M-K3-RP-NEW", "ML-K3-RP", "PN-K3-RP", qty="3")],
        ),
        _batch(db),
        date(2026, 8, 1),
        mode="upsert",
    )
    db.commit()
    assert _state(db, old_project.project_id).revision == 1
    assert _state(db, new_project.project_id).revision == 1
    moved = db.execute(
        select(FMaintenanceLine, FMaintenanceOrder)
        .join(FMaintenanceOrder, FMaintenanceOrder.id == FMaintenanceLine.order_id)
        .where(FMaintenanceLine.raw_line_id == "ML-K3-RP")
    ).one()
    assert moved.FMaintenanceOrder.raw_order_id == "M-K3-RP-NEW"
    assert moved.FMaintenanceLine.qty == Decimal("3")


# ---------- sales fallback：XSDD 回退层销售事实变化 → 归属项目 stale ----------

def test_sales_fallback_change_bumps_assigned_project(db):
    """台账缺位项目的合同额证据来自挂靠 XSDD 的销售事实（boss board #51 回退层）：
    销售单业务字段变化必须 bump；同值重导（仅 import_batch_id）不 bump。"""
    m_orders = {"M-K3-S": f.maintenance_head("M-K3-S", on=date(2026, 3, 9),
                                             sales_order="XSDD-K3-S")}
    m_lines = [f.maintenance_line("M-K3-S", "ML-K3-S1", "PN-K3-S", qty="1")]
    loader.load(db, f.maintenance_result(m_orders, m_lines), _batch(db),
                date(2026, 8, 1), mode="skip")
    db.commit()
    project = _make_project(db, tag="SALES", source_order_id="M-K3-S")
    ops.get_or_create_workbook_state(db, project_id=project.project_id)
    db.commit()

    s_orders = {"S-K3": f.sales_head("S-K3", order_no="XSDD-K3-S",
                                     on=date(2026, 2, 1),
                                     business_type="备件销售",
                                     amount_ex_tax=Decimal("1000"),
                                     tax_rate=Decimal("0.13"))}
    s_lines = [f.sales_line("S-K3", "SL-K3-1", "PN-K3-S", qty="1", price="1130")]
    # 首次插入销售事实 → 回退层证据从无到有 → +1
    loader.load(db, f.sales_result(s_orders, s_lines), _batch(db, "sales"),
                date(2026, 8, 1), mode="skip")
    db.commit()
    assert _state(db, project.project_id).revision == 1

    # 同值 upsert 重导 → +0
    loader.load(db, f.sales_result(s_orders, s_lines), _batch(db, "sales"),
                date(2026, 8, 1), mode="upsert")
    db.commit()
    assert _state(db, project.project_id).revision == 1

    # 金额变化（回退层可见事实）→ +1
    s_orders_v2 = {"S-K3": {**s_orders["S-K3"], "amount_ex_tax": Decimal("2000")}}
    loader.load(db, f.sales_result(s_orders_v2, s_lines), _batch(db, "sales"),
                date(2026, 8, 1), mode="upsert")
    db.commit()
    assert _state(db, project.project_id).revision == 2


def test_sales_order_number_change_invalidates_old_and_new_fallback_projects(db):
    """销售 raw identity 不变但单号修正时，旧、新单号两侧回退证据都变化。"""
    maintenance_rows = (
        ("M-K3-OLD", "XSDD-K3-OLD", "ML-K3-OLD"),
        ("M-K3-NEW", "XSDD-K3-NEW", "ML-K3-NEW"),
    )
    for raw_order_id, sales_order_no, raw_line_id in maintenance_rows:
        loader.load(
            db,
            f.maintenance_result(
                {
                    raw_order_id: f.maintenance_head(
                        raw_order_id,
                        on=date(2026, 3, 9),
                        sales_order=sales_order_no,
                    )
                },
                [f.maintenance_line(raw_order_id, raw_line_id, "PN-K3-RENAME", qty="1")],
            ),
            _batch(db),
            date(2026, 8, 1),
            mode="skip",
        )
    db.commit()
    old_project = _make_project(db, tag="SALES-OLD", source_order_id="M-K3-OLD")
    new_project = _make_project(db, tag="SALES-NEW", source_order_id="M-K3-NEW")
    for project in (old_project, new_project):
        ops.get_or_create_workbook_state(db, project_id=project.project_id)
    db.commit()

    sales_orders = {
        "S-K3-RENAME": f.sales_head(
            "S-K3-RENAME",
            order_no="XSDD-K3-OLD",
            on=date(2026, 2, 1),
            business_type="备件销售",
            amount_ex_tax=Decimal("1000"),
            tax_rate=Decimal("0.13"),
        )
    }
    sales_lines = [
        f.sales_line("S-K3-RENAME", "SL-K3-RENAME", "PN-K3-RENAME", qty="1", price="1130")
    ]
    loader.load(
        db,
        f.sales_result(sales_orders, sales_lines),
        _batch(db, "sales"),
        date(2026, 8, 1),
        mode="skip",
    )
    db.commit()
    assert _state(db, old_project.project_id).revision == 1
    assert _state(db, new_project.project_id).revision == 0

    renamed = {
        "S-K3-RENAME": {**sales_orders["S-K3-RENAME"], "order_no": "XSDD-K3-NEW"}
    }
    loader.load(
        db,
        f.sales_result(renamed, sales_lines),
        _batch(db, "sales"),
        date(2026, 8, 1),
        mode="upsert",
    )
    db.commit()
    assert _state(db, old_project.project_id).revision == 2
    assert _state(db, new_project.project_id).revision == 1


def test_sales_skip_uses_persisted_header_number_for_new_line_invalidation(db):
    m_orders = {
        "M-K3-SKIP": f.maintenance_head(
            "M-K3-SKIP", on=date(2026, 3, 9), sales_order="XSDD-K3-PERSISTED"
        )
    }
    loader.load(
        db,
        f.maintenance_result(
            m_orders,
            [f.maintenance_line("M-K3-SKIP", "ML-K3-SKIP", "PN-K3-SKIP", qty="1")],
        ),
        _batch(db),
        date(2026, 8, 1),
        mode="skip",
    )
    db.commit()
    project = _make_project(db, tag="SALES-SKIP", source_order_id="M-K3-SKIP")
    ops.get_or_create_workbook_state(db, project_id=project.project_id)
    db.commit()

    original = {
        "S-K3-SKIP": f.sales_head(
            "S-K3-SKIP",
            order_no="XSDD-K3-PERSISTED",
            on=date(2026, 2, 1),
            business_type="备件销售",
            amount_ex_tax=Decimal("1000"),
            tax_rate=Decimal("0.13"),
        )
    }
    loader.load(
        db,
        f.sales_result(
            original,
            [f.sales_line("S-K3-SKIP", "SL-K3-SKIP-1", "PN-K3-SKIP", qty="1", price="1130")],
        ),
        _batch(db, "sales"),
        date(2026, 8, 1),
        mode="skip",
    )
    db.commit()
    assert _state(db, project.project_id).revision == 1

    # skip 会保留既有 header 的 PERSISTED 单号，但允许插入新 line；失效必须
    # 跟数据库实际 parent，而不是跟 incoming payload 的 WRONG 单号。
    incoming = {
        "S-K3-SKIP": {**original["S-K3-SKIP"], "order_no": "XSDD-K3-WRONG"}
    }
    loader.load(
        db,
        f.sales_result(
            incoming,
            [f.sales_line("S-K3-SKIP", "SL-K3-SKIP-2", "PN-K3-SKIP", qty="1", price="1130")],
        ),
        _batch(db, "sales"),
        date(2026, 8, 1),
        mode="skip",
    )
    db.commit()
    assert _state(db, project.project_id).revision == 2


# ---------- fail closed：写后复核发现 probe 外项目 → 整批回滚 ----------

def test_import_fails_closed_when_unprobed_project_appears(db, monkeypatch):
    """预锁后、写后复核前若并发挂进了新项目：禁止在 order/line 锁后再拿新
    state 锁——直接回滚整批，调用方重试收敛。"""
    orders = {"M-K3-FC": f.maintenance_head("M-K3-FC", on=date(2026, 3, 9))}
    lines = [f.maintenance_line("M-K3-FC", "ML-K3-FC1", "PN-K3-FC", qty="2")]
    loader.load(db, f.maintenance_result(orders, lines), _batch(db),
                date(2026, 8, 1), mode="skip")
    db.commit()
    project = _make_project(db, tag="FC", source_order_id="M-K3-FC")

    calls = {"n": 0}
    real_probe = loader._probe_assigned_project_ids

    def spy(session, source_order_ids):
        calls["n"] += 1
        out = real_probe(session, source_order_ids)
        if calls["n"] == 2:  # 第二次 = 写后复核：注入预锁集合外的项目
            return set(out) | {"ghost-project"}
        return out

    monkeypatch.setattr(loader, "_probe_assigned_project_ids", spy)
    lines_v2 = [f.maintenance_line("M-K3-FC", "ML-K3-FC1", "PN-K3-FC", qty="9")]
    with pytest.raises(loader.WorkbookInvalidationConflictError):
        loader.load(db, f.maintenance_result(orders, lines_v2), _batch(db),
                    date(2026, 8, 1), mode="upsert")
    # 整批回滚：旧 qty 保留，revision 未 bump
    line = db.execute(
        select(FMaintenanceLine).where(FMaintenanceLine.raw_line_id == "ML-K3-FC1")
    ).scalar_one()
    assert line.qty == Decimal("2")
    states = db.scalars(select(MaintenanceProjectWorkbookState)).all()
    assert all(s.revision == 0 for s in states)


# ---------- recompute：代表性漏 writer——成本实际变化才 bump ----------

def _load_priced_maintenance(db, *, tag: str, price: str) -> MaintenanceProject:
    """采购直配（linked_maintenance_order_no=维保单号）→ recompute 必得 direct 成本。"""
    p_orders = {f"P-K3-{tag}": f.purchase_head(
        f"P-K3-{tag}", on=date(2026, 3, 2), source_type="维保需求",
        linked_maintenance_order_no=f"WBDD-K3-{tag}")}
    p_lines = [f.purchase_line(f"P-K3-{tag}", f"PL-K3-{tag}", f"PN-K3-{tag}",
                               qty="2", price=price)]
    loader.load(db, f.purchase_result(p_orders, p_lines), _batch(db, "purchase"),
                date(2026, 8, 1), mode="skip")
    m_orders = {f"M-K3-{tag}": f.maintenance_head(
        f"M-K3-{tag}", order_no=f"WBDD-K3-{tag}", on=date(2026, 3, 9))}
    m_lines = [f.maintenance_line(f"M-K3-{tag}", f"ML-K3-{tag}1",
                                  f"PN-K3-{tag}", qty="2")]
    loader.load(db, f.maintenance_result(m_orders, m_lines), _batch(db),
                date(2026, 8, 1), mode="skip")
    db.commit()
    return _make_project(db, tag=tag, source_order_id=f"M-K3-{tag}")


def test_recompute_bumps_only_on_actual_cost_change(db):
    project = _load_priced_maintenance(db, tag="RC", price="100")
    ops.get_or_create_workbook_state(db, project_id=project.project_id)
    db.commit()

    # 首轮：成本从 NULL → direct（语义变化）→ +1
    stats = maintenance_cost.recompute(db)
    assert stats["projects_workbook_bumped"] == 1
    assert _state(db, project.project_id).revision == 1
    line = db.execute(
        select(FMaintenanceLine).where(FMaintenanceLine.raw_line_id == "ML-K3-RC1")
    ).scalar_one()
    assert line.cost_source == "direct"
    assert line.cost_amount is not None

    # 第二轮：输入事实未变 → 成本结果逐字段一致 → +0（幂等，不多 bump）
    stats = maintenance_cost.recompute(db)
    assert stats["projects_workbook_bumped"] == 0
    assert _state(db, project.project_id).revision == 1

    # 采购价 100→200：实际成本变化 → +1
    pline = db.execute(
        select(FPurchaseLine).where(FPurchaseLine.raw_line_id == "PL-K3-RC")
    ).scalar_one()
    pline.unit_price = Decimal("200")
    pline.line_amount = pline.qty * Decimal("200")
    db.commit()
    stats = maintenance_cost.recompute(db)
    assert stats["projects_workbook_bumped"] == 1
    assert _state(db, project.project_id).revision == 2


def test_recompute_fails_closed_when_unprobed_project_appears(db, monkeypatch):
    """recompute 写后复核发现预锁集合外项目 → 整体回滚，已算出的新成本不落库。"""
    project = _load_priced_maintenance(db, tag="RF", price="100")
    ops.get_or_create_workbook_state(db, project_id=project.project_id)
    db.commit()

    calls = {"n": 0}
    real_probe = maintenance_cost._probe_assigned_project_ids

    def spy(session, source_order_ids):
        calls["n"] += 1
        out = real_probe(session, source_order_ids)
        if calls["n"] == 2:  # 第二次 = 写后复核
            return set(out) | {"ghost-project"}
        return out

    monkeypatch.setattr(maintenance_cost, "_probe_assigned_project_ids", spy)
    with pytest.raises(maintenance_cost.WorkbookInvalidationConflictError):
        maintenance_cost.recompute(db)
    line = db.execute(
        select(FMaintenanceLine).where(FMaintenanceLine.raw_line_id == "ML-K3-RF1")
    ).scalar_one()
    assert line.cost_source is None, "fail closed：回滚后成本保持重算前状态"
    assert _state(db, project.project_id).revision == 0
