"""A legacy return job must reacquire authority after its parsing boundary."""
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.authz import runtime
from app.config import get_settings
from app.services import maintenance_return_receipt_import as business
from tests.test_return_receipt_import import project, workbook  # noqa: F401


@pytest.mark.parametrize('parser_fails', [False, True])
def test_cutover_after_legacy_parse_prevents_plan_and_failure_publication(db, project, monkeypatch, parser_fails):
    settings = get_settings()
    monkeypatch.setattr(settings, 'authorization_mode', 'unified_modules')
    monkeypatch.setattr(settings, 'authz_enabled_resources', [])
    monkeypatch.setattr(settings, 'authz_sales_analysis_enabled', False)
    batch, created = business.enqueue(db, workbook(), 'legacy-queued.xlsx', 'tester', 'legacy-queued-cutover')
    assert created and batch.owner_user_id is None
    batch_id = batch.batch_id
    db.rollback()
    monkeypatch.setattr(business, 'SessionLocal', lambda: Session(db.get_bind()))
    original = business.parse_standard
    def parse_then_cutover(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(settings, 'authz_enabled_resources', ['maintenance_return_import'])
        runtime.activate_configured_resources(db.get_bind())
        if parser_fails:
            raise ValueError('synthetic parser failure after cutover')
        return result
    monkeypatch.setattr(business, 'parse_standard', parse_then_cutover)
    business.run_job(batch_id, 1)
    db.expire_all()
    current = db.get(business.Batch, batch_id)
    assert current.report_json['status'] == 'processing'
    assert 'error' not in current.report_json and 'rows' not in current.report_json
    assert db.scalar(select(func.count()).select_from(business.Head).where(business.Head.batch_id == batch_id)) == 0
    assert db.scalar(select(func.count()).select_from(business.Receipt)) == 0
