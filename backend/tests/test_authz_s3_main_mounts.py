"""Canonical grants traverse the actual application release gates."""
from fastapi.testclient import TestClient
from app.config import get_settings
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from tests.test_authz_s3_workspace import workspace, grant  # noqa:F401
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401


def test_actual_main_beta_release_gate_keeps_flags_without_legacy_account_allow(db,workspace,monkeypatch):
    from app.main import app
    _,headers,ids,actor=workspace
    settings=get_settings()
    monkeypatch.setattr(settings,'maintenance_beta_enabled',True)
    monkeypatch.setattr(settings,'maintenance_boss_dashboard_enabled',True)
    grant(db,actor,'maintenance_front_stock',['read'])
    grant(db,actor,'maintenance_recovery',['read'])
    db.add(SysAuthorizationGrant(user_id=actor.id,resource='maintenance_expense_reconcile',
        actions=['read'],scope='all',field_permissions=['data_profit'],version=1));db.commit()
    old=app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db]=lambda:db
    try:
        client=TestClient(app)
        paths=[f"/api/maintenance/projects/stable/{ids['manager']}/front-stock",
               f"/api/maintenance/projects/stable/{ids['manager']}/recovery-summary",
               '/api/maintenance/reconcile/expenses']
        for path in paths:
            view=client.get(path+'?view=authorization',headers=headers)
            assert view.status_code==200,view.text
            assert set(view.json())=={'locator','authorization_context'}
            assert client.get(path,headers=headers).status_code==200
        monkeypatch.setattr(settings,'maintenance_beta_enabled',False)
        for path in paths:assert client.get(path,headers=headers).status_code==404
    finally:
        if old is None:app.dependency_overrides.pop(get_db,None)
        else:app.dependency_overrides[get_db]=old
