"""人工登记领用的「关联需求单」候选端点测试（2026-10-12 按 PN 建议口径）。

覆盖：项目/备件过滤（跨项目排除）、customer_info 脱敏双路
（有 data_customer 可见 / 无权限三字段 null + restricted）、
未生效/墓碑/assignment 失活排除、sys_access_log 审计落库。
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models.dimensions import DimPart
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    MaintenanceDemandDeleteIntent,
    MaintenanceDemandTombstone,
)
from app.models.maintenance_source_assignment import (
    MaintenanceSourceOrderAssignment,
)
from app.models.system import SysAccessLog, SysImportBatch
from tests.boss_board_helpers import assign, client_for, make_project

CONTACT = {
    "receiver_address": "合成市领用建议路 8 号",
    "receiver": "合成领用联系人",
    "receiver_phone": "010-87654321",
}


def _order(
    db, project, *, order_no: str, contacts: dict | None = None,
    data_status: str = "已生效", assign_it: bool = True,
) -> FMaintenanceOrder:
    batch = SysImportBatch(
        filename="t.xlsx", file_type="maintenance",
        file_hash=uuid4().hex, status="success",
    )
    db.add(batch)
    db.flush()
    order = FMaintenanceOrder(
        raw_order_id=f"raw-{uuid4()}",
        order_no=order_no,
        order_date=None,
        project_raw=project.display_name,
        data_status=data_status,
        import_batch_id=batch.id,
        **(contacts or {}),
    )
    db.add(order)
    db.flush()
    if assign_it:
        assign(db, order, project)
    else:
        db.commit()
    return order


def _line(db, order: FMaintenanceOrder, part: DimPart, *, is_active: bool = True):
    row = FMaintenanceLine(
        raw_line_id=f"line-{uuid4()}",
        order_id=order.id,
        part_id=part.id,
        import_batch_id=order.import_batch_id,
        is_active=is_active,
    )
    db.add(row)
    db.commit()
    return row


def _tombstone(db, order: FMaintenanceOrder) -> None:
    """单独立墓碑（保持 data_status=已生效、挂靠 active），隔离墓碑口径。"""
    now = datetime.now(timezone.utc)
    intent = MaintenanceDemandDeleteIntent(
        intent_id=str(uuid4()),
        idempotency_key=f"idem-{uuid4()}",
        request_digest="0" * 64,
        selection_digest="0" * 64,
        status="executed",
        reason="合成墓碑",
        operated_by="tester",
        header_count=1,
        line_count=0,
        created_at=now,
        expires_at=now + timedelta(days=1),
        executed_at=now,
    )
    db.add(intent)
    db.flush()
    db.add(MaintenanceDemandTombstone(
        source_order_id=order.raw_order_id,
        delete_intent_id=intent.intent_id,
        version_digest="0" * 64,
        deleted_by="tester",
        delete_reason="合成墓碑",
        deleted_at=now,
    ))
    db.commit()


def _get(client, project_id: str, **params):
    return client.get(
        f"/api/maintenance/site-issues/projects/{project_id}/demand-candidates",
        params=params,
    )


def test_scoped_to_project_and_part_with_visible_contact(db):
    own, other = make_project(db, "领用建议本项目"), make_project(db, "领用建议他项目")
    part = DimPart(pn_std="PN-LY-SUGGEST-1", description="领用建议备件")
    db.add(part)
    db.commit()
    hit = _order(db, own, order_no="WBDD-LY-HIT-0001", contacts=CONTACT)
    miss = _order(db, own, order_no="WBDD-LY-MISS-0002", contacts=CONTACT)
    cross = _order(db, other, order_no="WBDD-LY-CROSS-0003", contacts=CONTACT)
    _line(db, hit, part)
    _line(db, cross, part)
    client = client_for(
        db,
        username="ly-suggest-visible",
        role="boss",
        overrides={
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "data_customer": True,
        },
    )
    resp = _get(client, own.project_id, part_id=part.id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 只见本项目、且含该备件活跃行的单（跨项目与无 PN 行的均排除）
    assert [row["source_order_id"] for row in body["rows"]] == [hit.raw_order_id]
    assert body["total"] == 1
    assert (body["page"], body["page_size"]) == (1, 50)
    row = body["rows"][0]
    assert row["order_no"] == hit.order_no
    # 有 data_customer → 地址三字段可见（领用地址建议的数据源）
    assert row["contact_info_state"] == "visible"
    assert {key: row[key] for key in CONTACT} == CONTACT
    assert miss.raw_order_id not in resp.text
    # 不传 part_id 回归全量本项目活跃单
    plain = _get(client, own.project_id)
    assert {r["source_order_id"] for r in plain.json()["rows"]} == {
        hit.raw_order_id, miss.raw_order_id,
    }


def test_contact_restricted_without_data_customer(db):
    project = make_project(db, "领用建议受限项目")
    part = DimPart(pn_std="PN-LY-RESTRICT-1", description="受限建议备件")
    db.add(part)
    db.commit()
    order = _order(db, project, order_no="WBDD-LY-RESTRICT-0004", contacts=CONTACT)
    _line(db, order, part)
    client = client_for(
        db,
        username="ly-suggest-restricted",
        role="boss",
        overrides={
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "data_customer": False,
        },
    )
    resp = _get(client, project.project_id, part_id=part.id)
    assert resp.status_code == 200, resp.text
    row = resp.json()["rows"][0]
    # 无客户信息权限：fail-closed，三字段 null + restricted，不泄露原文
    assert row["contact_info_state"] == "restricted"
    assert (row["receiver_address"], row["receiver"], row["receiver_phone"]) == (
        None, None, None,
    )
    for value in CONTACT.values():
        assert value not in resp.text


def test_excludes_voided_tombstoned_and_unassigned(db):
    project = make_project(db, "领用建议排除项目")
    part = DimPart(pn_std="PN-LY-EXCL-1", description="排除建议备件")
    db.add(part)
    db.commit()
    healthy = _order(db, project, order_no="WBDD-LY-OK-0005", contacts=CONTACT)
    voided = _order(db, project, order_no="WBDD-LY-VOID-0006", contacts=CONTACT,
                    data_status="未生效")
    tombstoned = _order(db, project, order_no="WBDD-LY-TOMB-0007", contacts=CONTACT)
    unassigned = _order(db, project, order_no="WBDD-LY-UNASSIGN-0008", contacts=CONTACT,
                        assign_it=False)
    for order in (healthy, voided, tombstoned, unassigned):
        _line(db, order, part)
    _tombstone(db, tombstoned)
    # 挂靠失活：历史表有不可改触发器，按归档口径直接落一条失活挂靠
    now = datetime.now(timezone.utc)
    db.add(MaintenanceSourceOrderAssignment(
        assignment_id=str(uuid4()),
        source_order_id=unassigned.raw_order_id,
        project_id=project.project_id,
        is_active=False,
        version=1,
        created_by="tester",
        created_at=now,
        archived_by="tester",
        archived_at=now,
    ))
    db.commit()
    client = client_for(
        db,
        username="ly-suggest-exclusions",
        role="boss",
        overrides={
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "data_customer": True,
        },
    )
    resp = _get(client, project.project_id, part_id=part.id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["source_order_id"] for row in body["rows"]] == [healthy.raw_order_id]
    assert body["total"] == 1


def test_unknown_project_404_and_bad_part_id_422(db):
    client = client_for(
        db,
        username="ly-suggest-bad-args",
        role="boss",
        overrides={
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "data_customer": True,
        },
    )
    assert _get(client, f"no-such-project-{uuid4().hex[:8]}").status_code == 404
    assert _get(client, "no-such-project-2", part_id=0).status_code == 422


def test_access_log_written_with_filters(db):
    project = make_project(db, "领用建议审计项目")
    part = DimPart(pn_std="PN-LY-AUDIT-1", description="审计建议备件")
    db.add(part)
    db.commit()
    order = _order(db, project, order_no="WBDD-LY-AUDIT-0009", contacts=CONTACT)
    _line(db, order, part)
    client = client_for(
        db,
        username="ly-suggest-auditor",
        role="boss",
        overrides={
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "data_customer": True,
        },
    )
    resp = _get(client, project.project_id, part_id=part.id, q="AUDIT")
    assert resp.status_code == 200, resp.text
    logs = (
        db.query(SysAccessLog)
        .filter_by(action="site_issue_demand_candidates", resource="maintenance_project")
        .all()
    )
    assert len(logs) == 1
    detail = logs[0].detail
    assert detail["project_id"] == project.project_id
    assert detail["part_id"] == part.id
    assert detail["searched"] is True
    assert detail["total"] == 1
