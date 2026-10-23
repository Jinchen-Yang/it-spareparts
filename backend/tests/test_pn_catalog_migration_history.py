"""Legacy worker identities remain historical; only current explicit work may execute."""

from uuid import uuid4
import pytest
from sqlalchemy import func, select
from app.pn_catalog.dto import AuthorizationDecision
from app.pn_catalog.errors import CatalogError
from app.pn_catalog.job_models import CatalogJob
from app.pn_catalog.migration_history import list_history
from app.pn_catalog.migration_models import LegacySourceRecord
from app.pn_catalog.migration_recovery import recover_record
from app.pn_catalog.migration_recovery_dto import (
    RetireLegacyWork,
)
from app.pn_catalog.migration_state_models import (
    MigrationRecordApplication,
    MigrationRecoveryDecision,
)
from tests.test_pn_catalog_migration_media_state import (
    owner,
    record,
    prepare,
    apply,
    actor,
)
from tests.test_pn_catalog_migration import allow
from tests.test_pn_catalog_migration_state import context


def history_records(subject):
    manual = {
        "request_id": str(uuid4()),
        "actor_id": str(subject),
        "status": "done",
        "reserved_tokens": 0,
        "settled_tokens": 321,
        "budget_tokens": 24000,
        "external_calls": 3,
    }
    job = {
        "id": str(uuid4()),
        "manual_request_id": manual["request_id"],
        "status": "done",
        "kind": "enrich",
        "attempts": 2,
    }
    run = {
        "id": str(uuid4()),
        "job_id": job["id"],
        "status": "done",
        "attempt": 2,
        "snapshot": {"secret": "provider snapshot"},
        "result": {"key": "private/object"},
        "cost_tokens": 4000,
    }
    recovery = {
        "image_id": str(uuid4()),
        "run_id": run["id"],
        "status": "recovered",
        "attempts": 1,
    }
    bundle = {
        "id": str(uuid4()),
        "job_id": str(uuid4()),
        "user_id": str(subject),
        "archives": [{"key": "private/bundle"}],
    }
    bundle_job = {
        "id": bundle["job_id"],
        "status": "done",
        "kind": "bundle",
        "attempts": 1,
    }
    audit = {
        "id": str(uuid4()),
        "actor_id": str(subject),
        "action": "pn.update",
        "entity": "pn",
        "detail": {"private": "secret"},
    }
    identity = {
        "id": str(uuid4()),
        "created_by": str(subject),
        "status": "applied",
        "copy_manifest": [{"object": "private"}],
    }
    conflict = {
        "id": str(uuid4()),
        "status": "resolved",
        "detail": {"secret": "conflict"},
    }
    link = {"source_namespace": "itsp", "part_id": 1, "status": "active"}
    return [
        record("manual_research", manual, manual["request_id"]),
        record("agent_job", job),
        record("agent_run", run),
        record("image_recovery", recovery, recovery["image_id"]),
        record("agent_job", bundle_job),
        record("bundle", bundle),
        record("audit", audit),
        record("identity_event", identity),
        record("catalog_conflict", conflict),
        record("catalog_link", link, "itsp:1"),
    ]


def test_historical_jobs_are_visible_to_original_owner_without_private_provider_payloads(
    db,
):
    uid, subject = owner(db)
    value, cmd = prepare(db, history_records(subject))
    result = apply(db, value, cmd)
    assert result["states"] == {"historical_terminal": 10}
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 0
    page = list_history(db, context=actor(uid), authorize=allow, limit=2)
    assert len(page["data"]) == 2 and page["meta"]["next_cursor"]
    rows = list(page["data"])
    while page["meta"]["next_cursor"]:
        page = list_history(
            db,
            context=actor(uid),
            authorize=allow,
            cursor=page["meta"]["next_cursor"],
            limit=2,
        )
        rows.extend(page["data"])
    assert len(rows) == 8  # Link/conflict have no attributable historical owner.
    assert len({item["source_record_id"] for item in rows}) == 8
    text = str(rows)
    assert all(
        secret not in text
        for secret in (
            "private/object",
            "provider snapshot",
            "cost_tokens",
            "settled_tokens",
            "private/bundle",
        )
    )
    assert list_history(db, context=actor(uid + 100), authorize=allow)["data"] == []
    assert all(
        item["legacy_status"] in {"done", "recovered", "applied", "recorded"}
        for item in rows
    )

    def denied(*_):
        raise CatalogError("auth.forbidden", "revoked", status_code=403)

    with pytest.raises(CatalogError):
        list_history(db, context=actor(uid), authorize=denied)


def test_active_or_unsettled_legacy_work_requires_review_and_never_gets_new_auth_epoch(
    db,
):
    uid, subject = owner(db)
    data = history_records(subject)
    active = data[0].payload | {"status": "running", "reserved_tokens": 1500}
    value, cmd = prepare(db, [record("manual_research", active, active["request_id"])])
    result = apply(db, value, cmd)
    assert result["states"] == {"recovery_required": 1} and not result["live_complete"]
    application = db.get(MigrationRecordApplication, cmd.record_ids[0])
    assert "authorization_epoch" not in str(application.target_ref)
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 0
    row = db.get(LegacySourceRecord, cmd.record_ids[0])
    recovery = RetireLegacyWork(
        decision="retire_fenced",
        expected_revision=1,
        source_sha256=row.payload_sha256,
        evidence_ref="isolated-review:provider-settled:1",
        reason="已核对原外呼和保留成果，结束旧执行",
        external_outcome="retained_effect_reviewed",
    )
    ctx = context()
    done = recover_record(db, row.id, recovery, context=ctx, authorize=allow)
    assert done["data"]["state"] == "historical_terminal"
    assert db.get(MigrationRecordApplication, row.id).revision == 2
    assert db.scalar(select(func.count()).select_from(MigrationRecoveryDecision)) == 1
    assert row.payload == active and row.payload_sha256 == recovery.source_sha256
    assert recover_record(db, row.id, recovery, context=ctx, authorize=allow)["meta"][
        "replayed"
    ]
    assert db.scalar(select(func.count()).select_from(MigrationRecoveryDecision)) == 1
    with pytest.raises(CatalogError, match="恢复记录已有新决定"):
        recover_record(db, row.id, recovery, context=context(), authorize=allow)


def test_recovery_rollback_restores_uncertain_state_and_revocation_blocks_replay(db):
    _, subject = owner(db)
    body = {"id": str(uuid4()), "actor_id": str(subject), "status": "running"}
    body["request_id"] = body.pop("id")
    value, cmd = prepare(db, [record("manual_research", body, body["request_id"])])
    apply(db, value, cmd)
    db.commit()
    row = db.get(LegacySourceRecord, cmd.record_ids[0])
    command = RetireLegacyWork(
        decision="retire_fenced",
        expected_revision=1,
        source_sha256=row.payload_sha256,
        evidence_ref="isolated-review:1",
        reason="结束未知外呼前已核对隔离证据",
        external_outcome="no_external_effect",
    )
    ctx = context()
    recover_record(db, row.id, command, context=ctx, authorize=allow)
    db.rollback()
    assert db.get(MigrationRecordApplication, row.id).state == "recovery_required"
    assert db.scalar(select(func.count()).select_from(MigrationRecoveryDecision)) == 0
    recover_record(db, row.id, command, context=ctx, authorize=allow)
    db.commit()

    def limited(*_):
        return AuthorizationDecision(
            frozenset({"description"}), frozenset({"description"}), {"scope": "all"}
        )

    with pytest.raises(CatalogError) as denied:
        recover_record(db, row.id, command, context=ctx, authorize=limited)
    assert denied.value.code == "auth.forbidden"


def test_restart_requires_an_actual_current_job_and_links_source_without_replaying_lease(
    db,
):
    from app.models.dimensions import DimPart
    from app.pn_catalog.migration import stage_manifest
    from app.pn_catalog.migration_dto import fingerprint
    from app.pn_catalog.migration_recovery_dto import RestartLegacyWork, RestartResearch
    from app.pn_catalog.enrichment_dto import ResearchPN
    from app.pn_catalog.migration_state import register_snapshot
    from app.pn_catalog.migration_state_dto import ApplySourceRecords
    from tests.test_pn_catalog_migration import manifest, proof
    from tests.test_pn_catalog_migration_state import command as registration
    from app.services import pn_catalog_enrichment as enrichment

    part = DimPart(pn_std=f"RESTART-HISTORY-{uuid4()}")
    db.add(part)
    db.commit()
    _, subject = owner(db)
    value = manifest(target=part.id)
    origin = value.mappings[0].source_pn_id
    payload = {
        "request_id": str(uuid4()),
        "actor_id": str(subject),
        "pn_id": str(origin),
        "status": "running",
        "reserved_tokens": 10,
    }
    source = record("manual_research", payload, payload["request_id"]).model_copy(
        update={"source_pn_id": origin}
    )
    value = value.model_copy(update={"records": [source]})
    stage_manifest(
        db, value, verified_source=proof(value), context=context(), authorize=allow
    )
    register_snapshot(db, registration(db, value), context=context(), authorize=allow)
    db.commit()
    row = db.scalar(select(LegacySourceRecord))
    apply(
        db, value, ApplySourceRecords(expected_snapshot_revision=1, record_ids=[row.id])
    )
    db.commit()
    command = RestartLegacyWork(
        decision="restart_current",
        expected_revision=1,
        source_sha256=fingerprint(payload),
        evidence_ref="isolated:old-calls-settled",
        reason="旧调用已结清，按当前资料重新研究",
        external_outcome="retained_effect_reviewed",
        replacement=RestartResearch(
            kind="research",
            command=ResearchPN(
                expected_identity_revision=0,
                expected_profile_revision=0,
                expected_content_revisions={"good": 0, "bad": 0},
                capabilities=["text"],
            ),
        ),
    )
    with pytest.raises(CatalogError) as absent:
        recover_record(db, row.id, command, context=context(), authorize=allow)
    assert absent.value.code == "migration.restart_unavailable"
    db.rollback()

    def restart(session, replacement, *, part_id, inbox_id, context):
        return enrichment.research(
            session, part_id, replacement.command, context=context, authorize=allow
        )["data"]["job_id"]

    ctx = context()
    result = recover_record(
        db, row.id, command, context=ctx, authorize=allow, restart_job=restart
    )
    job = db.scalar(select(CatalogJob))
    assert result["data"]["target_ref"]["replacement_job_id"] == str(job.id)
    assert job.status == "pending" and job.lease_token is None and job.attempt == 0
    assert job.context["actor"]["principal_id"] == ctx.actor.principal_id
    assert job.context["actor"]["authorization_epoch"] == ctx.actor.authorization_epoch
    assert job.context["actor"]["entrypoint"] == "itsp_ui"
    assert (
        job.context["actor"]["provenance"]["evidence_id"] == f"legacy-record:{row.id}"
    )
    assert job.context["causation_id"] == str(ctx.request_id)
    db.commit()
    assert recover_record(
        db, row.id, command, context=ctx, authorize=allow, restart_job=restart
    )["meta"]["replayed"]
    assert db.scalar(select(func.count()).select_from(CatalogJob)) == 1


def test_two_recovery_decisions_cannot_overwrite_each_other(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session

    _, subject = owner(db)
    payload = {
        "request_id": str(uuid4()),
        "actor_id": str(subject),
        "status": "running",
    }
    value, command = prepare(
        db, [record("manual_research", payload, payload["request_id"])]
    )
    apply(db, value, command)
    row = db.get(LegacySourceRecord, command.record_ids[0])
    record_id, source_hash = row.id, row.payload_sha256
    db.commit()
    gate, bind = Barrier(2), db.get_bind()

    def submit(number):
        recovery = RetireLegacyWork(
            decision="retire_fenced",
            expected_revision=1,
            source_sha256=source_hash,
            evidence_ref=f"isolated:{number}",
            reason="已核对旧执行及原外部成果",
            external_outcome="no_external_effect",
        )
        with Session(bind) as session:
            gate.wait(timeout=5)
            try:
                result = recover_record(
                    session, record_id, recovery, context=context(), authorize=allow
                )
                session.commit()
                return result["data"]["state"]
            except CatalogError as error:
                session.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (1, 2)))
    assert sorted(results) == ["historical_terminal", "migration.recovery_conflict"]
    assert db.scalar(select(func.count()).select_from(MigrationRecoveryDecision)) == 1


def test_wrong_resource_receipt_does_not_enter_recovery_projector(db):
    from types import SimpleNamespace
    from app.pn_catalog.migration_recovery import render_receipt

    with pytest.raises(CatalogError) as failure:
        render_receipt(
            db,
            SimpleNamespace(result={"resource": "profile", "part_id": 1}),
            context=context(),
            authorize=allow,
        )
    assert failure.value.status_code == 404
