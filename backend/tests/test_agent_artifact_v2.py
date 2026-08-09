"""Trusted Agent Artifact Delivery v2 backend contracts (GitHub #218)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import permissions, security
from app.auth import _make_token, hash_password
from app.main import app
from app.models.agent_artifact import AgentArtifact
from app.models.system import SysUser
from app.services import agent_files


def _login(db, client: TestClient, username: str, role: str = "sales") -> str:
    db.add(SysUser(username=username, role=role, password_hash=hash_password("pw123456")))
    db.commit()
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "pw123456"},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def test_upload_persists_complete_ready_artifact_metadata(db):
    content = "可信制品".encode()

    result = agent_files.save_upload(content, "核验记录.txt", "alice")

    artifact_id = result["file_id"]
    assert str(uuid.UUID(artifact_id)) == artifact_id
    assert result["artifact"]["id"] == artifact_id
    db.expire_all()
    artifact = db.get(AgentArtifact, artifact_id)
    assert artifact is not None
    assert artifact.owner_sub == "alice"
    assert artifact.filename == "核验记录.txt"
    assert artifact.media_type == "text/plain; charset=utf-8"
    assert artifact.size_bytes == len(content)
    assert artifact.sha256 == hashlib.sha256(content).hexdigest()
    assert artifact.status == "ready"
    assert artifact.storage_key == f"objects/{artifact.id}.txt"
    assert artifact.source_ids == []
    assert artifact.access_scope["policy"] == "owner_only"
    assert artifact.created_at is not None
    assert artifact.expires_at > artifact.created_at
    assert result["artifact"]["sha256"] == artifact.sha256
    assert result["artifact"]["mime_type"] == artifact.media_type
    assert result["artifact"]["download_url"].endswith(artifact.id)
    assert not agent_files._meta_path(result["file_id"]).exists()
    assert not (agent_files._dir() / f"{result['file_id']}.txt").exists()
    assert agent_files.get_download(artifact.id)[0].parent.name == "objects"


def test_upload_and_generated_output_are_immutable_distinct_artifacts(db):
    base = agent_files.save_upload(b"source", "source.txt", "alice")
    base_path, _ = agent_files.get_download(base["file_id"])
    original_hash = hashlib.sha256(base_path.read_bytes()).hexdigest()

    first = agent_files.write_excel(
        None,
        None,
        [{"row": 1, "col": "A", "value": "first"}],
        "result.xlsx",
        "alice",
    )
    second = agent_files.write_excel(
        None,
        None,
        [{"row": 1, "col": "A", "value": "second"}],
        "result.xlsx",
        "alice",
    )

    assert len({base["file_id"], first["file_id"], second["file_id"]}) == 3
    assert base["artifact"]["id"] == base["file_id"]
    assert first["artifact"]["id"] == first["file_id"]
    assert second["artifact"]["id"] == second["file_id"]
    assert hashlib.sha256(base_path.read_bytes()).hexdigest() == original_hash


def test_generated_artifact_records_source_ids(db):
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    base = agent_files.save_upload(buffer.getvalue(), "模板.xlsx", "alice")

    result = agent_files.write_excel(
        base["file_id"],
        None,
        [{"row": 1, "col": "A", "value": "done"}],
        "已回填.xlsx",
        "alice",
    )

    db.expire_all()
    artifact = db.get(AgentArtifact, result["artifact"]["id"])
    assert artifact is not None
    assert artifact.source_ids == [base["file_id"]]
    assert artifact.kind == "generated"


def test_failed_atomic_publish_is_not_downloadable_and_cleans_temp(db, tmp_path, monkeypatch):
    class FailAfterStageStore(agent_files.LocalArtifactStore):
        def publish_bytes(self, storage_key, content, *, validator=None):
            def fail(_path):
                raise OSError("simulated validation failure")

            return super().publish_bytes(storage_key, content, validator=fail)

    store = FailAfterStageStore(tmp_path)
    monkeypatch.setattr(agent_files, "get_artifact_store", lambda: store)

    with pytest.raises(agent_files.FileError, match="发布失败"):
        agent_files.save_upload(b"not published", "failure.txt", "failure-owner")

    db.expire_all()
    artifact = (
        db.query(AgentArtifact)
        .filter(AgentArtifact.owner_sub == "failure-owner")
        .order_by(AgentArtifact.created_at.desc())
        .first()
    )
    assert artifact is not None and artifact.status == "failed"
    with pytest.raises(agent_files.FileError):
        agent_files.get_download(artifact.id)
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.glob("*.txt"))


def test_non_ready_expired_and_integrity_mismatch_are_denied(db):
    failed = agent_files.save_upload(b"failed", "failed.txt", "alice")
    expired = agent_files.save_upload(b"expired", "expired.txt", "alice")
    corrupted = agent_files.save_upload(b"trusted", "trusted.txt", "alice")

    failed_row = db.get(AgentArtifact, failed["artifact"]["id"])
    expired_row = db.get(AgentArtifact, expired["artifact"]["id"])
    assert failed_row is not None and expired_row is not None
    failed_row.status = "failed"
    expired_row.created_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    expired_row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    with pytest.raises(agent_files.FileError):
        agent_files.get_download(failed["file_id"])
    with pytest.raises(agent_files.FileError):
        agent_files.preview(expired["file_id"])

    corrupt_path, _ = agent_files.get_download(corrupted["file_id"])
    corrupt_path.write_bytes(b"tampered")
    with pytest.raises(agent_files.FileError, match="完整性"):
        agent_files.get_download(corrupted["file_id"])


def test_storage_key_tampering_cannot_escape_store_root(db, tmp_path):
    result = agent_files.save_upload(b"safe", "safe.txt", "alice")
    artifact = db.get(AgentArtifact, result["artifact"]["id"])
    assert artifact is not None
    artifact.storage_key = "../outside-secret.txt"
    db.commit()
    (agent_files._dir().parent / "outside-secret.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(agent_files.FileError):
        agent_files.get_download(result["file_id"])


def test_download_uses_real_mime_hash_length_and_safe_unicode_filename(db):
    client = TestClient(app)
    token = _login(db, client, "artifact_alice")
    raw_name = "../报价\r\nX-Evil: yes.txt"
    result = agent_files.save_upload("报价".encode(), raw_name, "artifact_alice")

    response = client.get(
        f"/api/agent/files/{result['file_id']}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["content-length"] == str(len(response.content))
    assert response.headers["etag"] == f'"{hashlib.sha256(response.content).hexdigest()}"'
    disposition = unquote(response.headers["content-disposition"])
    assert "报价" in disposition
    assert "../" not in disposition
    assert "\r" not in disposition and "\n" not in disposition
    assert "X-Evil:" not in disposition
    assert response.headers["x-content-type-options"] == "nosniff"


def test_download_and_preview_are_owner_only_even_for_admin_and_boss(db):
    client = TestClient(app)
    owner_token = _login(db, client, "artifact_owner")
    admin_token = _login(db, client, "artifact_admin", "admin")
    boss_token = _login(db, client, "artifact_boss", "boss")
    upload = agent_files.save_upload(b"owner only", "owner.txt", "artifact_owner")
    paths = [
        f"/api/agent/files/{upload['file_id']}",
        f"/api/agent/files/{upload['artifact']['id']}",
        f"/api/agent/files/{upload['file_id']}/preview",
        f"/api/agent/files/{upload['artifact']['id']}/preview",
    ]

    for path in paths:
        assert client.get(path, headers={"Authorization": f"Bearer {owner_token}"}).status_code == 200
        assert client.get(path, headers={"Authorization": f"Bearer {admin_token}"}).status_code == 403
        assert client.get(path, headers={"Authorization": f"Bearer {boss_token}"}).status_code == 403


@pytest.mark.parametrize("payload", ["=1+1", "+SUM(A1:A2)", "-2+3", "@cmd", "  =HYPERLINK(\"x\")"])
def test_write_excel_neutralizes_formula_injection(db, payload):
    result = agent_files.write_excel(
        None,
        None,
        [{"row": 1, "col": "A", "value": payload}],
        "safe.xlsx",
        "alice",
    )
    path, _ = agent_files.get_download(result["file_id"])
    workbook = load_workbook(path, data_only=False)
    cell = workbook.active["A1"]
    assert cell.value == f"'{payload}"
    assert cell.data_type != "f"
    workbook.close()


def test_write_report_neutralizes_formula_injection_in_all_text_fields(db):
    result = agent_files.write_report(
        "=TITLE",
        ["@HEADER"],
        [["+ROW"]],
        "report.xlsx",
        "alice",
    )
    path, _ = agent_files.get_download(result["file_id"])
    workbook = load_workbook(path, data_only=False)
    sheet = workbook.active
    assert sheet["A1"].value == "'=TITLE"
    assert sheet["A2"].value == "'@HEADER"
    assert sheet["A3"].value == "'+ROW"
    assert all(sheet[cell].data_type != "f" for cell in ("A1", "A2", "A3"))
    workbook.close()


def test_generated_artifact_rechecks_current_scope_after_account_downgrade(db):
    creator_permissions = permissions.effective("sales", None)
    creator = security.UserContext(
        user_id="scoped-user",
        role="sales",
        permissions=creator_permissions,
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
    )
    result = agent_files.write_report(
        "含成本结果",
        ["成本", "毛利"],
        [[100, 20]],
        "scoped.xlsx",
        creator.user_id,
        access_scope=agent_files.snapshot_access_scope(creator),
    )
    artifact = db.get(AgentArtifact, result["artifact"]["id"])
    assert artifact is not None
    assert artifact.access_scope["policy"] == "current_scope_dominates"
    assert "data_purchase_cost" in artifact.access_scope["required_permissions"]
    assert "data_profit" in artifact.access_scope["required_permissions"]
    assert artifact.access_scope["data_permissions"]["data_profit"] is True
    assert artifact.access_scope["page_permissions"]["page_chat"] is True
    assert "profit_amount" in artifact.access_scope["visible_field_groups"]
    assert agent_files.access_allowed(result["file_id"], creator) is True

    downgraded = security.UserContext(
        user_id="scoped-user",
        role="sales",
        permissions={**creator_permissions, "data_purchase_cost": False, "data_profit": False},
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
    )
    assert agent_files.access_allowed(result["file_id"], downgraded) is False
    assert agent_files.access_allowed(result["artifact"]["id"], downgraded) is False


def test_current_scope_must_dominate_row_scope_but_may_expand(db):
    all_scope_permissions = permissions.effective("boss", None)
    creator = security.UserContext(
        user_id="boss-user", role="boss", permissions=all_scope_permissions,
        is_authenticated=True, authn="sys_user", has_stable_subject=True
    )
    result = agent_files.write_report(
        "全量结果", ["值"], [[1]], "all.xlsx", creator.user_id,
        access_scope=agent_files.snapshot_access_scope(creator),
    )
    narrowed = security.UserContext(
        user_id="boss-user",
        role="boss",
        permissions={**all_scope_permissions, "own_customers_only": True},
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
    )
    assert agent_files.access_allowed(result["file_id"], narrowed) is False

    own_scope_permissions = permissions.effective("sales", None)
    own_creator = security.UserContext(
        user_id="sales-user", role="sales", permissions=own_scope_permissions,
        is_authenticated=True, authn="sys_user", has_stable_subject=True
    )
    own_result = agent_files.write_report(
        "本人结果", ["值"], [[1]], "own.xlsx", own_creator.user_id,
        access_scope=agent_files.snapshot_access_scope(own_creator),
    )
    expanded = security.UserContext(
        user_id="sales-user",
        role="sales",
        permissions={**own_scope_permissions, "own_customers_only": False},
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
    )
    assert agent_files.access_allowed(own_result["file_id"], expanded) is True


def test_upload_is_explicit_owner_only_even_if_data_permissions_change(db):
    upload = agent_files.save_upload(b"private input", "input.txt", "alice")
    downgraded = security.UserContext(
        user_id="alice", role="sales", permissions={}, is_authenticated=True,
        authn="sys_user", has_stable_subject=True,
    )
    other = security.UserContext(
        user_id="bob", role="sales", permissions=permissions.effective("sales", None),
        is_authenticated=True, authn="sys_user", has_stable_subject=True,
    )
    assert agent_files.access_allowed(upload["file_id"], downgraded) is True
    assert agent_files.access_allowed(upload["file_id"], other) is False


def test_unclassified_generated_artifact_is_never_available_through_user_acl(db):
    result = agent_files.write_report(
        "unclassified", ["value"], [[1]], "unclassified.xlsx", "alice"
    )
    owner = security.UserContext(
        user_id="alice", role="admin", permissions=permissions.effective("admin", None),
        is_authenticated=True, authn="sys_user", has_stable_subject=True,
    )
    assert agent_files.access_allowed(result["file_id"], owner) is False


def test_v2_creation_rejects_missing_or_unauthenticated_owner(db):
    with pytest.raises(agent_files.FileError, match="已认证用户"):
        agent_files.save_upload(b"no owner", "owner.txt", None)
    with pytest.raises(agent_files.FileError, match="已认证用户"):
        agent_files.write_report("x", ["h"], [[1]], "x.xlsx", None)
    unauthenticated = security.UserContext(
        user_id="alice", role="sales", permissions=permissions.effective("sales", None)
    )
    with pytest.raises(agent_files.FileError, match="实名系统账号"):
        agent_files.snapshot_access_scope(unauthenticated)


def test_shared_or_unstable_subject_cannot_create_or_reopen_artifacts(db):
    upload = agent_files.save_upload(b"private", "private.txt", "alice")
    shared = security.UserContext(
        user_id="alice",
        role="admin",
        permissions=permissions.effective("admin", None),
        is_authenticated=True,
        authn="shared",
        has_stable_subject=False,
    )

    with pytest.raises(agent_files.FileError, match="实名系统账号"):
        agent_files.snapshot_access_scope(shared)
    assert agent_files.access_allowed(upload["file_id"], shared) is False


def test_shared_admin_token_cannot_create_or_access_v2_artifact(db):
    client = TestClient(app)
    token, _ = _make_token(
        "admin",
        "admin",
        None,
        perms=permissions.effective("admin", None),
        authn="shared",
    )
    existing = agent_files.save_upload(b"private", "private.txt", "admin")
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post(
        "/api/agent/upload",
        headers=headers,
        files={"file": ("private.txt", b"payload", "text/plain")},
    )

    assert response.status_code == 403, response.text
    assert client.get(f"/api/agent/files/{existing['file_id']}", headers=headers).status_code == 403
    assert db.query(AgentArtifact).filter(AgentArtifact.owner_sub == "admin").count() == 1


def test_upload_access_audit_never_records_raw_filename(db, monkeypatch):
    from app.api import agent as agent_api

    client = TestClient(app)
    token = _login(db, client, "audit_owner")
    calls = []

    def capture(_ctx, action, resource, detail=None):
        calls.append((action, resource, detail))

    monkeypatch.setattr(agent_api, "record_access_log", capture)
    secret_name = "甲方-客户合同-秘密报价.txt"
    response = client.post(
        "/api/agent/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": (secret_name, b"payload", "text/plain")},
    )

    assert response.status_code == 200, response.text
    assert calls == [("upload", "agent_file", {"size_bytes": len(b"payload")})]
    assert secret_name not in repr(calls)


def test_ready_commit_fault_after_object_write_removes_v2_object(db, monkeypatch):
    owner = "fault-ready-commit"
    monkeypatch.setattr(
        agent_files,
        "_mark_artifact_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("commit failed")),
    )

    with pytest.raises(agent_files.FileError, match="发布失败"):
        agent_files.save_upload(b"fault", "fault.txt", owner)

    db.expire_all()
    artifact = db.query(AgentArtifact).filter(AgentArtifact.owner_sub == owner).one()
    assert artifact.status == "failed"
    assert not agent_files._meta_path(artifact.id).exists()
    assert not agent_files._data_path(artifact.id, "txt").exists()


def test_legacy_short_id_sidecar_and_url_remain_readable(db):
    legacy_id = "abcdef123456"
    meta = {
        "filename": "legacy.txt",
        "ext": "txt",
        "kind": "upload",
        "operated_by": "alice",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    agent_files._data_path(legacy_id, "txt").write_bytes(b"legacy")
    agent_files._save_meta(legacy_id, meta)

    path, filename = agent_files.get_download(legacy_id)

    assert path.read_bytes() == b"legacy"
    assert filename == "legacy.txt"
    assert agent_files.owner_of(legacy_id) == "alice"


def test_legacy_generated_without_scope_fails_closed_for_every_role(db):
    legacy_id = "fedcba654321"
    agent_files._data_path(legacy_id, "txt").write_bytes(b"legacy generated")
    agent_files._save_meta(legacy_id, {
        "filename": "legacy-generated.txt",
        "ext": "txt",
        "kind": "generated",
        "operated_by": "alice",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    sales = security.UserContext(
        user_id="alice", role="sales", is_authenticated=True,
        authn="sys_user", has_stable_subject=True
    )
    admin = security.UserContext(
        user_id="admin", role="admin", is_authenticated=True,
        authn="sys_user", has_stable_subject=True
    )

    assert agent_files.access_allowed(legacy_id, sales) is False
    assert agent_files.access_allowed(legacy_id, admin) is False
