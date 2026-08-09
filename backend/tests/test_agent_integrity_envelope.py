"""Shared integrity-envelope/v1 contract for Agent control-plane evidence."""

from __future__ import annotations

import copy

import pytest

from app.services import agent_integrity


def _keyring(status: str = "active") -> agent_integrity.IntegrityKeyring:
    return agent_integrity.IntegrityKeyring(
        active_key_id="test-v1",
        keys={
            "test-v1": agent_integrity.IntegrityKey(
                secret=b"test-only-integrity-key-material-32-bytes",
                status=status,
            )
        },
    )


def test_integrity_envelope_authenticates_purpose_schema_and_payload():
    payload = {"owner_sub": "alice", "contained_fields": ["purchase_cost"]}
    envelope = agent_integrity.seal(
        payload,
        purpose="agent.source-snapshot",
        payload_schema_version="artifact-source-snapshot/v1",
        keyring=_keyring(),
    )

    assert envelope["header"] == {
        "schema_version": "integrity-envelope/v1",
        "purpose": "agent.source-snapshot",
        "payload_schema_version": "artifact-source-snapshot/v1",
        "canonicalization": "RFC8785",
        "algorithm": "HMAC-SHA-256",
        "key_id": "test-v1",
        "payload_sha256": envelope["header"]["payload_sha256"],
    }
    assert agent_integrity.verify(
        envelope,
        allowed_purposes={"agent.source-snapshot"},
        allowed_payload_schemas={"artifact-source-snapshot/v1"},
        keyring=_keyring(),
    ) == payload

    tampered = copy.deepcopy(envelope)
    tampered["payload"]["owner_sub"] = "mallory"
    with pytest.raises(agent_integrity.IntegrityError):
        agent_integrity.verify(
            tampered,
            allowed_purposes={"agent.source-snapshot"},
            allowed_payload_schemas={"artifact-source-snapshot/v1"},
            keyring=_keyring(),
        )


def test_integrity_key_rotation_allows_verify_only_but_rejects_revoked_keys():
    envelope = agent_integrity.seal(
        {"source_ref": "opaque-ref"},
        purpose="agent.source-snapshot",
        payload_schema_version="artifact-source-snapshot/v1",
        keyring=_keyring(),
    )

    assert agent_integrity.verify(
        envelope,
        allowed_purposes={"agent.source-snapshot"},
        allowed_payload_schemas={"artifact-source-snapshot/v1"},
        keyring=_keyring("verify_only"),
    )["source_ref"] == "opaque-ref"
    with pytest.raises(agent_integrity.IntegrityError, match="撤销"):
        agent_integrity.verify(
            envelope,
            allowed_purposes={"agent.source-snapshot"},
            allowed_payload_schemas={"artifact-source-snapshot/v1"},
            keyring=_keyring("revoked"),
        )


def test_canonicalization_rejects_lone_unicode_surrogates_with_stable_error():
    with pytest.raises(agent_integrity.IntegrityError, match="Unicode"):
        agent_integrity.canonicalize({"value": "\ud800"})


def test_restricted_rfc8785_schema_rejects_non_ascii_object_keys():
    for key in ("字段", "\U0001f600"):
        with pytest.raises(agent_integrity.IntegrityError, match="ASCII"):
            agent_integrity.canonicalize({key: "value"})


def test_frozen_keyring_defensively_copies_the_key_mapping():
    original = {
        "test-v1": agent_integrity.IntegrityKey(
            secret=b"test-only-integrity-key-material-32-bytes",
            status="active",
        )
    }
    keyring = agent_integrity.IntegrityKeyring("test-v1", original)
    original["test-v1"] = agent_integrity.IntegrityKey(
        secret=b"replacement-integrity-key-material-32-bytes",
        status="revoked",
    )

    assert keyring.signing_key()[1].status == "active"
    with pytest.raises(TypeError):
        keyring.keys["other"] = original["test-v1"]


def test_verify_returns_a_defensive_canonical_roundtrip_copy():
    envelope = agent_integrity.seal(
        {"nested": {"values": ["original"]}},
        purpose="agent.source-snapshot",
        payload_schema_version="artifact-source-snapshot/v1",
        keyring=_keyring(),
    )

    verified = agent_integrity.verify(
        envelope,
        allowed_purposes={"agent.source-snapshot"},
        allowed_payload_schemas={"artifact-source-snapshot/v1"},
        keyring=_keyring(),
    )
    verified["nested"]["values"][0] = "mutated"

    assert envelope["payload"]["nested"]["values"] == ["original"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"extra": "forbidden"}),
        lambda value: value["header"].update({"extra": "forbidden"}),
    ],
)
def test_integrity_envelope_rejects_unknown_top_level_or_header_fields(mutation):
    envelope = agent_integrity.seal(
        {"source_ref": "opaque-ref"},
        purpose="agent.source-snapshot",
        payload_schema_version="artifact-source-snapshot/v1",
        keyring=_keyring(),
    )
    mutation(envelope)
    with pytest.raises(agent_integrity.IntegrityError):
        agent_integrity.verify(
            envelope,
            allowed_purposes={"agent.source-snapshot"},
            allowed_payload_schemas={"artifact-source-snapshot/v1"},
            keyring=_keyring(),
        )


def test_integrity_envelope_rejects_wrong_purpose_or_payload_schema():
    envelope = agent_integrity.seal(
        {"source_ref": "opaque-ref"},
        purpose="agent.source-snapshot",
        payload_schema_version="artifact-source-snapshot/v1",
        keyring=_keyring(),
    )
    for purposes, schemas in (
        ({"agent.checkpoint"}, {"artifact-source-snapshot/v1"}),
        ({"agent.source-snapshot"}, {"agent-checkpoint/v1"}),
    ):
        with pytest.raises(agent_integrity.IntegrityError, match="purpose"):
            agent_integrity.verify(
                envelope,
                allowed_purposes=purposes,
                allowed_payload_schemas=schemas,
                keyring=_keyring(),
            )


def test_integrity_envelope_enforces_size_depth_and_i_json_type_budgets():
    with pytest.raises(agent_integrity.IntegrityError, match="大小"):
        agent_integrity.seal(
            {"value": "too-large"},
            purpose="agent.source-snapshot",
            payload_schema_version="artifact-source-snapshot/v1",
            keyring=_keyring(),
            max_payload_bytes=1,
        )
    nested = None
    for _ in range(40):
        nested = [nested]
    with pytest.raises(agent_integrity.IntegrityError, match="嵌套"):
        agent_integrity.canonicalize({"value": nested})
    for invalid in (9_007_199_254_740_992, 1.5, {"not-json"}):
        with pytest.raises(agent_integrity.IntegrityError):
            agent_integrity.canonicalize({"value": invalid})


def test_canonicalization_enforces_total_node_string_and_work_budgets(monkeypatch):
    monkeypatch.setattr(agent_integrity, "MAX_JSON_NODES", 4)
    with pytest.raises(agent_integrity.IntegrityError, match="节点"):
        agent_integrity.canonicalize({"values": [1, 2]})

    monkeypatch.setattr(agent_integrity, "MAX_JSON_NODES", 10_000)
    monkeypatch.setattr(agent_integrity, "MAX_JSON_STRING_BYTES", 3)
    with pytest.raises(agent_integrity.IntegrityError, match="字符串"):
        agent_integrity.canonicalize({"k": "abc"})

    monkeypatch.setattr(agent_integrity, "MAX_JSON_STRING_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(agent_integrity, "MAX_JSON_WORK", 4)
    with pytest.raises(agent_integrity.IntegrityError, match="工作量"):
        agent_integrity.canonicalize({"values": [1]})


def test_verify_only_key_cannot_sign_and_unknown_key_cannot_verify():
    with pytest.raises(agent_integrity.IntegrityError, match="active"):
        agent_integrity.seal(
            {"source_ref": "opaque-ref"},
            purpose="agent.source-snapshot",
            payload_schema_version="artifact-source-snapshot/v1",
            keyring=_keyring("verify_only"),
        )
    envelope = agent_integrity.seal(
        {"source_ref": "opaque-ref"},
        purpose="agent.source-snapshot",
        payload_schema_version="artifact-source-snapshot/v1",
        keyring=_keyring(),
    )
    unknown = agent_integrity.IntegrityKeyring(
        active_key_id="other",
        keys={
            "other": agent_integrity.IntegrityKey(
                secret=b"other-test-integrity-key-material-32-bytes",
                status="active",
            )
        },
    )
    with pytest.raises(agent_integrity.IntegrityError, match="未知"):
        agent_integrity.verify(
            envelope,
            allowed_purposes={"agent.source-snapshot"},
            allowed_payload_schemas={"artifact-source-snapshot/v1"},
            keyring=unknown,
        )


def test_digest_and_mac_comparisons_use_constant_time_primitive(monkeypatch):
    envelope = agent_integrity.seal(
        {"source_ref": "opaque-ref"},
        purpose="agent.source-snapshot",
        payload_schema_version="artifact-source-snapshot/v1",
        keyring=_keyring(),
    )
    calls = []
    real_compare = agent_integrity.hmac.compare_digest

    def capture(left, right):
        calls.append((type(left), type(right)))
        return real_compare(left, right)

    monkeypatch.setattr(agent_integrity.hmac, "compare_digest", capture)
    agent_integrity.verify(
        envelope,
        allowed_purposes={"agent.source-snapshot"},
        allowed_payload_schemas={"artifact-source-snapshot/v1"},
        keyring=_keyring(),
    )
    assert calls == [(str, str), (str, str)]
