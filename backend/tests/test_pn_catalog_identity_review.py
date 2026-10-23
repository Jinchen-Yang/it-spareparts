"""Independent authorization review regression."""

import pytest
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.models import CommandReceipt
from app.services import pn_catalog_identity as identity
from tests.test_pn_catalog_identity import allow, command, context


def test_identity_receipt_fails_closed_when_read_adapter_returns_no_decision(db):
    a, b = DimPart(pn_std="REVIEW-A"), DimPart(pn_std="REVIEW-B")
    db.add_all([a, b])
    db.commit()
    identity.execute(db, command(db, a.id, b.id), context=context(), authorize=allow)
    db.commit()
    receipt = db.scalar(select(CommandReceipt).where(CommandReceipt.operation == "identity.merge"))
    def broken_read(db, context, action, part_id):
        return None if action == "read" else allow()
    with pytest.raises(CatalogError):
        identity.project_receipt(db, receipt, context=context(), authorize=broken_read)
