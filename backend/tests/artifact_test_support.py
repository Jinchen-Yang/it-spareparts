"""Owner-only helpers for constructing otherwise unreachable Artifact crash fixtures."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.db import engine
from app.models.agent_artifact import AgentArtifact
from app.services import agent_artifact_provenance, agent_files


def force_artifact_state(
    db,
    artifact: AgentArtifact | str,
    status: str,
    **changes: Any,
) -> AgentArtifact:
    """Test-only trigger bypass in the isolated per-run PostgreSQL database.

    Production intentionally has no bypass. Tests use this only to model a crash
    snapshot that the normal state machine cannot create retrospectively.
    """
    assert str(engine.url.database or "").startswith("spareparts_test_")
    artifact_id = artifact.id if isinstance(artifact, AgentArtifact) else artifact
    db.commit()
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE agent_artifact DISABLE TRIGGER "
            "trg_agent_artifact_status_transition"
        ))
    try:
        current = db.get(AgentArtifact, artifact_id)
        assert current is not None
        for key, value in changes.items():
            setattr(current, key, value)
        current.status = status
        current.binding_envelope = agent_artifact_provenance.seal_artifact_binding(
            agent_files._binding_metadata_from_row(current)
        )
        db.commit()
    finally:
        db.rollback()
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE agent_artifact ENABLE TRIGGER "
                "trg_agent_artifact_status_transition"
            ))
    db.expire_all()
    current = db.get(AgentArtifact, artifact_id)
    assert current is not None
    return current
