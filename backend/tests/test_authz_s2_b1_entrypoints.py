"""Closed B1 HTTP inventory and denied requests against real PG/Cerbos."""
import pytest
from sqlalchemy import func, select

from app.models.system import SysAuditLog
from tests.test_authz_s2_b1_adversarial import b1, cerbos_url, subject  # noqa: F401


# Template path stays distinct from the concrete request; new routes fail closure.
ENTRIES = [
    ('GET', '/api/sales/analytics/options', '', None),
    ('GET', '/api/sales/analytics/summary', '', None),
    ('GET', '/api/sales/analytics/orders', '', None),
    ('GET', '/api/sales/analytics/groups', '?group_by=business_type', None),
    ('GET', '/api/sales/analytics/trend', '', None),
    ('GET', '/api/sales/analytics/amount-histogram', '', None),
    ('GET', '/api/sales/analytics/orders/export', '', None),
    ('GET', '/api/sales/analytics/orders/{order_id}', '', None),
    ('GET', '/api/purchases/recent', '', None),
    ('GET', '/api/purchases/cancellation-stats', '', None),
    ('GET', '/api/purchases/analysis', '', None),
    ('GET', '/api/purchases/analysis/part', '?part_id=1', None),
    ('GET', '/api/purchases/analytics/options', '', None),
    ('GET', '/api/purchases/analytics/summary', '', None),
    ('GET', '/api/purchases/analytics/orders', '', None),
    ('GET', '/api/purchases/analytics/orders/{order_id}', '', None),
    ('GET', '/api/profit', '', None),
    ('GET', '/api/profit/export', '', None),
    ('POST', '/api/profit/recompute', '', None),
    ('GET', '/api/inventory', '', None),
    ('GET', '/api/inventory/dynamic', '', None),
    ('GET', '/api/inventory/warehouses', '', None),
    ('PUT', '/api/inventory/{inv_id}', '', {'manual_qty': 1}),
    ('GET', '/api/system-settings', '', None),
    ('PUT', '/api/system-settings', '', {
        'maintenance_display_basis': 'inc', 'purchase_display_basis': 'inc',
        'sales_display_basis': 'inc', 'expected_version': 1}),
]


def test_every_b1_route_has_an_explicit_denial_case(b1):
    actual = {(method, route.path) for route in b1.app.routes
              if getattr(route, 'path', '').startswith('/api/')
              for method in getattr(route, 'methods', [])}
    assert actual == {(method, path) for method, path, _, _ in ENTRIES}


@pytest.mark.parametrize('method,path,query,body', ENTRIES,
                         ids=[f'{method} {path}' for method, path, _, _ in ENTRIES])
def test_admin_without_any_module_grants_cannot_use_b1_entry(db, b1, method, path, query, body):
    _, headers = subject(db, 'sales_analysis', [], role='admin')
    concrete = path.replace('{order_id}', '1').replace('{inv_id}', '1') + query
    before = db.scalar(select(func.count()).select_from(SysAuditLog))
    response = b1.request(method, concrete, headers=headers, **({'json': body} if body is not None else {}))
    assert response.status_code == 403, response.text
    assert db.scalar(select(func.count()).select_from(SysAuditLog)) == before
