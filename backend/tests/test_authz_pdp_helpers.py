"""Deterministic regression for overlapping ephemeral Cerbos listener addresses."""

import json
from pathlib import Path

import pytest

from tests.authz_pdp_helpers import local_cerbos_config


SOURCE = Path(__file__).resolve().parents[2] / "deploy/cerbos/config.yaml"


@pytest.mark.parametrize("http_port,grpc_port", [
    (35930, 35931), (35931, 35930), (3593, 3592), (35920, 35921), (50000, 35930),
])
def test_listener_fields_are_independent_even_when_addresses_overlap(http_port, grpc_port):
    source = SOURCE.read_text() + 'fixtureNote: "127.0.0.1:3593 and 0.0.0.0:3592"\n'
    policies = Path('/tmp/policy dir: 127.0.0.1:3593 "quoted"')
    rendered = local_cerbos_config(
        source, http_port=http_port, grpc_port=grpc_port, policies=policies,
    )
    lines = rendered.splitlines()
    assert f"  httpListenAddr: 127.0.0.1:{http_port}" in lines
    assert f"  grpcListenAddr: 127.0.0.1:{grpc_port}" in lines
    assert f"    directory: {json.dumps(str(policies))}" in lines
    assert lines[-1] == 'fixtureNote: "127.0.0.1:3593 and 0.0.0.0:3592"'
    # Security and engine settings, comments and every other field are unchanged.
    changed = {"httpListenAddr", "grpcListenAddr", "directory"}

    def unchanged(text):
        return [line for line in text.splitlines()
                if line.strip().split(":", 1)[0] not in changed]

    assert unchanged(rendered) == unchanged(source)


@pytest.mark.parametrize("field", ["httpListenAddr", "grpcListenAddr", "directory"])
@pytest.mark.parametrize("duplicate", [False, True])
def test_fixture_config_drift_is_rejected_instead_of_leaving_a_wrong_listener(field, duplicate):
    lines = SOURCE.read_text().splitlines(keepends=True)
    selected = next(line for line in lines if line.strip().startswith(field + ":"))
    source = "".join(lines) + selected if duplicate else "".join(line for line in lines if line != selected)
    with pytest.raises(ValueError, match="exactly one"):
        local_cerbos_config(source, http_port=35930, grpc_port=35931, policies=Path('/tmp/policies'))
