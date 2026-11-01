"""D-0051: 全看板同源销售范围及返件项目名称。"""
import csv
import io
import json
from uuid import uuid4

from app.services import maintenance_dashboard as dash
from tests.test_maintenance_dashboard_api import (
    BASE, _assign_manager, _freeze_today, client_for, seeded,
)
from tests.test_maintenance_return_receipts_api import _wbdd


def test_salesperson_scope_uses_master_then_source_but_never_overrides_manual_clear(db, seeded):
    p1, p2, _ = seeded
    o1 = _wbdd(db, project=p1, order_no=f"WBDD-{uuid4()}")
    o2 = _wbdd(db, project=p2, order_no=f"WBDD-{uuid4()}")
    o1.salesperson = "来源销售"
    o2.salesperson = "来源销售"
    p1.salesperson = "主档销售"
    db.commit()
    assert dash.narrow_scope_by_salesperson(db, None, "主档销售") == {p1.project_id}
    assert dash.narrow_scope_by_salesperson(db, None, "来源销售") == {p2.project_id}
    p2.salesperson_override_active = True
    db.commit()
    assert dash.narrow_scope_by_salesperson(db, None, "来源销售") == set()
    assert dash.narrow_scope_by_salesperson(db, set(), "主档销售") == set()


def test_salesperson_applies_to_all_views_and_exports_with_same_project_scope(db, seeded):
    p1, p2, _ = seeded
    p1.salesperson, p2.salesperson = "销售甲", "销售乙"
    p1.display_name = "实际项目甲"
    db.commit()
    client = client_for(db, username="salesperson-all-views", role="admin")
    assert client.get(f"{BASE}/options").json()["salespersons"] == ["销售乙", "销售甲"]
    for endpoint in ("summary", "projects", "parts", "valuation", "receipts", "quantity-checks",
                     "orders", "consumption", "consumption/issues"):
        filtered = client.get(f"{BASE}/{endpoint}", params={"salesperson": "销售甲"})
        explicit = client.get(f"{BASE}/{endpoint}", params={"project": p1.project_id})
        assert filtered.status_code == explicit.status_code == 200, (endpoint, filtered.text)
        # 服务端as_of等时间字段可变化，核心业务结果与同一项目筛选应完全一致。
        left, right = filtered.json(), explicit.json()
        for payload in (left, right):
            payload.pop("as_of", None)
            if isinstance(payload.get("meta"), dict):
                payload["meta"].pop("as_of", None)
        assert left == right, endpoint
        assert p2.project_id not in json.dumps(left)
    for kind in ("projects", "parts", "receipts", "quantity_checks", "orders", "consumption_daily", "consumption_issues"):
        filtered = client.get(f"{BASE}/export", params={"kind": kind, "salesperson": "销售甲"})
        explicit = client.get(f"{BASE}/export", params={"kind": kind, "project": p1.project_id})
        assert filtered.status_code == explicit.status_code == 200
        assert filtered.content == explicit.content, kind
    receipts = client.get(f"{BASE}/receipts", params={"salesperson": "销售甲"}).json()["rows"]
    assert receipts and all(row["project_name"] == "实际项目甲" for row in receipts)
    exported = client.get(f"{BASE}/export", params={"kind": "receipts", "salesperson": "销售甲"})
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert rows and all(row["项目名称"] == "实际项目甲" for row in rows)


def test_salesperson_options_and_filter_do_not_expand_authorized_projects(db, seeded):
    p1, p2, _ = seeded
    p1.salesperson, p2.salesperson = "可见销售", "隐藏销售"
    db.commit()
    client = client_for(db, username="salesperson-own-project", role="maintenance_manager")
    _assign_manager(db, "salesperson-own-project", p1)
    assert client.get(f"{BASE}/options").json()["salespersons"] == ["可见销售"]
    rows = client.get(f"{BASE}/projects", params={"salesperson": "隐藏销售"}).json()["rows"]
    assert rows == []
    checks = client.get(f"{BASE}/quantity-checks", params={"salesperson": "隐藏销售"}).json()
    assert checks["rows"] == [] and checks["cards"]["unassigned_receipt_qty"] == "0.000"
