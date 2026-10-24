"""Registered protocol boundaries; real PG binding, shared S3 remains the default."""
from dataclasses import replace

import pytest
from fastapi import HTTPException

from app.services import authorization_artifacts as artifacts
from tests.test_authz_s3_artifacts import domain, register  # noqa: F401
from tests.authz_pdp_helpers import cerbos_url  # noqa: F401


def test_registered_protocol_cannot_borrow_the_s3_marker(db, domain, monkeypatch):
    _, identity, target, _ = domain
    original = artifacts._DOMAINS[target.kind]
    assert original.protocol == 'authz_s3'
    monkeypatch.setitem(artifacts._DOMAINS, target.kind, replace(original, protocol='authz_s4'))
    with pytest.raises(HTTPException) as error:
        register(db, identity, target, complete=False)
    assert error.value.status_code == 503
    assert error.value.detail['code'] == 'AUTHZ_ARTIFACT_PROTOCOL_REQUIRED'
    db.rollback()


def test_adapter_protocol_is_closed_and_s3_default_remains_usable(db, domain):
    _, identity, target, _ = domain
    original = artifacts._DOMAINS[target.kind]
    with pytest.raises(ValueError):
        replace(original, protocol='unregistered_protocol')
    binding = register(db, identity, target)
    db.commit()
    assert artifacts.load_artifact_binding(db, target=target) == binding
