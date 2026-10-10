"""Production entrypoints remain available without experimental Beta features."""
import hashlib
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.business_time import business_today
from app.config import Settings, get_settings
from app.main import app
from app.models.maintenance import FMaintenanceOrder
from tests.boss_board_helpers import client_for
from tests.test_maintenance_analytics import _part, _project
from tests.test_maintenance_analytics_explorer import URL, demand
from tests.test_maintenance_analytics_filters import _assign
from tests.test_maintenance_return_metrics import _issue, _receipt


def test_production_explorer_drills_into_project_with_beta_disabled(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "maintenance_beta_enabled", False)
    monkeypatch.setattr(settings, "maintenance_boss_dashboard_enabled", True)
    project = _project(db, "PROD-EXPLORER")
    part = _part(db, "PROD-EXPLORER-PN")
    line = demand(db, project, part, "2", "12", order_date=business_today())
    order = db.get(FMaintenanceOrder, line.order_id)
    _issue(db, project, part, "2", source_order_id=order.raw_order_id,
           issue_date=business_today())
    db.commit()
    with client_for(db, username="production-explorer", overrides={
        "page_maintenance": True, "page_maintenance_boss": True,
        "page_maintenance_beta": False, "data_purchase_cost": True,
    }) as client:
        ranking = client.get(URL, params={"range": "all"})
        assert ranking.status_code == 200, ranking.text
        assert ranking.headers["cache-control"] == "no-store"
        assert Decimal(ranking.json()["rows"][0]["value"]) == 2
        target = ranking.json()["focus"]["rows"][0]["project_id"]
        assert target == project.project_id

        # Board visibility alone does not grant stable-project access. Preserve
        # that production boundary, then grant the explicit project assignment.
        assert client.get(f"/api/maintenance/projects/stable/{target}").status_code == 403
        _assign(db, project, "production-explorer")
        overview = client.get(f"/api/maintenance/projects/stable/{target}")
        assert overview.status_code == 200, overview.text
        assert overview.json()["project"]["project_id"] == target
        card = client.get(f"/api/maintenance/boss-board/projects/{target}")
        assert card.status_code == 200, card.text
        assert card.json()["project_id"] == target
        orders = client.get(f"/api/maintenance/boss-board/projects/{target}/orders")
        assert orders.status_code == 200, orders.text
        source = orders.json()["rows"][0]["source_order_id"]
        assert source == order.raw_order_id
        lines = client.get(f"/api/maintenance/boss-board/orders/{source}/lines")
        assert lines.status_code == 200, lines.text
        assert lines.json()["total"] == 1


def test_main_resale_exports_remain_registered_with_separate_credentials(db, monkeypatch):
    settings = get_settings()
    project = _project(db, "PROD-RESALE")
    part = _part(db, "PROD-RESALE-PN")
    receipt = _receipt(db, project, part, "1")
    receipt.serial_numbers = ["PROD-RESALE-SN"]
    db.commit()
    receipt_token = "synthetic-receipt-export-token-for-test"
    catalog_token = "synthetic-catalog-export-token-for-test"
    receipt_path = "/api/integrations/resale/bad-receipts"
    catalog_path = "/api/integrations/resale/parts/snapshots"
    search_path = "/api/integrations/resale/parts/search"
    monkeypatch.setattr(settings, "resale_export_token_sha256", SecretStr(""))
    monkeypatch.setattr(settings, "resale_catalog_token_sha256", SecretStr(""))
    with TestClient(app) as client:
        disabled = client.get(receipt_path)
        assert disabled.status_code == 404
        assert disabled.json()["detail"] == "返件只读导出尚未启用"
        disabled = client.post(catalog_path, json={"partIds": [part.id]})
        assert disabled.status_code == 404
        assert disabled.json()["detail"] == "PN 只读导出尚未启用"
        monkeypatch.setattr(settings, "resale_export_token_sha256",
                            SecretStr(hashlib.sha256(receipt_token.encode()).hexdigest()))
        monkeypatch.setattr(settings, "resale_catalog_token_sha256",
                            SecretStr(hashlib.sha256(catalog_token.encode()).hexdigest()))
        assert client.get(receipt_path).status_code == 401
        assert client.post(catalog_path, json={"partIds": [part.id]}).status_code == 401
        # One integration's credential must never authorize the other export.
        assert client.get(receipt_path, headers={"Authorization": f"Bearer {catalog_token}"}).status_code == 401
        assert client.post(catalog_path, json={"partIds": [part.id]},
                           headers={"Authorization": f"Bearer {receipt_token}"}).status_code == 401
        assert client.post(search_path, json={"q": part.pn_std},
                           headers={"Authorization": f"Bearer {receipt_token}"}).status_code == 401

        receipts = client.get(receipt_path, params={"contract": 2},
                              headers={"Authorization": f"Bearer {receipt_token}"})
        assert receipts.status_code == 200, receipts.text
        assert receipts.headers["cache-control"] == "no-store"
        assert receipts.json()["items"][0]["receiptId"] == receipt.rkd_line_id
        assert receipts.json()["items"][0]["partId"] == part.id
        for path, payload in ((catalog_path, {"partIds": [part.id]}),
                              (search_path, {"q": part.pn_std})):
            result = client.post(path, json=payload,
                                 headers={"Authorization": f"Bearer {catalog_token}"})
            assert result.status_code == 200, result.text
            assert result.headers["cache-control"] == "no-store"
            assert result.json()["items"][0]["partId"] == part.id
            assert result.json()["items"][0]["pnStd"] == part.pn_std


def test_production_export_hash_configuration_rejects_plaintext_credentials():
    for field in ("resale_export_token_sha256", "resale_catalog_token_sha256"):
        with pytest.raises(ValidationError, match="64 位小写十六进制 SHA256"):
            Settings(_env_file=None, **{field: "synthetic-plaintext-token-for-test"})
        valid = Settings(_env_file=None, **{field: "a" * 64})
        assert getattr(valid, field).get_secret_value() == "a" * 64
