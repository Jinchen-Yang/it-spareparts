"""Independent original-byte release checks after the authorized RR ends."""
from sqlalchemy import update
from sqlalchemy.orm import Session
from app.api import maintenance_collection_plan_imports as api
from app.models.system import SysUser
from tests.test_authz_s3_collection_plan import plans, grant, upload, protected  # noqa:F401
from tests.test_authz_s3_projects import canonical  # noqa:F401
from tests.test_authz_s3_project_foundation import projects  # noqa:F401
from tests.authz_pdp_helpers import cerbos_url  # noqa:F401


def test_raw_file_rechecks_identity_after_render_before_publication(db, plans, monkeypatch):
    client, headers, actor, _, _, _ = plans
    uid = actor.id
    grant(db, actor, protected.WRITE, ['preview'])
    response = upload(plans)
    assert response.status_code == 200, response.text
    batch_id = response.json()['batch_id']
    grant(db, actor, protected.READ, ['raw_download'])
    def revoke_after_render(*args, **kwargs):
        with Session(db.get_bind()) as administrator:
            administrator.execute(update(SysUser).where(SysUser.id == uid).values(token_version=SysUser.token_version + 1))
            administrator.commit()
    monkeypatch.setattr(api, 'record_access_log', revoke_after_render)
    response = client.get(f'/api/maintenance/collection-plan-imports/{batch_id}/source-file', headers=headers)
    assert response.status_code == 401, 'Revoked reader received the actual original file bytes'
