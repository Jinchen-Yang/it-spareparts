"""The real legacy worker must not outlive its resource's persistent cutover."""
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api import maintenance as api
from app.config import get_settings
from app.models.maintenance import FMaintenanceLine, MaintenanceRoundtripOperation
from app.models.system import SysImportBatch
from app.security import UserContext
from app.services import maintenance_roundtrip as business
from tests.test_authz_s3_roundtrip import upload  # noqa: F401
from tests.test_authz_s3_source_cost import source_cost  # noqa: F401
from tests.test_authz_s3_analysis import analysis_client  # noqa: F401
from tests.test_authz_s3_workspace import workspace  # noqa: F401
from tests.test_authz_s3_projects import canonical  # noqa: F401
from tests.test_authz_s3_project_foundation import projects  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


@pytest.mark.parametrize('withdraw', [False, True])
def test_queued_legacy_roundtrip_cannot_write_after_resource_cutover(db, upload, monkeypatch, withdraw):
    _, _, actor, _, path, line_id, _ = upload
    name = actor.username
    if withdraw:
        settings = get_settings()
        monkeypatch.setattr(settings, 'authz_enabled_resources', [])
        monkeypatch.setattr(settings, 'authz_sales_analysis_enabled', False)
        monkeypatch.setattr(settings, 'authorization_mode', 'legacy_compat')
    with pytest.raises(HTTPException) as denied:
        api._import_roundtrip_in_worker(str(path), path.name, name,
            UserContext(user_id=name, role='admin', is_authenticated=True))
    assert denied.value.status_code == 503
    assert denied.value.detail['code'] == 'AUTHZ_LEGACY_EXECUTION_RETIRED'
    db.expire_all()
    assert db.get(FMaintenanceLine, line_id).description == '原始描述'
    assert db.scalar(select(func.count()).select_from(MaintenanceRoundtripOperation)) == 0
    assert db.scalar(select(func.count()).select_from(SysImportBatch).where(
        SysImportBatch.file_type == business.ROUNDTRIP_FILE_TYPE)) == 0
