"""Run only in the isolated, restored developer test database."""
import json
import secrets
from pathlib import Path
from sqlalchemy import select, text
from app.config import get_settings
from app.db import SessionLocal
from app.auth import hash_password
from app.models.system import SysUser
from app.models.maintenance_project import MaintenanceProject

settings = get_settings()
assert settings.mcp_test_environment is True
assert settings.mcp_public_base_url == "https://mcp-test.yabowei.xyz"
creds = json.loads(Path('/run/test-account.json').read_text())
with SessionLocal() as db:
    assert db.scalar(text('select current_database()')) == 'partflow_mcp_test_20260916'
    existing = db.scalar(select(SysUser).where(SysUser.username == creds['username']))
    assert existing is None, 'Test account already exists; do not reset it implicitly'
    # Production password hashes should not serve as credentials in a test copy.
    for user in db.scalars(select(SysUser)):
        user.is_active = False
        user.token_version += 1
        user.password_hash = hash_password(secrets.token_urlsafe(32))
    db.add(SysUser(username=creds['username'], password_hash=hash_password(creds['password']),
                   role='admin', display_name='开发测试专用账号（非生产）', is_active=True))
    project = MaintenanceProject(project_id='059f6ecd-55bb-43f2-94b0-cf9c36160916',
        project_code='MCP-TEST-CANARY-20260916', display_name='MCP 开发测试专用验收项目（非生产）',
        lifecycle_status='ongoing')
    db.add(project)
    db.commit()
print('Independent test account and canary project created; copied production identities disabled.')
