"""Scoped, bounded PN and price evidence exports for the independent shop."""

import hashlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.api import resale_catalog_export
from app.config import Settings
from app.models.dimensions import DimPart, PartAlias
from app.models.master_data import ProductSpec
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import SysImportBatch

TOKEN = "synthetic-catalog-only-" + "c" * 36


def test_catalog_credential_is_default_off_and_digest_only():
    assert not Settings(_env_file=None).resale_catalog_token_sha256.get_secret_value()
    with pytest.raises(ValidationError):
        Settings(_env_file=None, resale_catalog_token_sha256=SecretStr("not-a-digest"))


def client(monkeypatch, *, enabled=True):
    digest = hashlib.sha256(TOKEN.encode()).hexdigest() if enabled else ""
    monkeypatch.setattr(
        resale_catalog_export,
        "get_settings",
        lambda: SimpleNamespace(resale_catalog_token_sha256=SecretStr(digest)),
    )
    app = FastAPI()
    app.include_router(resale_catalog_export.router, prefix="/api")
    caller = TestClient(app)
    caller.headers["Authorization"] = f"Bearer {TOKEN}"
    return caller


def test_catalog_disabled_or_invalid_credentials_never_expose_part(db, monkeypatch):
    db.add(DimPart(pn_std="EXPORT-PRIVATE-PN", description="商品描述"))
    db.commit()
    caller = client(monkeypatch, enabled=False)
    assert (
        caller.post(
            "/api/integrations/resale/parts/snapshots", json={"partIds": [1]}
        ).status_code
        == 404
    )
    caller = client(monkeypatch)
    caller.headers["Authorization"] = "Bearer " + "wrong" * 10
    assert (
        caller.post(
            "/api/integrations/resale/parts/snapshots", json={"partIds": [1]}
        ).status_code
        == 401
    )
    caller.headers.pop("Authorization")
    assert (
        caller.post(
            "/api/integrations/resale/parts/price-evidence",
            json={"partIds": [1], "side": "sales"},
        ).status_code
        == 401
    )


def test_part_snapshot_merges_aliases_and_only_returns_product_fields(db, monkeypatch):
    canonical = DimPart(
        pn_std="PN-REAL-001",
        description="商品描述",
        brand="示例品牌",
        status="active",
        locked_fields=["description"],
        needs_review=False,
    )
    db.add(canonical)
    db.flush()
    merged = DimPart(pn_std="PN-OLD", status="merged", merged_into_id=canonical.id)
    db.add_all(
        [
            merged,
            PartAlias(
                pn_raw="ALIAS-ONE",
                pn_std=canonical.pn_std,
                part_id=canonical.id,
                status="active",
            ),
            PartAlias(
                pn_raw="PENDING-ALIAS",
                pn_std=canonical.pn_std,
                part_id=canonical.id,
                status="pending",
            ),
            ProductSpec(
                part_id=canonical.id,
                spec_key="capacity",
                spec_value="1.2TB",
                spec_unit="TB",
                source="manual",
            ),
        ]
    )
    db.commit()
    caller = client(monkeypatch)
    resolved = caller.post(
        "/api/integrations/resale/parts/resolve",
        json={
            "items": [
                {"sourceKey": "one", "pnRaw": "ALIAS-ONE"},
                {"sourceKey": "two", "pnRaw": "PENDING-ALIAS"},
            ]
        },
    )
    assert resolved.status_code == 200
    assert [item["status"] for item in resolved.json()["items"]] == [
        "alias",
        "ambiguous",
    ]
    assert resolved.json()["items"][0]["partId"] == canonical.id
    snapshots = caller.post(
        "/api/integrations/resale/parts/snapshots",
        json={"partIds": [merged.id, canonical.id]},
    )
    assert snapshots.status_code == 200
    assert snapshots.headers["Cache-Control"] == "no-store"
    by_requested = {row["requestedPartId"]: row for row in snapshots.json()["items"]}
    assert by_requested[merged.id]["status"] == "merged"
    assert by_requested[merged.id]["partId"] == canonical.id
    assert by_requested[canonical.id]["description"] == "商品描述"
    assert by_requested[canonical.id]["lockedFields"] == ["description"]
    assert by_requested[canonical.id]["specs"] == [
        {"key": "capacity", "value": "1.2TB", "unit": "TB", "source": "manual"}
    ]
    assert "inventory" not in snapshots.text and "customer" not in snapshots.text
    assert (
        caller.post(
            "/api/integrations/resale/parts/snapshots", json={"partIds": []}
        ).status_code
        == 422
    )


def test_sale_purchase_evidence_page_by_source_row_without_names(db, monkeypatch):
    batch = SysImportBatch(
        filename="test.xlsx",
        file_type="sales",
        file_hash="catalog-export-fixture",
        uploaded_by="synthetic",
        status="success",
    )
    part = DimPart(pn_std="CATALOG-TEST-PN", description="销售描述")
    db.add_all([batch, part])
    db.flush()
    sale = FSalesOrder(
        raw_order_id="sale-head-1",
        order_no="S-001",
        order_date=date(2026, 9, 1),
        data_status="已生效",
        business_type="备件销售",
        salesperson="DO-NOT-LEAK-SELLER",
        import_batch_id=batch.id,
    )
    purchase = FPurchaseOrder(
        raw_order_id="purchase-head-1",
        order_no="P-001",
        order_date=date(2026, 8, 1),
        data_status="已生效",
        is_tax_inclusive=False,
        source_type="指定采购",
        purchaser="DO-NOT-LEAK-PURCHASER",
        import_batch_id=batch.id,
    )
    db.add_all([sale, purchase])
    db.flush()
    db.add_all(
        [
            FSalesLine(
                raw_line_id="sale-line-1",
                order_id=sale.id,
                part_id=part.id,
                qty=Decimal("2"),
                unit_price=Decimal("100.00"),
                import_batch_id=batch.id,
            ),
            FSalesLine(
                raw_line_id="sale-line-2",
                order_id=sale.id,
                part_id=part.id,
                qty=Decimal("1"),
                unit_price=Decimal("80.00"),
                import_batch_id=batch.id,
            ),
            FPurchaseLine(
                raw_line_id="purchase-line-1",
                order_id=purchase.id,
                part_id=part.id,
                qty=Decimal("2"),
                unit_price=Decimal("20.00"),
                import_batch_id=batch.id,
            ),
        ]
    )
    db.commit()
    caller = client(monkeypatch)
    url = "/api/integrations/resale/parts/price-evidence"
    first = caller.post(url, json={"partIds": [part.id], "side": "sales", "limit": 1})
    assert first.status_code == 200
    assert first.json()["hasMore"] is True
    assert first.json()["items"][0]["unitPrice"] == "100.00"
    assert first.json()["items"][0]["channel"] == "unknown"
    second = caller.post(
        url,
        json={
            "partIds": [part.id],
            "side": "sales",
            "limit": 1,
            "after": first.json()["nextCursor"],
        },
    )
    assert second.status_code == 200
    assert [
        r["sourceLineId"] for r in first.json()["items"] + second.json()["items"]
    ] == ["sale-line-1", "sale-line-2"]
    assert second.json()["hasMore"] is False
    bought = caller.post(url, json={"partIds": [part.id], "side": "purchases"})
    assert bought.status_code == 200
    assert bought.json()["items"][0]["isTaxInclusive"] is False
    assert bought.json()["items"][0]["unitPrice"] == "20.00"
    assert "DO-NOT-LEAK-SELLER" not in first.text + second.text + bought.text
    assert "DO-NOT-LEAK-PURCHASER" not in first.text + second.text + bought.text
    assert (
        caller.post(
            url, json={"partIds": list(range(1, 52)), "side": "sales"}
        ).status_code
        == 422
    )
