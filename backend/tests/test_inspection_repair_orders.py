"""The candidate UI and both repair write paths share return-receipt capacity."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.system import SysUser
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_return_metrics import _receipt


BASE = "/api/inspection-repair"


def _user(db, name, capability, *, role="readonly"):
    return client_for(db, username=name, role=role, overrides={
        capability: True, "page_maintenance": True,
    })


def _command(client, endpoint, role, payload, key=None):
    return client.post(
        f"{BASE}{endpoint}",
        params={"role": role},
        json=payload,
        headers={"Idempotency-Key": key or uuid4().hex},
    )


def _candidate_response(supervisor, query):
    response = supervisor.get(f"{BASE}/return-candidates", params={"role": "supervisor", "q": query})
    assert response.status_code == 200, response.text
    return response.json()


def _create_payload(receipt, qty, assignee):
    return {
        "mode": "external",
        "assignee": assignee,
        "lines": [{
            "pn": "SERVER-DECIDES",
            "description": "SERVER-DECIDES",
            "qty": qty,
            "sourceReceiptId": receipt.rkd_line_id,
            "sourceVersion": receipt.version,
        }],
    }


def _old_result(client, part_id, qty):
    return client.post(
        "/api/cycle/repair-records",
        json={"part_id": part_id, "fixed_qty": 0, "scrap_qty": qty},
        headers={"Idempotency-Key": uuid4().hex},
    )


def test_real_return_candidates_and_two_ledgers_share_remaining_capacity(db):
    project = _project(db, "REPAIR-CANDIDATE")
    part = _part(db, "REPAIR-PN", "主数据描述")
    bad = _receipt(db, project, part, "5", condition="坏件")
    _receipt(db, project, part, "12", condition="成品")
    _receipt(db, project, part, "1.5", condition="坏品")
    review = _receipt(db, project, part, "11", condition="故障", occurred_at=None)
    review.review_required = True
    # Use a second PN to verify backward compatibility for line shapes not
    # exposed by the new selector.
    other_part = _part(db, "REVIEW-PN")
    review.part_id = other_part.id
    db.commit()
    with (
        _user(db, "repair-supervisor", "page_repair_order_supervisor", role="admin") as supervisor,
        _user(db, "repair-tech", "page_repair_order_technician") as technician,
        _user(db, "repair-old", "page_cycle_repair") as old_repairer,
        _user(db, "repair-unscoped", "page_repair_order_supervisor") as unscoped,
    ):
        assert _candidate_response(unscoped, "REPAIR-PN") == {"items": [], "total": 0}
        assert _candidate_response(supervisor, "REVIEW-PN") == {"items": [], "total": 0}
        response = technician.get(f"{BASE}/return-candidates", params={"role": "supervisor"})
        assert response.status_code == 403
        initial = _candidate_response(supervisor, "REPAIR-PN")
        assert initial["total"] == 1
        assert initial["items"][0]["receiptId"] == bad.rkd_line_id
        assert initial["items"][0]["availableQty"] == 5
        assert set(initial["items"][0]) == {
            "receiptId", "version", "headNo", "occurredAt", "pn",
            "description", "condition", "availableQty",
        }
        assert _old_result(old_repairer, part.id, 2).status_code == 201
        assert _candidate_response(supervisor, "REPAIR-PN")["items"][0]["availableQty"] == 3

        data = _create_payload(bad, 3, "repair-tech")
        key = uuid4().hex
        created = _command(supervisor, "/orders", "supervisor", data, key)
        assert created.status_code == 201, created.text
        order = created.json()
        assert order["lines"][0]["pn"] == "REPAIR-PN"
        assert order["lines"][0]["sourceHeadNo"] == bad.head_no
        replay = _command(supervisor, "/orders", "supervisor", data, key)
        assert replay.status_code == 201 and replay.json()["id"] == order["id"]
        assert _candidate_response(supervisor, "REPAIR-PN")["total"] == 0
        assert _old_result(old_repairer, part.id, 1).status_code == 400

        db.expire_all()
        assert db.scalar(
            text("SELECT pending_qty FROM v_repair_pool WHERE part_id = :part"),
            {"part": part.id},
        ) == 0
        with pytest.raises(IntegrityError):
            db.execute(
                text("UPDATE maintenance_rkd_return_line SET qty = 4 WHERE rkd_line_id = :receipt"),
                {"receipt": bad.rkd_line_id},
            )
            db.commit()
        db.rollback()
        assert db.get(type(bad), bad.rkd_line_id).qty == 5


def test_competing_legacy_and_order_paths_cannot_take_same_return(db):
    project = _project(db, "REPAIR-RACE")
    part = _part(db, "REPAIR-RACE-PN")
    bad = _receipt(db, project, part, "2")
    db.commit()
    with (
        _user(db, "repair-race-supervisor", "page_repair_order_supervisor", role="admin") as supervisor,
        _user(db, "repair-race-tech", "page_repair_order_technician"),
        _user(db, "repair-race-old", "page_cycle_repair") as old_repairer,
    ):
        body = _create_payload(bad, 2, "repair-race-tech")
        part_id = part.id
        with ThreadPoolExecutor(max_workers=2) as pool:
            order_call = pool.submit(_command, supervisor, "/orders", "supervisor", body)
            old_call = pool.submit(_old_result, old_repairer, part_id, 2)
            responses = [order_call.result(), old_call.result()]
        assert [r.status_code for r in responses].count(201) == 1
        assert all(r.status_code in {201, 400, 409} for r in responses)
        assert _candidate_response(supervisor, "REPAIR-RACE-PN")["total"] == 0


def test_real_assignee_can_return_and_supervisor_can_correct_and_redispatch(db):
    with (
        _user(db, "actual-sales", "page_repair_order_sales") as sales,
        _user(db, "actual-supervisor", "page_repair_order_supervisor", role="admin") as supervisor,
        _user(db, "actual-tech", "page_repair_order_technician") as tech,
        _user(db, "second-tech", "page_repair_order_technician") as second_tech,
        _user(db, "actual-admin", "page_repair_order_disposition") as admin,
    ):
        created = _command(sales, "/orders", "sales", {
            "mode": "external", "lines": [{"pn": "REAL-PN", "qty": 4}],
        })
        assert created.status_code == 201, created.text
        order = created.json()
        route = f"/orders/{order['id']}/actions"
        claimed = _command(tech, route, "technician", {"type": "claim", "version": order["version"]})
        assert claimed.status_code == 200, claimed.text
        version = claimed.json()["version"]
        blocked = _command(second_tech, route, "technician", {
            "type": "result", "version": version, "line": 0, "fixed": 1, "unfixed": 0,
        })
        assert blocked.status_code in {403, 404}
        result = _command(tech, route, "technician", {
            "type": "result", "version": version, "line": 0, "fixed": 1, "unfixed": 0,
        })
        assert result.status_code == 200, result.text
        returned = _command(tech, route, "technician", {
            "type": "return", "version": result.json()["version"],
        })
        assert returned.status_code == 200, returned.text
        order = returned.json()
        assert order["lines"][0]["fixed"] == 1 and order["returned"]
        stale = _command(supervisor, route, "supervisor", {
            "type": "assign", "version": version, "assignee": "second-tech",
        })
        assert stale.status_code == 409
        corrected = _command(supervisor, route, "supervisor", {
            "type": "edit", "version": order["version"], "mode": "internal",
            "assignee": "second-tech", "lines": [{"pn": "REAL-PN", "qty": 3, "fixed": 1, "unfixed": 1}],
        })
        assert corrected.status_code == 200, corrected.text
        order = corrected.json()
        assert order["assignee"] == "second-tech" and not order["returned"]
        assert order["lines"][0]["qty"] == 3 and order["lines"][0]["unfixed"] == 1
        assert {event["name"] for event in order["history"]} >= {"actual-sales", "actual-tech", "second-tech"}
        assert db.scalar(select(SysUser.id).where(SysUser.username == order["assignee"])) is not None
        pending = admin.get(f"{BASE}/orders", params={"role": "admin"})
        assert pending.status_code == 200
        assert [item["id"] for item in pending.json()] == [order["id"]]


def test_export_only_fixed_quantities_with_recorded_destinations(db):
    from io import BytesIO

    from openpyxl import load_workbook

    with (
        _user(db, "export-supervisor", "page_repair_order_supervisor") as supervisor,
        _user(db, "export-tech", "page_repair_order_technician") as tech,
        _user(db, "export-admin", "page_repair_order_disposition") as admin,
    ):
        created = _command(supervisor, "/orders", "supervisor", {
            "mode": "internal", "assignee": "export-tech",
            "lines": [{"pn": "00123", "description": "{=1+1}", "qty": 7}],
        })
        assert created.status_code == 201, created.text
        order = created.json()
        order_id = order["id"]
        route = f"/orders/{order_id}/actions"
        result = _command(tech, route, "technician", {
            "type": "result", "version": order["version"], "line": 0, "fixed": 5, "unfixed": 2,
        })
        assert result.status_code == 200, result.text
        version = result.json()["version"]
        for kind, repair_result, qty in (
            ("shop", "fixed", 2), ("company", "fixed", 1), ("shop", "unfixed", 1),
        ):
            response = _command(admin, route, "admin", {
                "type": "dispose", "version": version, "line": 0,
                "result": repair_result, "kind": kind, "qty": qty,
            })
            assert response.status_code == 200, response.text
            version = response.json()["version"]

        denied = admin.get(f"{BASE}/orders/export", params={"role": "admin"})
        assert denied.status_code == 403
        forged = tech.get(f"{BASE}/orders/export", params={"role": "supervisor"})
        assert forged.status_code == 403

        def workbook_rows():
            exported = supervisor.get(f"{BASE}/orders/export", params={"role": "supervisor"})
            assert exported.status_code == 200, exported.text[:120]
            assert exported.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument")
            assert exported.headers["cache-control"] == "no-store"
            workbook = load_workbook(BytesIO(exported.content), read_only=True)
            try:
                worksheet = workbook.active
                data = list(worksheet.values)
                assert worksheet["D2"].data_type == "s"
                return data
            finally:
                workbook.close()

        rows = workbook_rows()
        assert rows[0] == ("维修单号", "任务条", "PN", "描述", "去向", "数量", "标签")
        assert rows[1:] == [
            (order_id, 1, "00123", "{=1+1}", "电商", 2, "修好 · 已登记去向"),
            (order_id, 1, "00123", "{=1+1}", "主公司", 1, "修好 · 已登记去向"),
        ]

        corrected = _command(supervisor, route, "supervisor", {
            "type": "edit", "version": version, "mode": "internal", "assignee": "export-tech",
            "lines": [{
                "pn": "00123", "description": "{=1+1}", "qty": 7, "fixed": 5, "unfixed": 2,
                "dispositions": [
                    {"kind": "shop", "result": "fixed", "qty": 0},
                    {"kind": "company", "result": "fixed", "qty": 1},
                    {"kind": "shop", "result": "unfixed", "qty": 1},
                ],
            }],
        })
        assert corrected.status_code == 200, corrected.text
        assert workbook_rows()[1:] == [(order_id, 1, "00123", "{=1+1}", "主公司", 1, "修好 · 已登记去向")]


def test_increasing_a_source_reservation_requires_the_current_version(db):
    project = _project(db, "REPAIR-SOURCE-VERSION")
    part = _part(db, "REPAIR-SOURCE-VERSION-PN")
    receipt = _receipt(db, project, part, "5")
    db.commit()

    with (
        _user(db, "source-version-supervisor", "page_repair_order_supervisor", role="admin") as supervisor,
        _user(db, "source-version-tech", "page_repair_order_technician"),
    ):
        created = _command(
            supervisor, "/orders", "supervisor",
            _create_payload(receipt, 2, "source-version-tech"),
        )
        assert created.status_code == 201, created.text
        order = created.json()
        endpoint = f"/orders/{order['id']}/actions"
        line = {
            "pn": part.pn_std,
            "qty": 3,
            "fixed": 0,
            "unfixed": 0,
            "sourceReceiptId": receipt.rkd_line_id,
        }
        payload = {
            "type": "edit", "version": order["version"], "mode": "external",
            "assignee": "source-version-tech",
        }
        missing_version = _command(supervisor, endpoint, "supervisor", {**payload, "lines": [line]})
        assert missing_version.status_code == 409, missing_version.text
        assert "版本" in missing_version.json()["detail"]

        old_version = receipt.version
        receipt.version += 1
        db.commit()
        stale = _command(supervisor, endpoint, "supervisor", {
            **payload, "lines": [{**line, "sourceVersion": old_version}],
        })
        assert stale.status_code == 409, stale.text
        assert _candidate_response(supervisor, part.pn_std)["items"][0]["availableQty"] == 3

        saved = _command(supervisor, endpoint, "supervisor", {
            **payload, "lines": [{**line, "sourceVersion": receipt.version}],
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["lines"][0]["qty"] == 3
        assert _candidate_response(supervisor, part.pn_std)["items"][0]["availableQty"] == 2
