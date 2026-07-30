"""维保出库明细的公开行标识契约。"""
import re
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import permissions
from app.auth import hash_password
from app.etl import loader
from app.main import app
from app.models.maintenance import FMaintenanceOrder
from app.models.system import SysImportBatch, SysUser
from tests import factories as f
from tests.test_maintenance_download_object_semantics import _scoped_sales_client
from tests.test_maintenance_export_headers import _admin_client


def test_project_lines_require_authentication_before_returning_identifiers(db):
    response = TestClient(app).get(
        "/api/maintenance/lines",
        params={"project": "任意项目"},
    )

    assert response.status_code == 401


def test_project_lines_require_maintenance_page_permission(db):
    effective = permissions.effective("readonly", None)
    assert effective["page_maintenance"] is False
    db.add(SysUser(
        username="maintenance_line_page_denied",
        role="readonly",
        password_hash=hash_password("pw123456"),
        is_active=True,
        template_code="readonly",
        template_version=1,
        template_perms=effective,
        permissions=effective,
    ))
    db.commit()
    client = TestClient(app)
    login = client.post(
        "/api/auth/login",
        json={
            "username": "maintenance_line_page_denied",
            "password": "pw123456",
        },
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['token']}"})

    response = client.get(
        "/api/maintenance/lines",
        params={"project": "任意项目"},
    )

    assert response.status_code == 403


def test_project_lines_expose_stable_unique_ids_across_pages(db):
    batch = SysImportBatch(
        filename="maintenance-line-identity.xlsx",
        file_type="maintenance",
        file_hash="maintenance-line-identity",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.maintenance_result(
            {
                "M-LINE-IDENTITY": f.maintenance_head(
                    "M-LINE-IDENTITY",
                    order_no="WBDD-LINE-IDENTITY",
                    on=date(2026, 7, 30),
                    project="明细行标识项目",
                ),
            },
            [
                f.maintenance_line(
                    "M-LINE-IDENTITY",
                    "ML-LINE-IDENTITY-A",
                    "PN-LINE-A",
                    qty="1",
                ),
                f.maintenance_line(
                    "M-LINE-IDENTITY",
                    "ML-LINE-IDENTITY-B",
                    "PN-LINE-B",
                    qty="2",
                ),
                f.maintenance_line(
                    "M-LINE-IDENTITY",
                    "ML-LINE-IDENTITY-C",
                    "PN-LINE-C",
                    qty="3",
                ),
            ],
        ),
        batch.id,
        date(2026, 7, 30),
    )
    db.commit()
    client = _admin_client(db)

    first_page = client.get(
        "/api/maintenance/lines",
        params={"project": "明细行标识项目", "page": 1, "page_size": 2},
    )
    repeated_page = client.get(
        "/api/maintenance/lines",
        params={"project": "明细行标识项目", "page": 1, "page_size": 2},
    )
    second_page = client.get(
        "/api/maintenance/lines",
        params={"project": "明细行标识项目", "page": 2, "page_size": 2},
    )

    assert first_page.status_code == 200, first_page.text
    assert repeated_page.status_code == 200, repeated_page.text
    assert second_page.status_code == 200, second_page.text
    first_rows = first_page.json()["rows"]
    repeated_rows = repeated_page.json()["rows"]
    second_rows = second_page.json()["rows"]
    first_ids = [row["id"] for row in first_rows]
    repeated_ids = [row["id"] for row in repeated_rows]
    all_rows = [*first_rows, *second_rows]

    assert first_ids == repeated_ids
    assert all(
        isinstance(row["id"], str)
        and re.fullmatch(r"ML-[0-9a-f]{24}", row["id"])
        for row in all_rows
    )
    assert len({row["id"] for row in all_rows}) == 3
    assert [(row["pn_std"], row["qty"]) for row in all_rows] == [
        ("PN-LINE-C", 3.0),
        ("PN-LINE-B", 2.0),
        ("PN-LINE-A", 1.0),
    ]


def test_project_lines_identifiers_do_not_expand_scoped_sales_rows(db):
    batch = SysImportBatch(
        filename="maintenance-line-sales-scope.xlsx",
        file_type="maintenance",
        file_hash="maintenance-line-sales-scope",
    )
    db.add(batch)
    db.flush()
    loader.load(
        db,
        f.maintenance_result(
            {
                "M-SALES-A": f.maintenance_head(
                    "M-SALES-A",
                    order_no="WBDD-SALES-A",
                    project="共享销售项目",
                ),
                "M-SALES-B": f.maintenance_head(
                    "M-SALES-B",
                    order_no="WBDD-SALES-B",
                    project="共享销售项目",
                ),
            },
            [
                f.maintenance_line(
                    "M-SALES-A",
                    "ML-SALES-A",
                    "PN-SALES-A",
                    qty="1",
                ),
                f.maintenance_line(
                    "M-SALES-B",
                    "ML-SALES-B",
                    "PN-SALES-B",
                    qty="2",
                ),
            ],
        ),
        batch.id,
        date(2026, 7, 30),
    )
    orders = db.scalars(
        select(FMaintenanceOrder).order_by(FMaintenanceOrder.id),
    ).all()
    assert len(orders) == 2
    orders[0].salesperson = "销售A"
    orders[1].salesperson = "销售B"
    db.commit()
    client = _scoped_sales_client(db, salesperson_name="销售A")

    response = client.get(
        "/api/maintenance/lines",
        params={"project": "共享销售项目"},
    )

    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    assert [(row["pn_std"], row["qty"]) for row in rows] == [
        ("PN-SALES-A", 1.0),
    ]
    assert isinstance(rows[0]["id"], str)
    assert re.fullmatch(r"ML-[0-9a-f]{24}", rows[0]["id"])
    assert all("raw_line_id" not in row and "order_id" not in row for row in rows)
