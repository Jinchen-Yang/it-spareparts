"""Independent adversarial review of actual roundtrip archives and final policy."""
from pathlib import Path

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models.authorization import SysAuthorizationGrant
from app.models.maintenance import FMaintenanceLine, MaintenanceRoundtripOperation
from app.models.system import SysImportBatch, SysRawFile
from app.services import maintenance_roundtrip as business
from app.services import maintenance_roundtrip_authorization as protected
from tests.test_authz_s3_roundtrip import upload, post  # noqa: F401
from tests.test_authz_s3_source_cost import source_cost  # noqa: F401
from tests.test_authz_s3_analysis import analysis_client  # noqa: F401
from tests.test_authz_s3_workspace import workspace  # noqa: F401
from tests.test_authz_s3_projects import canonical  # noqa: F401
from tests.test_authz_s3_project_foundation import projects  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401
from tests.test_maintenance_roundtrip import _export_to_path, _edit_data_row


def test_actual_archive_metadata_cannot_be_rebound_without_matching_receipt(db, upload):
    response=post(upload);assert response.status_code==200,response.text
    batch_id=response.json()['batch_id']
    raw=db.scalar(select(SysRawFile).where(SysRawFile.batch_id==batch_id))
    original=Path(raw.storage_path).read_bytes()
    raw.filename='changed-producer-metadata.xlsx';db.commit()
    response=post(upload)
    assert response.status_code==403,response.text
    assert Path(raw.storage_path).read_bytes()==original
    assert db.scalar(select(func.count()).select_from(MaintenanceRoundtripOperation))==1


def test_failed_second_publication_preserves_first_successful_original(db, upload, tmp_path, monkeypatch):
    response=post(upload);assert response.status_code==200,response.text
    original_raw=db.scalar(select(SysRawFile).where(SysRawFile.batch_id==response.json()['batch_id']))
    original_path=Path(original_raw.storage_path);original_bytes=original_path.read_bytes()
    second=_export_to_path(db,tmp_path/'second.xlsx',contract='XSDD-RT-S3')
    _edit_data_row(second,'03_订单明细',{'操作':'UPDATE','产品描述':'second unsuccessful edit','变更原因':'actual second change'})
    paths=set(original_path.parent.glob('*'))
    real_publish=protected.artifacts.register_artifact_binding
    def fail(*args,**kwargs):
        real_publish(*args,**kwargs)
        raise RuntimeError('independent after actual receipt registration')
    monkeypatch.setattr(protected.artifacts,'register_artifact_binding',fail)
    with pytest.raises(RuntimeError,match='independent after actual receipt'):
        protected.execute(db,upload[6],path=str(second),filename=second.name)
    assert set(original_path.parent.glob('*'))==paths
    assert original_path.read_bytes()==original_bytes
    db.expire_all()
    assert db.get(FMaintenanceLine,upload[5]).description=='S3 authorized update'
    assert db.scalar(select(func.count()).select_from(MaintenanceRoundtripOperation))==1
    assert db.scalar(select(func.count()).select_from(SysImportBatch).where(SysImportBatch.file_type==business.ROUNDTRIP_FILE_TYPE))==1


def test_staged_domain_changes_cannot_commit_after_own_field_permission_removed(db, upload, monkeypatch):
    actor_id=upload[2].id
    real_recompute=business._recompute_in_transaction
    def remove_field(db):
        result=real_recompute(db)
        with Session(db.get_bind()) as administrator:
            grant=administrator.scalar(select(SysAuthorizationGrant).where(SysAuthorizationGrant.user_id==actor_id,
                SysAuthorizationGrant.resource==protected.RESOURCE))
            grant.field_permissions=['data_purchase_cost','data_profit'];grant.version+=1;administrator.commit()
        return result
    monkeypatch.setattr(business,'_recompute_in_transaction',remove_field)
    response=post(upload)
    assert response.status_code==403,response.text
    db.expire_all()
    assert db.get(FMaintenanceLine,upload[5]).description=='原始描述'
    assert db.scalar(select(func.count()).select_from(MaintenanceRoundtripOperation))==0
    assert db.scalar(select(func.count()).select_from(SysImportBatch).where(SysImportBatch.file_type==business.ROUNDTRIP_FILE_TYPE))==0
