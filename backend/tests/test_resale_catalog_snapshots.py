"""D-36 窄例外：商城排序只读快照的销售明细行聚合契约。"""

import hashlib
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import event

from app.config import get_settings
from app.db import engine, get_db
from app.main import app
from app.models.dimensions import DimPart
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysImportBatch

URL = "/api/integrations/resale/parts/snapshots"
TOKEN = "synthetic-snapshot-catalog-token-for-tests"


@pytest.fixture()
def catalog_client(db, monkeypatch):
    monkeypatch.setattr(
        get_settings(), "resale_catalog_token_sha256",
        SecretStr(hashlib.sha256(TOKEN.encode()).hexdigest()),
    )
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            client.headers["Authorization"] = f"Bearer {TOKEN}"
            yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


def _parts(db, count=2):
    parts = [DimPart(pn_std=f"SNAPSHOT-{i}") for i in range(count)]
    db.add_all(parts)
    db.flush()
    return parts


def _sales(db, parts):
    batch = SysImportBatch(filename="synthetic-sales.xlsx", file_type="sales", file_hash="snapshots")
    db.add(batch)
    db.flush()
    # Two lines on one old order; qty and price must not change the line count.
    cases = [
        ("old", "已生效", "备件销售", date(2000, 1, 1), parts[0], 2),
        ("exchange", "已生效", "销售换货", None, parts[0], 1),
        ("other-part", "已生效", "整机销售", date(2026, 1, 1), parts[1], 1),
        ("draft", "草稿", None, None, parts[0], 1),
        ("cancelled", "已取消", None, None, parts[0], 1),
        ("pending", "进行中", None, None, parts[0], 1),
        ("unknown", None, None, None, parts[0], 1),
    ]
    for key, status, business_type, order_date, part, count in cases:
        order = FSalesOrder(
            raw_order_id=key, order_no=f"PRIVATE-{key}", salesperson="PRIVATE-PERSON",
            data_status=status, business_type=business_type, order_date=order_date,
            import_batch_id=batch.id,
        )
        db.add(order)
        db.flush()
        for i in range(count):
            db.add(FSalesLine(
                raw_line_id=f"{key}-{i}", order_id=order.id, part_id=part.id,
                # Deliberately misleading text: identity must come from part_id.
                pn_std=parts[0].pn_std, pn_raw="PRIVATE-ORIGINAL-PN",
                qty=999, unit_price=0, counts_revenue=False,
                import_batch_id=batch.id,
            ))
    db.commit()


def test_sales_count_uses_all_effective_lines_and_exposes_no_sales_details(db, catalog_client):
    parts = _parts(db, 3)
    parts[0].is_excluded = True
    parts[0].needs_review = True
    _sales(db, parts)
    before = datetime.now(timezone.utc)
    response = catalog_client.post(URL, json={"partIds": [p.id for p in parts]})
    after = datetime.now(timezone.utc)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["schemaVersion"] == 1
    rows = payload["items"]
    assert [row["salesRecordCount"] for row in rows] == [3, 1, 0]
    assert len({row["salesRecordCountAsOf"] for row in rows}) == 1
    for row in rows:
        observed = datetime.fromisoformat(row["salesRecordCountAsOf"].replace("Z", "+00:00"))
        assert observed.utcoffset().total_seconds() == 0
        assert before <= observed <= after
        assert set(row) == {
            "requestedPartId", "partId", "status", "pnStd", "description", "brand",
            "categoryMajor", "categoryMinor", "unit", "needsReview", "isExcluded",
            "lockedFields", "updatedAt", "specs", "salesRecordCount", "salesRecordCountAsOf",
        }
    assert "PRIVATE-" not in response.text


def test_counts_follow_canonical_id_and_unknown_identity_stays_null(db, catalog_client):
    parts = _parts(db, 5)
    _sales(db, parts)
    source, cycle_a, cycle_b = parts[2:]
    source.status, source.merged_into_id = "merged", parts[0].id
    cycle_a.status, cycle_a.merged_into_id = "merged", cycle_b.id
    cycle_b.status, cycle_b.merged_into_id = "merged", cycle_a.id
    db.commit()
    missing = 999999
    requested = [source.id, parts[0].id, cycle_a.id, missing, source.id]
    response = catalog_client.post(URL, json={"partIds": requested})
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert [row["requestedPartId"] for row in rows] == requested[:-1]
    assert rows[0]["status"] == "merged"
    assert rows[0]["partId"] == rows[1]["partId"] == parts[0].id
    assert rows[0]["salesRecordCount"] == rows[1]["salesRecordCount"] == 3
    for row in rows[2:]:
        assert row == {
            "requestedPartId": row["requestedPartId"], "status": "not_found",
            "salesRecordCount": None, "salesRecordCountAsOf": None,
        }


def test_100_parts_share_one_sales_aggregate_query(db, catalog_client):
    parts = _parts(db, 100)
    db.commit()
    aggregates = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "f_sales_line" in statement.lower() and "group by" in statement.lower():
            aggregates.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = catalog_client.post(URL, json={"partIds": [p.id for p in parts]})
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 100
    assert {row["salesRecordCount"] for row in response.json()["items"]} == {0}
    assert len(aggregates) == 1


def test_failed_aggregate_returns_unknown_and_same_session_recovers(db, catalog_client):
    parts = _parts(db, 2)
    _sales(db, parts)

    def fail_aggregate(_conn, _cursor, statement, parameters, _context, _executemany):
        if "f_sales_line" in statement.lower() and "group by" in statement.lower():
            # A real PostgreSQL statement error, including aborted-transaction semantics.
            return "SELECT 1 / 0", ()
        return statement, parameters

    event.listen(engine, "before_cursor_execute", fail_aggregate, retval=True)
    try:
        response = catalog_client.post(URL, json={"partIds": [p.id for p in parts]})
    finally:
        event.remove(engine, "before_cursor_execute", fail_aggregate)
    assert response.status_code == 200, response.text
    for row in response.json()["items"]:
        assert row["pnStd"].startswith("SNAPSHOT-")
        assert row["salesRecordCount"] is None
        assert row["salesRecordCountAsOf"] is None
    assert "division" not in response.text.lower()
    # Fixture injects the same Session for both requests: failed SQL was rolled back.
    recovered = catalog_client.post(URL, json={"partIds": [p.id for p in parts]})
    assert recovered.status_code == 200, recovered.text
    assert [row["salesRecordCount"] for row in recovered.json()["items"]] == [3, 1]


def test_sales_state_refresh_does_not_depend_on_part_updated_at(db, catalog_client):
    parts = _parts(db, 2)
    _sales(db, parts)
    first = catalog_client.post(URL, json={"partIds": [parts[0].id]}).json()["items"][0]
    order = db.query(FSalesOrder).filter_by(raw_order_id="old").one()
    order.data_status = "已取消"
    db.commit()
    second = catalog_client.post(URL, json={"partIds": [parts[0].id]}).json()["items"][0]
    assert first["salesRecordCount"] == 3
    assert second["salesRecordCount"] == 1
    assert first["updatedAt"] == second["updatedAt"]


@pytest.mark.parametrize("part_ids", [[], [0], [-1], [1] * 101])
def test_snapshot_request_bounds_remain_enforced(part_ids, catalog_client):
    assert catalog_client.post(URL, json={"partIds": part_ids}).status_code == 422


def test_unknown_only_batch_has_no_sales_query(db, catalog_client):
    aggregates = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "f_sales_line" in statement.lower():
            aggregates.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = catalog_client.post(URL, json={"partIds": [999999]})
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert response.json()["items"] == [{
        "requestedPartId": 999999, "status": "not_found",
        "salesRecordCount": None, "salesRecordCountAsOf": None,
    }]
    assert aggregates == []
