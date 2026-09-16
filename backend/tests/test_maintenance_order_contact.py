"""Contact read contracts: per WBDD, customer permissions, imports and audit isolation."""

import json

import pytest
from sqlalchemy import select

from app.etl import pipeline
from app.models.maintenance import (
    FMaintenanceOrder,
    MaintenanceDemandDeleteIntentItem,
)
from app.models.system import SysUser
from app.security import UserContext
from app.services import maintenance_demands
from app.services.maintenance_order_contact import order_contact
from tests.boss_board_helpers import assign, client_for, make_project
from tests.test_maintenance_demand_project_scope import _assign_manager
from tests.wbdd_fixtures import COLUMNS_90, COLUMNS_91, make_rows, write_workbook


CONTACTS = [
    {
        "receiver_address": "合成市长地址路" * 60 + "\n1号楼",
        "receiver": "合成联系人甲",
        "receiver_phone": "010-00123456 转 007",
    },
    {
        "receiver_address": "合成乙市2号",
        "receiver": "合成联系人乙",
        "receiver_phone": "00123456789",
    },
    {"receiver_address": None, "receiver": None, "receiver_phone": None},
    {"receiver_address": None, "receiver": "合成联系人丁", "receiver_phone": None},
]
HEADERS = {
    "receiver_address": "收货地址",
    "receiver": "收货人",
    "receiver_phone": "收货人电话",
}


def _import(db, tmp_path, contacts=CONTACTS, columns=COLUMNS_91, name="contacts.xlsx"):
    rows = make_rows(orders=len(contacts), lines_per_order=2)
    for row in rows:
        index = int(row["数据ID(不可修改)"].removeprefix("SYN-O")) - 1
        row.update({HEADERS[key]: value for key, value in contacts[index].items()})
    path = write_workbook(str(tmp_path / name), columns, rows)
    batch = pipeline.run_import(db, path, name, uploaded_by="tester", mode="upsert")
    db.commit()
    assert batch.status == "success"
    db.expire_all()
    return list(
        db.scalars(select(FMaintenanceOrder).order_by(FMaintenanceOrder.raw_order_id))
    )


@pytest.fixture
def seeded(db, tmp_path):
    orders = _import(db, tmp_path)
    project = make_project(db)
    for order in orders:
        assign(db, order, project)
    return project, orders


def _read(client, endpoint, project_id):
    if endpoint == "search":
        response = client.post(
            "/api/maintenance/demands/search", json={"page": 1, "page_size": 20}
        )
        key = "items"
    else:
        response = client.get(
            f"/api/maintenance/boss-board/projects/{project_id}/orders"
        )
        key = "rows"
    assert response.status_code == 200, response.text
    return response, response.json()[key]


@pytest.mark.parametrize("endpoint", ["search", "project_orders"])
@pytest.mark.parametrize(
    "role,grant,visible",
    [
        ("boss", True, True),
        ("boss", False, False),
        # Real admin context uses the platform's effective all-on data permissions.
        ("admin", False, True),
    ],
)
def test_contact_read_permissions_and_distinct_orders(
    db, seeded, endpoint, role, grant, visible
):
    project, orders = seeded
    client = client_for(
        db,
        username="contact-reader",
        role=role,
        overrides={
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "data_customer": grant,
        },
    )
    response, rows = _read(client, endpoint, project.project_id)
    by_id = {row["source_order_id"]: row for row in rows}
    assert set(by_id) == {order.raw_order_id for order in orders}
    for order, expected in zip(orders, CONTACTS):
        row = by_id[order.raw_order_id]
        assert row["contact_info_state"] == ("visible" if visible else "restricted")
        assert {key: row[key] for key in HEADERS} == (
            expected if visible else dict.fromkeys(HEADERS)
        )
    if not visible:
        for contact in CONTACTS:
            for value in contact.values():
                if value:
                    assert value not in response.text


@pytest.mark.parametrize("endpoint", ["search", "project_orders"])
def test_customer_permission_does_not_bypass_owned_project_scope(
    db, tmp_path, endpoint
):
    own, other = make_project(db, "CONTACT-OWN"), make_project(db, "CONTACT-OTHER")
    own_order, other_order = _import(db, tmp_path, contacts=CONTACTS[:2])
    assign(db, own_order, own)
    assign(db, other_order, other)
    client = client_for(
        db,
        username="scoped-contact-reader",
        role="purchaser",
        overrides={
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "data_customer": True,
            "own_maintenance_projects_only": True,
        },
    )
    user = db.scalar(select(SysUser).where(SysUser.username == "scoped-contact-reader"))
    _assign_manager(db, project=own, user=user, assigned_by="tester")

    # Positive control: page/data permissions really work, but only for our project.
    response, rows = _read(client, endpoint, own.project_id)
    assert response.json()["total"] == 1
    assert [row["source_order_id"] for row in rows] == [own_order.raw_order_id]
    assert rows[0]["contact_info_state"] == "visible"
    assert {key: rows[0][key] for key in HEADERS} == CONTACTS[0]
    for value in CONTACTS[1].values():
        assert value not in response.text

    if endpoint == "search":
        # An exact other-order query must not turn the field grant into row access,
        # including when the caller asks to see voided orders.
        for include_voided in (False, True):
            denied = client.post(
                "/api/maintenance/demands/search",
                json={"q": other_order.order_no, "include_voided": include_voided},
            )
            assert denied.status_code == 200
            assert denied.json()["items"] == []
            assert denied.json()["total"] == 0
            for value in CONTACTS[1].values():
                assert value not in denied.text
    else:
        denied = client.get(
            f"/api/maintenance/boss-board/projects/{other.project_id}/orders"
        )
        assert denied.status_code == 404
        for value in CONTACTS[1].values():
            assert value not in denied.text
        assert other_order.raw_order_id not in denied.text


@pytest.mark.parametrize("endpoint", ["search", "project_orders"])
def test_contact_reads_require_page_permission(db, seeded, endpoint):
    project, _ = seeded
    client = client_for(
        db, username="contact-no-page", overrides={"data_customer": True}
    )
    response = (
        client.post("/api/maintenance/demands/search", json={})
        if endpoint == "search"
        else client.get(
            f"/api/maintenance/boss-board/projects/{project.project_id}/orders"
        )
    )
    assert response.status_code == 403
    assert "合成联系人" not in response.text


@pytest.mark.parametrize(
    "role,perms,visible",
    [
        ("admin", None, True),
        ("purchaser", None, False),
        ("boss", {}, False),
        ("purchaser", {"data_customer": True}, True),
        ("boss", {"data_customer": False}, False),
    ],
)
def test_contact_uses_effective_context_and_legacy_template(role, perms, visible):
    order = FMaintenanceOrder(**CONTACTS[0])
    contact = order_contact(
        order, UserContext(user_id="synthetic", role=role, permissions=perms)
    )
    assert contact["contact_info_state"] == ("visible" if visible else "restricted")
    assert contact["receiver_phone"] == (
        CONTACTS[0]["receiver_phone"] if visible else None
    )
    assert order_contact(order, None) == {
        "contact_info_state": "restricted",
        **dict.fromkeys(HEADERS),
    }


@pytest.mark.parametrize("grant", [True, False])
def test_voided_search_keeps_contact_permission_boundary(db, seeded, grant):
    _, orders = seeded
    source_id = orders[0].raw_order_id
    maintenance_demands.void_fast(
        db,
        source_order_ids=[source_id],
        reason="合成作废",
        operated_by="tester",
    )
    db.commit()
    client = client_for(
        db,
        username="contact-voided-reader",
        role="boss",
        overrides={
            "page_maintenance": True,
            "page_maintenance_beta": True,
            "data_customer": grant,
        },
    )
    active = client.post("/api/maintenance/demands/search", json={})
    assert active.status_code == 200
    assert source_id not in {row["source_order_id"] for row in active.json()["items"]}
    response = client.post(
        "/api/maintenance/demands/search", json={"include_voided": True}
    )
    assert response.status_code == 200
    row = next(
        row for row in response.json()["items"] if row["source_order_id"] == source_id
    )
    assert row["is_voided"] is True
    assert row["contact_info_state"] == ("visible" if grant else "restricted")
    assert {key: row[key] for key in HEADERS} == (
        CONTACTS[0] if grant else dict.fromkeys(HEADERS)
    )


def test_search_enrichment_never_enters_delete_snapshots(db, seeded):
    _, orders = seeded
    source_ids = [order.raw_order_id for order in orders]
    ctx = UserContext(user_id="tester", role="admin")
    result = maintenance_demands.search_demands(
        db, q=None, page=1, page_size=20, user_ctx=ctx
    )
    assert result["items"][0]["receiver"] is not None
    intent = maintenance_demands.create_delete_intent(
        db,
        source_order_ids=source_ids,
        reason="合成复核",
        idempotency_key="contact-audit-isolation",
        operated_by="tester",
    )
    for item in intent["items"]:
        assert not (set(HEADERS) | {"contact_info_state"}) & item.keys()
    saved = list(db.scalars(select(MaintenanceDemandDeleteIntentItem)))
    assert len(saved) == len(source_ids)
    for item in saved:
        serialized = json.dumps(item.snapshot_json, ensure_ascii=False)
        assert "receiver" not in serialized and "contact_info_state" not in serialized
        assert "合成联系人" not in serialized and "00123456789" not in serialized
    # Digest still detects source changes; no plaintext contact is added to it.
    previous = {
        item["source_order_id"]: item["version_digest"] for item in intent["items"]
    }
    orders[0].receiver_phone = "000999"
    db.flush()
    current = maintenance_demands.search_demands(
        db, q=None, page=1, page_size=20, user_ctx=ctx
    )
    changed = next(
        row for row in current["items"] if row["source_order_id"] == source_ids[0]
    )
    assert changed["version_digest"] != previous[source_ids[0]]


@pytest.mark.parametrize(
    "columns", [COLUMNS_90, COLUMNS_91], ids=["90-columns", "91-columns"]
)
def test_canonical_import_headers_refresh_and_clear_contacts(db, tmp_path, columns):
    orders = _import(db, tmp_path, columns=columns)
    for order, expected in zip(orders, CONTACTS):
        assert {key: getattr(order, key) for key in HEADERS} == expected
    refreshed = [CONTACTS[1], CONTACTS[2], CONTACTS[0], CONTACTS[3]]
    orders = _import(
        db,
        tmp_path,
        contacts=refreshed,
        columns=columns,
        name="contacts-refreshed.xlsx",
    )
    assert len(orders) == 4
    for order, expected in zip(orders, refreshed):
        assert {key: getattr(order, key) for key in HEADERS} == expected
