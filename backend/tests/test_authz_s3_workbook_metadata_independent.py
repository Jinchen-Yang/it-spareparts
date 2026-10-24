"""Independent stored-input checks complement metadata-only bootstrap tests."""

import pytest
from sqlalchemy import select

from app.models.maintenance_acceptance_checklist import (
    MaintenanceAcceptanceChecklistBatch as Batch,
    MaintenanceAcceptanceChecklistItem as Item,
)
from tests.test_authz_s3_checklist import setup, _create, cerbos_url  # noqa: F401


@pytest.mark.parametrize('changed_column', ['requirement', 'done', 'raw_json'])
def test_checklist_stored_input_tamper_cannot_reuse_original_file_binding(db, setup, changed_column):
    _, identity, pid, client, headers = setup
    batch_id = _create(db, identity, pid)
    original_hash = db.get(Batch, batch_id).file_hash
    item = db.scalar(select(Item).where(Item.batch_id == batch_id))
    if changed_column == 'requirement':
        item.requirement = 'changed after the uploaded input was bound'
    elif changed_column == 'done':
        item.done = not item.done
    else:
        item.raw_json = {**item.raw_json, 'injected_after_preview': 'different raw source'}
    db.commit()
    assert db.get(Batch, batch_id).file_hash == original_hash

    response = client.post(f'/api/maintenance/acceptance-checklist/{batch_id}/apply', headers=headers)
    assert response.status_code == 403, response.text
    db.expire_all()
    assert db.get(Batch, batch_id).status == 'pending'
