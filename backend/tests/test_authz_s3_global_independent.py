"""Independent global-export checks after complete RR materialization."""
from sqlalchemy import update
from sqlalchemy.orm import Session
from app.models.maintenance import FMaintenanceOrder
from app.models.authorization import SysAuthorizationGrant
from app.services import maintenance_export
from tests.test_authz_s3_source_cost import source_cost,give
from tests.test_authz_s3_analysis import analysis_client
from tests.test_authz_s3_workspace import workspace
from tests.test_authz_s3_projects import canonical
from tests.test_authz_s3_project_foundation import projects
from tests.authz_pdp_helpers import cerbos_url
import pytest


@pytest.mark.parametrize('change',['source_sales','field_grant'])
def test_global_export_rechecks_actual_relation_and_grant_after_render(db,source_cost,monkeypatch,change):
    client,headers,actor=source_cost
    grant=give(db,actor,['export_orders'],fields=['data_purchase_cost'])
    grant_id=grant.id;original=maintenance_export.build_workbook;spools=[]
    def render(snapshot,*args,**kwargs):
        spool=original(snapshot,*args,**kwargs);spools.append(spool)
        with Session(db.get_bind()) as writer:
            if change=='source_sales':
                writer.execute(update(FMaintenanceOrder).where(FMaintenanceOrder.order_no=='WBDD-S3-other').values(salesperson='a different real owner'))
            else:
                writer.execute(update(SysAuthorizationGrant).where(SysAuthorizationGrant.id==grant_id).values(field_permissions=[],version=2))
            writer.commit()
        return spool
    monkeypatch.setattr(maintenance_export,'build_workbook',render)
    response=client.get('/api/maintenance/orders/export',headers=headers)
    assert response.status_code==403,response.text
    assert len(spools)==1 and spools[0].closed
