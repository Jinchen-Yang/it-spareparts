"""The candidate UI and both repair write paths share return-receipt capacity."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.maintenance_project import MaintenanceProjectUserAssignment
from app.models.system import SysUser
from app.services import repair_order_sources
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


def test_sales_can_only_read_their_own_repair_orders(db):
    with (
        _user(db, "ownership-sales-a", "page_repair_order_sales", role="sales") as first,
        _user(db, "ownership-sales-b", "page_repair_order_sales", role="sales") as second,
    ):
        data = {"mode": "internal", "lines": [{"pn": "TEST-REPAIR-PN", "qty": 1}]}
        key = uuid4().hex
        a = _command(first, "/orders", "sales", data, key)
        b = _command(second, "/orders", "sales", data, key)
        assert a.status_code == b.status_code == 201
        a_id, b_id = a.json()["id"], b.json()["id"]
        assert a_id != b_id

        for client, own, other in ((first, a_id, b_id), (second, b_id, a_id)):
            session = client.get(f"{BASE}/session")
            assert session.status_code == 200 and session.json()["roles"] == ["sales"]
            listing = client.get(f"{BASE}/orders", params={"role": "sales"})
            assert listing.status_code == 200
            assert [order["id"] for order in listing.json()] == [own]
            for role in ("supervisor", "technician", "admin"):
                assert client.get(f"{BASE}/orders", params={"role": role}).status_code == 403
            action = _command(client, f"/orders/{other}/actions", "sales", {
                "type": "claim", "version": 1,
            })
            assert action.status_code == 404
            replay = _command(client, "/orders", "sales", data, key)
            assert replay.status_code == 201 and replay.json()["id"] == own


def test_sales_repair_relationships_hide_other_creators_order_numbers(db):
    with (
        _user(db, "relationship-sales-a", "page_repair_order_sales", role="sales") as first,
        _user(db, "relationship-sales-b", "page_repair_order_sales", role="sales") as second,
        _user(db, "relationship-tech", "page_repair_order_technician") as technician,
        _user(db, "relationship-dispose", "page_repair_order_disposition") as disposer,
        _user(db, "relationship-manager", "page_repair_order_supervisor") as supervisor,
    ):
        created = _command(first, "/orders", "sales", {
            "mode": "internal", "assignee": "relationship-tech",
            "lines": [{"pn": "RETRY-EXAMPLE", "qty": 1}],
        })
        assert created.status_code == 201, created.text
        parent = created.json()
        parent_route = f"/orders/{parent['id']}/actions"
        result = _command(technician, parent_route, "technician", {
            "type": "result", "version": parent["version"], "line": 0, "unfixed": 1, "fixed": 0,
        })
        assert result.status_code == 200, result.text
        assigned = _command(disposer, parent_route, "admin", {
            "type": "dispose", "version": result.json()["version"], "line": 0,
            "result": "unfixed", "kind": "retry", "qty": 1,
        })
        assert assigned.status_code == 200, assigned.text

        def listed(client, role="sales"):
            response = client.get(f"{BASE}/orders", params={"role": role})
            assert response.status_code == 200, response.text
            return {item["id"]: item for item in response.json()}

        own = listed(first)
        retry = next(order for order in own.values() if order.get("repairSource"))
        retry_id = retry["id"]
        assert retry["repairSource"]["orderId"] == parent["id"]
        assert own[parent["id"]]["lines"][0]["dispositions"][0]["retryOrderId"] == retry_id

        changed = _command(supervisor, f"/orders/{retry_id}/actions", "supervisor", {
            "type": "edit", "version": retry["version"], "mode": "internal",
            "creator": "relationship-sales-b", "note": retry["note"],
            "lines": [{"pn": "RETRY-EXAMPLE", "qty": 1, "fixed": 0, "unfixed": 0}],
        })
        assert changed.status_code == 200, changed.text
        left, right = listed(first), listed(second)
        assert set(left) == {parent["id"]}
        assert set(right) == {retry_id}
        assert retry_id not in json.dumps(left)
        assert parent["id"] not in json.dumps(right)
        assert left[parent["id"]]["lines"][0]["dispositions"] == [
            {"kind": "retry", "result": "unfixed", "qty": 1},
        ]

        both = listed(supervisor, "supervisor")
        assert both[parent["id"]]["lines"][0]["dispositions"][0]["retryOrderId"] == retry_id
        assert both[retry_id]["repairSource"]["orderId"] == parent["id"]


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


@pytest.mark.parametrize("source_access", ["no-page", "out-of-scope", "merged-pn"])
def test_supervisor_can_release_an_existing_reservation_without_new_source_access(db, source_access):
    project = _project(db, "REPAIR-SOURCE-RELEASE")
    part = _part(db, "REPAIR-SOURCE-RELEASE-PN")
    receipt = _receipt(db, project, part, "5")
    db.commit()
    receipt_id = receipt.rkd_line_id
    read_source = source_access == "merged-pn"

    with (
        _user(db, "release-creator", "page_repair_order_supervisor", role="admin") as creator,
        _user(db, "release-tech", "page_repair_order_technician") as technician,
        client_for(db, username="release-editor", role="admin" if read_source else "readonly", overrides={
            "page_repair_order_supervisor": True,
            "page_maintenance": source_access != "no-page",
        }) as editor,
    ):
        create = _create_payload(receipt, 3, "release-tech")
        create["lines"].append({"pn": "MANUAL-PN", "qty": 1})
        created = _command(creator, "/orders", "supervisor", create)
        assert created.status_code == 201, created.text
        order = created.json()
        endpoint = f"/orders/{order['id']}/actions"
        if read_source:
            canonical_part = _part(db, "REPAIR-SOURCE-RELEASE-CANONICAL-PN")
            part.status = "merged"
            part.merged_into_id = canonical_part.id
            db.commit()

        def order_reservation():
            return db.scalar(text(
                "SELECT COALESCE(SUM(qty), 0) FROM maintenance_repair_order_line "
                "WHERE source_receipt_id = :receipt_id"
            ), {"receipt_id": receipt_id})

        def assert_source_hidden(line):
            if not read_source:
                assert not {"sourceReceiptId", "sourceHeadNo", "sourceVersion", "sourceAvailableQty"} & line.keys()

        visible = editor.get(f"{BASE}/orders", params={"role": "supervisor"}).json()[0]
        assert_source_hidden(visible["lines"][0])
        line = {"pn": part.pn_std, "qty": 2, "fixed": 0, "unfixed": 0}
        manual = {"pn": "MANUAL-PN", "qty": 1, "fixed": 0, "unfixed": 0}
        payload = {
            "type": "edit", "version": order["version"], "mode": "external",
            "assignee": "release-tech", "lines": [line, manual], "sourceIndices": [0, 1],
        }
        assert _command(technician, endpoint, "technician", payload).status_code == 403
        assert order_reservation() == 3
        decreased = _command(editor, endpoint, "supervisor", payload, "decrease-existing")
        assert decreased.status_code == 200, decreased.text
        assert decreased.json()["lines"][0]["qty"] == 2
        assert_source_hidden(decreased.json()["lines"][0])
        assert order_reservation() == 2
        replay = _command(editor, endpoint, "supervisor", payload, "decrease-existing")
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert_source_hidden(replay.json()["lines"][0])
        current_version = decreased.json()["version"]

        increased = _command(editor, endpoint, "supervisor", {
            **payload, "version": current_version,
            "lines": [{**line, "qty": 4, "sourceVersion": receipt.version}, manual],
        })
        assert increased.status_code == (409 if read_source else 403), increased.text
        assert order_reservation() == 2

        removed = _command(editor, endpoint, "supervisor", {
            **payload, "version": current_version, "lines": [manual], "sourceIndices": [1],
        })
        assert removed.status_code == 200, removed.text
        assert len(removed.json()["lines"]) == 1
        assert order_reservation() == 0


def test_mixed_source_changes_authorize_each_increase_and_commit_together(db):
    hidden_project = _project(db, "REPAIR-MIXED-HIDDEN")
    scoped_project = _project(db, "REPAIR-MIXED-VISIBLE")
    part = _part(db, "REPAIR-MIXED-PN")
    hidden = _receipt(db, hidden_project, part, "5")
    scoped = _receipt(db, scoped_project, part, "5")
    db.commit()

    with (
        _user(db, "mixed-creator", "page_repair_order_supervisor", role="admin") as creator,
        _user(db, "mixed-tech", "page_repair_order_technician"),
        _user(db, "mixed-editor", "page_repair_order_supervisor") as editor,
    ):
        account = db.scalar(select(SysUser).where(SysUser.username == "mixed-editor"))
        scope_grant = MaintenanceProjectUserAssignment(
            assignment_id=str(uuid4()), project_id=scoped_project.project_id,
            responsibility_type="viewer", user_id=account.id,
            assigned_by="test", assignment_reason="查看此项目的返还",
        )
        db.add(scope_grant)
        db.commit()

        create = _create_payload(hidden, 3, "mixed-tech")
        create["lines"] += _create_payload(scoped, 2, "mixed-tech")["lines"]
        created = _command(creator, "/orders", "supervisor", create)
        assert created.status_code == 201, created.text
        order = created.json()
        endpoint = f"/orders/{order['id']}/actions"
        line = {"pn": part.pn_std, "qty": 1, "fixed": 0, "unfixed": 0}
        payload = {
            "type": "edit", "version": order["version"], "mode": "external",
            "assignee": "mixed-tech", "sourceIndices": [0, 1],
        }

        def current_order():
            return editor.get(f"{BASE}/orders", params={"role": "supervisor"}).json()[0]

        forbidden = _command(editor, endpoint, "supervisor", {
            **payload, "lines": [{**line, "qty": 4, "sourceVersion": hidden.version}, line],
        })
        assert forbidden.status_code == 403, forbidden.text
        assert [row["qty"] for row in current_order()["lines"]] == [3, 2]

        stale = _command(editor, endpoint, "supervisor", {
            **payload, "lines": [line, {**line, "qty": 3, "sourceVersion": scoped.version + 1}],
        })
        assert stale.status_code == 409, stale.text
        assert [row["qty"] for row in current_order()["lines"]] == [3, 2]

        accepted = {
            **payload, "lines": [line, {**line, "qty": 3, "sourceVersion": scoped.version}],
        }
        saved = _command(editor, endpoint, "supervisor", accepted, "mixed-source-edit")
        assert saved.status_code == 200, saved.text
        first, second = saved.json()["lines"]
        assert first["qty"] == 1 and "sourceReceiptId" not in first
        assert second["qty"] == 3 and second["sourceReceiptId"] == scoped.rkd_line_id
        scope_grant.archived_at = datetime.now(UTC)
        scope_grant.archived_by = "test"
        scope_grant.archive_reason = "撤销项目查看权限"
        scope_grant.version += 1
        db.commit()
        replay = _command(editor, endpoint, "supervisor", accepted, "mixed-source-edit")
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert all("sourceReceiptId" not in line for line in replay.json()["lines"])


def test_source_reservation_restores_the_write_lock_timeout(db, monkeypatch):
    project = _project(db, "REPAIR-LOCK-TIMEOUT")
    part = _part(db, "REPAIR-LOCK-TIMEOUT-PN")
    receipt = _receipt(db, project, part, "5")
    db.commit()

    lock_projects = repair_order_sources.lock_receipt_projects

    def check_timeout(session, project_ids):
        assert session.scalar(text("SHOW lock_timeout")) == "5s"
        return lock_projects(session, project_ids)

    monkeypatch.setattr(repair_order_sources, "lock_receipt_projects", check_timeout)
    with (
        _user(db, "lock-timeout-supervisor", "page_repair_order_supervisor", role="admin") as supervisor,
        _user(db, "lock-timeout-tech", "page_repair_order_technician"),
    ):
        payload = _create_payload(receipt, 2, "lock-timeout-tech")
        created = _command(supervisor, "/orders", "supervisor", payload)
        assert created.status_code == 201, created.text
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {
            "key": f"repair-part:{part.id}",
        })
        blocked = _command(supervisor, "/orders", "supervisor", payload, "blocked-source")
        assert blocked.status_code == 409, blocked.text
        db.rollback()
        retried = _command(supervisor, "/orders", "supervisor", payload, "blocked-source")
        assert retried.status_code == 201, retried.text
