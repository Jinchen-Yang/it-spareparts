"""Local synthetic authorization probes; no production DB, credentials or LLM.

Run from the repository root, with backend requirements installed:
  PYTHONPATH=backend python docs/reviews/authz-rewrite-20261023/data-scope-field-probes.py
JSON goes to stdout. Assertions intentionally capture the audited baseline's gaps.
"""
from contextlib import ExitStack
from datetime import date, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def context(**overrides):
    from app.permissions import PERMISSION_META
    from app.security import UserContext
    perms = {key: False for key in PERMISSION_META}
    perms.update(overrides)
    return UserContext(user_id="SYNTHETIC-USER", role="readonly",
                       permissions=perms, is_authenticated=True)


def supplier_filter():
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from app import config, security
    from app.services import pool_metrics, purchase_query
    engine = create_engine("sqlite:///:memory:")
    tables = (
        "CREATE TABLE dim_part (id INTEGER PRIMARY KEY, pn_std TEXT, needs_review BOOLEAN)",
        "CREATE TABLE dim_supplier (id INTEGER PRIMARY KEY, name_normalized TEXT)",
        "CREATE TABLE f_purchase_order (id INTEGER PRIMARY KEY, order_no TEXT, order_date DATE, purchaser TEXT, source_type TEXT, data_status TEXT, is_tax_inclusive BOOLEAN, supplier_id INTEGER)",
        "CREATE TABLE f_purchase_line (id INTEGER PRIMARY KEY, order_id INTEGER, part_id INTEGER, description TEXT, brand TEXT, qty NUMERIC, unit_price NUMERIC, line_amount NUMERIC)",
    )
    with engine.begin() as conn:
        for ddl in tables:
            conn.execute(text(ddl))
        conn.execute(text("INSERT INTO dim_part VALUES (1,'SYNTHETIC-PN',0)"))
        conn.execute(text("INSERT INTO dim_supplier VALUES (1,'合成供应商甲')"))
        conn.execute(text("INSERT INTO f_purchase_order VALUES (1,'SYNTHETIC-ORDER',:day,'合成采购','合成来源',:status,0,1)"),
                     {"day": date.today(), "status": config.ACTIVE_STATUS})
        conn.execute(text("INSERT INTO f_purchase_line VALUES (1,1,1,'合成描述','合成品牌',1,100,100)"))
    ctx = context(page_purchases=True, data_supplier=False)
    security.require_page("page_purchases")(ctx)
    output = []
    with Session(engine) as db, patch.object(pool_metrics, "active_pool_map", return_value={}):
        for candidate in ("合成供应商甲", "合成供应商乙"):
            data = security.apply_field_visibility(
                purchase_query.recent_purchases(db, ctx, supplier=candidate), ctx)
            output.append({"filter": candidate, "total": data["total"],
                           "items": [{key: row[key] for key in ("order_no", "supplier", "pn_std")}
                                     for row in data["items"]]})
    engine.dispose()
    assert [r["total"] for r in output] == [1, 0]
    assert output[0]["items"][0]["supplier"] is None
    return {"level": "E1", "results": output, "stub": "pool_metrics.active_pool_map only"}


def purchase_derived():
    from app import security
    from app.services import pool_metrics, purchase_analysis
    ctx = context(page_purchases=True, data_supplier=True)
    today = date.today()
    rows = []
    for index, price in enumerate((100, 200), 1):
        amount = Decimal(price)
        rows.append({"part_id": 1, "pn_std": "SYNTHETIC-PN", "needs_review": False,
                     "description": "合成描述", "brand": "合成品牌", "qty": Decimal(1),
                     "unit_price": amount, "line_amount": amount, "is_tax_inclusive": False,
                     "order_id": index, "order_date": today - timedelta(days=2-index),
                     "source_channel": "渠道甲" if index == 1 else "渠道乙", "source_type": "合成来源",
                     "amount_ex_tax": amount, "amount_inc_tax": amount * Decimal("1.13")})
    with patch.object(purchase_analysis, "_window_lines", return_value=rows), \
         patch.object(pool_metrics, "active_pool_map", return_value={}):
        data = security.apply_field_visibility(
            purchase_analysis.analysis(MagicMock(), ctx, freq_threshold=3, as_of=today), ctx)
    row = data["rows"][0]
    result = {key: row[key] for key in ("price_ex_last", "price_trend", "advice")}
    result["channels_in_hidden_amount_order"] = [r["channel"] for r in data["source_composition"]]
    result["hidden_amounts"] = [r["amount"] for r in data["source_composition"]]
    assert result == {"price_ex_last": None, "price_trend": "up", "advice": "谈价",
                      "channels_in_hidden_amount_order": ["渠道乙", "渠道甲"], "hidden_amounts": [None, None]}
    return {"level": "E2", "result": result, "stub": "_window_lines and pool_metrics.active_pool_map"}


def shared_file():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import auth, security
    from app.api import agent
    from app.db import get_db
    from app.services import agent_files
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(agent.router)
    db = MagicMock()
    db.scalar.return_value = None  # No existing SysUser; existing/inactive users do NOT fall back.
    app.dependency_overrides[get_db] = lambda: db
    with TemporaryDirectory(prefix="synthetic-authz-files-") as directory, \
         patch.object(agent_files, "_dir", return_value=Path(directory)), \
         patch.object(security, "record_security_event", return_value=None), \
         patch.object(agent, "record_access_log", return_value=None), \
         TestClient(app) as client:
        tokens = []
        for _ in range(2):
            response = client.post("/auth/login", json={"username": "synthetic-shared-name",
                                   "password": "SYNTHETIC-shared-password"})
            assert response.status_code == 200
            token = response.json()["token"]
            assert auth.verify_token(token)["fb"] is True
            tokens.append(token)
        content = b"SYNTHETIC FILE ONLY; no customer data"
        uploaded = client.post("/agent/upload", files={"file": ("synthetic.txt", content, "text/plain")},
                               headers={"Authorization": f"Bearer {tokens[0]}"})
        assert uploaded.status_code == 200
        file_id = uploaded.json()["file_id"]
        downloaded = client.get(f"/agent/files/{file_id}", headers={"Authorization": f"Bearer {tokens[1]}"})
        assert downloaded.status_code == 200 and downloaded.content == content
        assert agent_files.owner_of(file_id) == "synthetic-shared-name"
    return {"level": "E1", "login_fb": [True, True], "upload_status": 200,
            "same_self_reported_name_download_status": 200, "content_matches": True,
            "stub": "get_db means absent SysUser; audit no-op; TemporaryDirectory file root",
            "limit": "Does not impersonate existing SysUser; file_id is known in this synthetic scenario"}


def agent_page_gate():
    from fastapi import HTTPException
    from app import security
    from app.agent import tools
    ctx = context(page_chat=True, page_inventory=False)
    security.require_page("page_chat")(ctx)
    try:
        security.require_page("page_inventory")(ctx)
        raise AssertionError("Expected inventory page denial")
    except HTTPException as exc:
        assert exc.status_code == 403
    with patch.object(tools.inventory, "list_dynamic", return_value={"total": 1, "items": [{"pn_std": "SYNTHETIC-PN"}]}) as downstream:
        data = tools.dispatch(MagicMock(), "get_inventory", {}, ctx)
        downstream.assert_called_once()
    assert data["items"][0]["pn_std"] == "SYNTHETIC-PN"
    return {"level": "E2", "page_inventory_status": 403, "agent_calls_service": True,
            "output": data, "stub": "inventory.list_dynamic only"}


def agent_profit():
    from fastapi import HTTPException
    from app.agent import tools
    from app.api.profit import _aggregate_for_user
    output = []
    for cost in (False, True):
        ctx = context(page_chat=True, data_purchase_cost=cost)
        db = MagicMock()
        db.execute.return_value.all.return_value = [SimpleNamespace(
            dim="合成客户甲", revenue=Decimal(1000), rev_costed=Decimal(900),
            cost_ma=Decimal(500), cost_ff=Decimal(500), lines=2, no_cost=1, excluded_revenue=Decimal(0))]
        row = tools.dispatch(db, "get_profit_ranking", {"dimension": "customer"}, ctx)["rows"][0]
        try:
            _aggregate_for_user(db, "customer", None, None, False, ctx)
            raise AssertionError("Expected hidden customer dimension denial")
        except HTTPException as exc:
            assert exc.status_code == 403
        assert row["dimension"] == "合成客户甲" and row["gross_profit_moving"] is None
        assert row["revenue_costed"] == 900 and row["no_cost"] == 1
        assert row["cost_moving_avg"] == (500 if cost else None)
        # Lift customer gate solely to compare API financial projection.
        ctx.permissions["data_customer"] = True
        direct = _aggregate_for_user(db, "customer", None, None, False, ctx)["rows"][0]
        keys = ("revenue_costed", "cost_moving_avg", "gross_profit_moving", "no_cost")
        assert all(direct[key] is None for key in keys)
        output.append({"data_customer": False, "data_profit": False, "data_purchase_cost": cost,
                       "api_customer_status": 403, "agent_fields": {key: row[key] for key in ("dimension", *keys)},
                       "inferred_profit": row["revenue_costed"] - row["cost_moving_avg"] if cost else None,
                       "api_financial_projection": {key: direct[key] for key in keys}})
    return {"level": "E2", "results": output, "stub": "DB execute aggregate row only; real service, tool and both projections"}


def no_network(*args, **kwargs):
    raise AssertionError("Network and application DB connections forbidden by synthetic probe")


def main():
    from app import config
    # Empty environment and _env_file=None prevent real credentials being loaded.
    with patch.dict(os.environ, {}, clear=True):
        settings = config.Settings(_env_file=None, environment="dev",
                                   database_url="postgresql+psycopg://synthetic:synthetic@invalid.invalid/synthetic",
                                   admin_password="SYNTHETIC-shared-password",
                                   secret_key="SYNTHETIC-probe-key-never-use-outside-local-test")
    with ExitStack() as stack:
        stack.enter_context(patch.object(config, "get_settings", return_value=settings))
        stack.enter_context(patch.object(socket.socket, "connect", no_network))
        stack.enter_context(patch.object(socket, "create_connection", no_network))
        from sqlalchemy import event
        from app import db, security
        # psycopg may bypass Python sockets; deny the application's DB connection too.
        event.listen(db.engine, "do_connect", no_network)
        stack.callback(event.remove, db.engine, "do_connect", no_network)
        stack.enter_context(patch.object(security, "record_access_log", return_value=None))
        result = {"baseline": "7a9fd2098acb6be0009a59a84e7d7330bb960d6b",
                  "scope": "Local synthetic only; no production, network, external DB or LLM",
                  "SF-01_supplier_filter": supplier_filter(),
                  "SF-02_purchase_derived": purchase_derived(),
                  "SF-03_shared_file": shared_file(),
                  "SF-04_agent_page_gate": agent_page_gate(),
                  "SF-04_agent_profit": agent_profit()}
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
