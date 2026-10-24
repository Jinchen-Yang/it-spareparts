"""Global diagnostic grants remain independent from legacy admin status."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.api import maintenance, maintenance_audit, maintenance_expense_reconcile
from app.authz.catalog import get_resource_definition
from app.db import get_db
from app.models.authorization import SysAuthorizationGrant
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401


@pytest.mark.parametrize('resource,path,router',[
 ('maintenance_business_date','/api/maintenance/as-of',maintenance.router),
 ('maintenance_audit','/api/maintenance/match-audit',maintenance_audit.router),
 ('maintenance_expense_reconcile','/api/maintenance/reconcile/expenses',maintenance_expense_reconcile.router),
])
def test_diagnostic_exact_grant_allows_readonly_and_view_then_denies_revoke(db,canonical,projects,resource,path,router):
    _,headers,_=canonical;actor=projects[0]
    app=FastAPI();app.include_router(router,prefix='/api');app.dependency_overrides[get_db]=lambda:db
    client=TestClient(app)
    denied=client.get(path,headers=headers)
    assert denied.status_code==403,denied.text
    definition=get_resource_definition(resource)
    row=SysAuthorizationGrant(user_id=actor.id,resource=resource,actions=['read'],
        scope=next(iter(definition.scopes)),field_permissions=sorted(definition.field_permissions),version=1)
    db.add(row);db.commit()
    view=client.get(path+'?view=authorization',headers=headers)
    assert view.status_code==200,view.text
    assert set(view.json())=={'locator','authorization_context'}
    assert view.json()['authorization_context']['resources'][resource]['decisions']['read']['effect']=='allow'
    result=client.get(path,headers=headers)
    assert result.status_code==200,result.text
    assert result.headers['cache-control']=='no-store'
    assert 'authorization_context' in result.json()
    row.actions=[];row.version+=1;db.commit()
    assert client.get(path,headers=headers).status_code==403
