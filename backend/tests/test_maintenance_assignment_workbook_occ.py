"""写侧 OCC：归属/负责人写入与 MaintenanceProjectWorkbookState revision 的口径。

覆盖：
1. 直接 assign/archive primary_manager：每次真实变更 revision 恰好 +1；
2. 冲突/no-op（陈旧版本、同人重复指派）：零写、revision 不变；
3. auto_assign 只认销售合同 XSDD owner：挂靠 + 销售/负责人回填 + 账号指派
   多变更同事务仍只 +1；无/非法 XSDD 一律跳过，绝不按名称匹配或新建项目；
4. backfill 全 no-op：+0；
5. 锁序/并发：规划后归属并发出现 / prelocked states 未覆盖候选 →
   fail closed（SourceAssignmentConflict），零半截写入。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
import threading

import pytest
from sqlalchemy import event, select, text

from app.auth import hash_password
from app.db import SessionLocal
from app.etl import loader
from app.models.maintenance import FMaintenanceOrder
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAuditLog,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_project_operations import (
    MaintenanceProjectWorkbookState,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.system import SysImportBatch, SysUser
from app.security import UserContext
from app.services import maintenance_project_assignments as pa
from app.services import maintenance_project_catalog as catalog
from app.services import maintenance_project_operations as operations
from app.services import maintenance_source_assignments as sa
from app.services.maintenance_manager_workbook_adapter import (
    MaintenanceManagerWorkbookAdapter,
)
from tests import factories as f


def _load_orders(
    db, *, project: str, n: int = 1, sales_order: str | None = None
) -> list[FMaintenanceOrder]:
    batch = SysImportBatch(
        filename=f"synthetic-occ-{project}.xlsx",
        file_type="maintenance",
        file_hash=f"occ-{project}-{n}-{sales_order}".ljust(64, "0"),
        status="success",
    )
    db.add(batch)
    db.flush()
    heads = {}
    for i in range(n):
        raw_id = f"WBDD-OCC-{project}-{i}"
        heads[raw_id] = f.maintenance_head(
            raw_id, order_no=f"NO-OCC-{project}-{i}", project=project,
            sales_order=sales_order,
        )
    lines = [f.maintenance_line(rid, f"{rid}-L1", "PN-OCC-001") for rid in heads]
    loader.load(db, f.maintenance_result(heads, lines), batch.id,
                date(2026, 8, 26), mode="upsert")
    db.commit()
    return list(
        db.execute(
            select(FMaintenanceOrder).where(
                FMaintenanceOrder.raw_order_id.in_(list(heads))
            )
        ).scalars()
    )


def _contract_owner_project(
    db, *, project_id: str, code: str, name: str, xsdd: str
) -> MaintenanceProject:
    """显式人工建项目 + 销售合同建立 XSDD owner（合同是唯一事实来源）。"""
    project = MaintenanceProject(
        project_id=project_id, project_code=code,
        display_name=name, lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()  # 测试会话 autoflush=False；create_contract 先按 project_id 查主档
    operations.create_contract(
        db,
        project_id=project_id,
        contract_id=f"{project_id}-contract",
        contract_no=f"XSDD-{xsdd}",
        contract_amount=Decimal("100.00"),
        contract_status="正常",
        status_mapping_state="mapped",
        status_mapping_version="occ-test-v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        source="test",
        reason="销售合同先建立 XSDD owner",
        operated_by="occ-contract-setup",
    )
    db.commit()
    return project


def _admin_ctx() -> UserContext:
    return UserContext(user_id="occ-admin", role="admin", is_authenticated=True)


def _state(db, project_id: str) -> MaintenanceProjectWorkbookState | None:
    return db.scalar(
        select(MaintenanceProjectWorkbookState).where(
            MaintenanceProjectWorkbookState.project_id == project_id
        )
    )


def _revision(db, project_id: str) -> int:
    state = _state(db, project_id)
    return state.revision if state is not None else 0


def _user(db, username: str, *, salesperson_name: str | None = None) -> SysUser:
    user = SysUser(
        username=username,
        role="purchaser",
        display_name=username,
        salesperson_name=salesperson_name,
        password_hash=hash_password("synthetic-occ-password-1"),
        is_active=True,
    )
    db.add(user)
    db.commit()
    return user


def test_direct_assign_and_archive_bump_revision_once_each(db):
    project = MaintenanceProject(
        project_id="occ-direct", project_code="OCC-DIRECT",
        display_name="OCC直接改派项目", lifecycle_status="ongoing",
    )
    db.add(project)
    first = _user(db, "occ_direct_first")
    second = _user(db, "occ_direct_second")

    created = pa.assign_primary_manager(
        db, project_id=project.project_id, user_id=first.id,
        expected_assignment_id=None, expected_assignment_version=None,
        reason="首次指定主负责人", operated_by="occ-admin",
    )
    db.commit()
    assert created["version"] == 1
    assert _revision(db, project.project_id) == 1

    reassigned = pa.assign_primary_manager(
        db, project_id=project.project_id, user_id=second.id,
        expected_assignment_id=created["assignment_id"],
        expected_assignment_version=1,
        reason="改派主负责人", operated_by="occ-admin",
    )
    db.commit()
    assert _revision(db, project.project_id) == 2

    archived = pa.archive_primary_manager(
        db, assignment_id=reassigned["assignment_id"], version=1,
        reason="归档负责人关系", operated_by="occ-admin",
    )
    db.commit()
    assert archived["archived_at"] is not None
    assert _revision(db, project.project_id) == 3


def test_assign_conflict_and_noop_write_nothing(db):
    project = MaintenanceProject(
        project_id="occ-noop", project_code="OCC-NOOP",
        display_name="OCC零写项目", lifecycle_status="ongoing",
    )
    db.add(project)
    first = _user(db, "occ_noop_first")
    second = _user(db, "occ_noop_second")
    created = pa.assign_primary_manager(
        db, project_id=project.project_id, user_id=first.id,
        expected_assignment_id=None, expected_assignment_version=None,
        reason="首次指定主负责人", operated_by="occ-admin",
    )
    db.commit()
    assert _revision(db, project.project_id) == 1

    def audit_count() -> int:
        return len(list(db.scalars(
            select(MaintenanceProjectAuditLog).where(
                MaintenanceProjectAuditLog.project_id == project.project_id
            )
        )))

    before_audits = audit_count()

    # 陈旧版本改派 → 409 语义，零写
    with pytest.raises(pa.MaintenanceProjectAssignmentConflict):
        pa.assign_primary_manager(
            db, project_id=project.project_id, user_id=second.id,
            expected_assignment_id=created["assignment_id"],
            expected_assignment_version=99,
            reason="过期版本改派", operated_by="occ-admin",
        )
    db.rollback()
    # 同人重复指派（no-op 请求显式拒绝）→ 零写
    with pytest.raises(pa.MaintenanceProjectAssignmentError):
        pa.assign_primary_manager(
            db, project_id=project.project_id, user_id=first.id,
            expected_assignment_id=created["assignment_id"],
            expected_assignment_version=1,
            reason="同人重复指派", operated_by="occ-admin",
        )
    db.rollback()
    # 陈旧版本归档 → 零写
    with pytest.raises(pa.MaintenanceProjectAssignmentConflict):
        pa.archive_primary_manager(
            db, assignment_id=created["assignment_id"], version=99,
            reason="过期版本归档", operated_by="occ-admin",
        )
    db.rollback()

    assert _revision(db, project.project_id) == 1
    assert audit_count() == before_audits


def test_manager_snapshot_waits_for_state_without_holding_owner_user(db):
    """Real PostgreSQL two-session regression for the former user→state cycle.

    Session A models the canonical direct-assign prefix (state, then target
    user).  Session B loads a manager snapshot with row locks.  B must block on
    the state *without* already owning the user row; otherwise A's later user
    lock forms the exact historical deadlock cycle.
    """

    manager = _user(db, "occ_manager_lock_order")
    project = MaintenanceProject(
        project_id="occ-manager-lock-order",
        project_code="OCC-MANAGER-LOCK-ORDER",
        display_name="OCC项目经理锁序项目",
        lifecycle_status="ongoing",
    )
    db.add(project)
    db.flush()
    db.add(
        MaintenanceProjectUserAssignment(
            assignment_id="occ-manager-lock-order-assignment",
            project_id=project.project_id,
            responsibility_type="primary_manager",
            user_id=manager.id,
            version=1,
            assigned_at=datetime.now(UTC),
            assigned_by="occ-admin",
            assignment_reason="锁序回归测试",
        )
    )
    operations.lock_workbook_states(db, project_ids=[project.project_id])
    manager_id = manager.id
    manager_username = manager.username
    project_id = project.project_id
    db.commit()

    state_lock_attempted = threading.Event()

    def load_locked_snapshot() -> dict:
        with SessionLocal() as manager_db:
            connection = manager_db.connection()
            connection.execute(text("SET LOCAL deadlock_timeout = '100ms'"))
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            connection.execute(text("SET LOCAL statement_timeout = '10s'"))

            def observe_state_lock(
                _conn, _cursor, statement, _parameters, _context, _executemany,
            ) -> None:
                normalized = " ".join(statement.lower().split())
                if (
                    "from maintenance_project_workbook_state" in normalized
                    and "for update" in normalized
                ):
                    state_lock_attempted.set()

            event.listen(connection, "before_cursor_execute", observe_state_lock)
            try:
                adapter = MaintenanceManagerWorkbookAdapter(
                    manager_db,
                    user_ctx=UserContext(
                        user_id=manager_username,
                        role="purchaser",
                        is_authenticated=True,
                        permissions={
                            "page_maintenance": True,
                            "data_profit": True,
                            "data_purchase_cost": True,
                        },
                    ),
                    operator=manager_username,
                    as_of=date(2026, 8, 26),
                )
                snapshot = adapter.load_snapshot(date(2026, 8, 1), lock=True)
                manager_db.commit()
                return snapshot
            finally:
                event.remove(
                    connection, "before_cursor_execute", observe_state_lock
                )

    # Session A takes state first, exactly as assign_primary_manager does.
    with SessionLocal() as direct_db:
        direct_db.execute(text("SET LOCAL deadlock_timeout = '100ms'"))
        direct_db.execute(text("SET LOCAL lock_timeout = '5s'"))
        direct_db.execute(text("SET LOCAL statement_timeout = '10s'"))
        locked_state = direct_db.scalar(
            select(MaintenanceProjectWorkbookState)
            .where(
                MaintenanceProjectWorkbookState.project_id == project_id
            )
            .with_for_update()
        )
        assert locked_state is not None

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(load_locked_snapshot)
            assert state_lock_attempted.wait(timeout=10)

            # If manager workbook still held owner user before waiting for
            # state, PostgreSQL would now detect user→state / state→user.
            locked_user = direct_db.scalar(
                select(SysUser)
                .where(SysUser.id == manager_id)
                .with_for_update()
            )
            assert locked_user is not None
            direct_db.commit()
            snapshot = future.result(timeout=20)

    assert [row["project_id"] for row in snapshot["projects"]] == [
        project_id
    ]


def test_auto_assign_existing_project_multi_changes_single_bump(db):
    # 有效 XSDD：严格挂销售合同 owner 项目（名称不同也不能推翻 owner）
    project = _contract_owner_project(
        db,
        project_id="occ-auto-existing",
        code="OCC-AUTO-EX",
        name="OCC自动挂靠既有项目",
        xsdd="20260826-0001",
    )
    _user(db, "occ_auto_sales", salesperson_name="测试销售")
    # 合同创建本身已落 state 并 bump 一次；以此刻为基线验证 auto_assign 只 +1
    base_revision = _revision(db, project.project_id)
    _load_orders(
        db, project="OCC预交付-任意别名", n=2, sales_order="XSDD-20260826-0001"
    )

    result = sa.auto_assign_unassigned(
        db, operated_by="occ-admin", user_ctx=_admin_ctx()
    )
    db.commit()

    # 一次运行里同时发生：2 张单挂靠 + 销售回填 + 负责人回填 + 账号级指派，
    # 同一根事务 revision 恰好 +1
    assert result["assigned_orders"] == 2
    assert result["created_projects"] == 0
    assert result["sales_filled_projects"] == 1
    assert result["manager_filled_projects"] == 1
    assert result["assignments_created"] == 1
    assert _revision(db, project.project_id) == base_revision + 1
    db.refresh(project)
    assert project.version == 2
    owner_audit = db.scalar(
        select(MaintenanceProjectAuditLog).where(
            MaintenanceProjectAuditLog.project_id == project.project_id,
            MaintenanceProjectAuditLog.entity_type == "project",
            MaintenanceProjectAuditLog.action == "update",
        )
    )
    assert owner_audit is not None
    assert owner_audit.before_json["version"] == 1
    assert owner_audit.after_json["version"] == 2

    with pytest.raises(catalog.MaintenanceProjectCatalogConflict):
        catalog.update_project(
            db,
            project_id=project.project_id,
            version=1,
            updates={"project_manager_id": "过期负责人"},
            reason="验证旧版本不能覆盖自动回填",
            operated_by="occ-admin",
        )
    db.rollback()

    # 幂等重跑：无未归属单、字段已满 → 全部零增量，revision 不动
    again = sa.auto_assign_unassigned(
        db, operated_by="occ-admin", user_ctx=_admin_ctx()
    )
    db.commit()
    assert again["assigned_orders"] == 0
    assert again["sales_filled_projects"] == 0
    assert again["manager_filled_projects"] == 0
    assert again["assignments_created"] == 0
    assert _revision(db, project.project_id) == base_revision + 1


def test_backfill_noop_keeps_revision_zero(db):
    project = MaintenanceProject(
        project_id="occ-backfill-noop", project_code="OCC-BF-NOOP",
        display_name="OCC回填零写项目", lifecycle_status="ongoing",
        salesperson="台账销售", project_manager_id="手工负责人",
    )
    db.add(project)
    db.commit()
    orders = _load_orders(db, project="OCC回填零写项目", n=1)
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id="occ-bf-noop-as", source_order_id=orders[0].raw_order_id,
        project_id=project.project_id, is_active=True, version=1,
        created_by="tester"))
    operations.lock_workbook_states(db, project_ids=[project.project_id])
    db.commit()
    assert _revision(db, project.project_id) == 0

    stats = sa.backfill_owner_fields(db, operated_by="occ-admin")
    db.commit()

    assert stats == {"sales_filled_projects": 0, "manager_filled_projects": 0,
                     "assignments_created": 0}
    assert _revision(db, project.project_id) == 0


def test_auto_assign_skips_missing_or_invalid_xsdd_without_creating(db):
    """无/非法 XSDD 的 WBDD：只跳过保持待处理，绝不按名称匹配既有项目，
    也绝不新建 AUTO 项目——即使项目名与既有项目完全一致。"""
    existing = MaintenanceProject(
        project_id="occ-name-only-existing", project_code="OCC-NAME-ONLY",
        display_name="OCC同名已有项目", lifecycle_status="ongoing",
    )
    db.add(existing)
    db.commit()
    no_xsdd = _load_orders(db, project="OCC同名已有项目", n=2)
    invalid_xsdd = _load_orders(
        db, project="OCC非法单号项目", n=1, sales_order="XSDD-非法单号"
    )

    result = sa.auto_assign_unassigned(
        db, operated_by="occ-admin", user_ctx=_admin_ctx()
    )
    db.commit()

    # 不建项、不挂靠：全部 3 张单保持未归属，没有任何新项目出现
    assert result["assigned_orders"] == 0
    assert result["created_projects"] == 0
    assert result["matched_projects"] == 0
    assert result["skipped_groups"] == 2
    assert db.scalar(
        select(MaintenanceSourceOrderAssignment.assignment_id).where(
            MaintenanceSourceOrderAssignment.source_order_id.in_(
                [order.raw_order_id for order in [*no_xsdd, *invalid_xsdd]]
            ),
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
    ) is None
    assert db.scalars(
        select(MaintenanceProject).where(
            MaintenanceProject.project_id != existing.project_id
        )
    ).first() is None
    # 零写：同名项目没有被挂靠改变，revision 不动
    assert _revision(db, existing.project_id) == 0


def test_auto_assign_fails_closed_when_assignment_appears_concurrently(
    db, monkeypatch
):
    """规划（只读）之后、锁之前归属被并发写入 → fail closed，零半截写入。"""
    project = _contract_owner_project(
        db,
        project_id="occ-race",
        code="OCC-RACE",
        name="OCC并发归属项目",
        xsdd="20260826-0002",
    )
    orders = _load_orders(
        db, project="OCC并发归属项目", n=1, sales_order="XSDD-20260826-0002"
    )
    raw_id = orders[0].raw_order_id
    # 合同建立 owner 已 bump 一次；fail closed 要求 auto_assign 自身零增量
    base_revision = _revision(db, project.project_id)

    real_lock = operations.lock_workbook_states
    injected = {"done": False}

    def racy_lock(session, *, project_ids):
        if not injected["done"]:
            injected["done"] = True
            # 全局数据锁已把真实第二连接序列化；在同一事务中注入锁后状态
            # 漂移，验证后续复核仍会 fail closed，且回滚不留半截写入。
            session.add(MaintenanceSourceOrderAssignment(
                assignment_id="occ-race-injected",
                source_order_id=raw_id,
                project_id=project.project_id,
                is_active=True, version=1, created_by="concurrent",
            ))
            session.flush()
        return real_lock(session, project_ids=project_ids)

    monkeypatch.setattr(operations, "lock_workbook_states", racy_lock)

    with pytest.raises(sa.SourceAssignmentConflict):
        sa.auto_assign_unassigned(
            db, operated_by="occ-admin", user_ctx=_admin_ctx()
        )
    db.rollback()

    # 零半截写入：本事务没留下任何指派/审计/项目，revision 不变
    assert db.scalar(
        select(MaintenanceProjectAuditLog.id).where(
            MaintenanceProjectAuditLog.operated_by == "occ-admin"
        )
    ) is None
    assert db.scalar(
        select(MaintenanceProject).where(
            MaintenanceProject.project_code.like("AUTO-%")
        )
    ) is None
    assert _revision(db, project.project_id) == base_revision
    # 注入的状态漂移与本事务一起回滚，不留下任何活动归属。
    assert db.scalar(
        select(MaintenanceSourceOrderAssignment).where(
            MaintenanceSourceOrderAssignment.source_order_id == raw_id,
            MaintenanceSourceOrderAssignment.is_active.is_(True),
        )
    ) is None


def test_backfill_prelocked_states_must_cover_candidates(db):
    """内部调用传 prelocked states 但未覆盖候选 → fail closed，不得晚锁。"""
    project = MaintenanceProject(
        project_id="occ-prelocked", project_code="OCC-PRELOCK",
        display_name="OCC预锁护栏项目", lifecycle_status="ongoing",
    )
    db.add(project)
    db.commit()
    orders = _load_orders(db, project="OCC预锁护栏项目", n=1)
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id="occ-prelocked-as", source_order_id=orders[0].raw_order_id,
        project_id=project.project_id, is_active=True, version=1,
        created_by="tester"))
    db.commit()

    with pytest.raises(sa.SourceAssignmentConflict):
        sa.backfill_owner_fields(
            db, operated_by="occ-admin", _prelocked_states={}
        )
    db.rollback()

    db.refresh(project)
    assert project.salesperson is None
    assert project.project_manager_id is None
    assert db.scalar(
        select(MaintenanceProjectUserAssignment).where(
            MaintenanceProjectUserAssignment.project_id == project.project_id
        )
    ) is None
    assert _state(db, project.project_id) is None
