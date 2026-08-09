"""Trusted Agent Artifact Delivery v2 backend contracts (GitHub #218)."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app import permissions, security
from app.auth import _make_token, hash_password
from app.config import Settings, get_settings
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


def _owner_from_context(db, ctx: security.UserContext) -> agent_files.VerifiedArtifactOwner:
    user = db.query(SysUser).filter(SysUser.username == ctx.user_id).one_or_none()
    if user is None:
        user = SysUser(
            username=ctx.user_id,
            role=ctx.role,
            salesperson_name=ctx.salesperson_name,
            permissions=ctx.permissions,
            password_hash=hash_password("pw123456"),
            token_version=ctx.token_version or 0,
            is_active=True,
        )
        db.add(user)
    else:
        user.role = ctx.role
        user.salesperson_name = ctx.salesperson_name
        user.permissions = ctx.permissions
        user.is_active = True
    db.commit()
    current_permissions = permissions.runtime_safe(permissions.effective_for_user(user))
    current = security.UserContext(
        user_id=user.username,
        role=user.role,
        salesperson_name=user.salesperson_name,
        permissions=current_permissions,
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
        token_version=user.token_version or 0,
    )
    return agent_files.verified_artifact_owner(db, current)


def _verified_owner(db, username: str) -> agent_files.VerifiedArtifactOwner:
    user = db.query(SysUser).filter(SysUser.username == username).one_or_none()
    if user is None:
        user = SysUser(
            username=username,
            role="admin",
            permissions=permissions.effective("admin", None),
            password_hash=hash_password("pw123456"),
        )
        db.add(user)
        db.commit()
    return _owner_from_context(db, security.UserContext(
        user_id=user.username,
        role=user.role,
        salesperson_name=user.salesperson_name,
        permissions=permissions.runtime_safe(permissions.effective_for_user(user)),
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
        token_version=user.token_version or 0,
    ))


def test_artifact_v2_kill_switch_defaults_off(monkeypatch):
    monkeypatch.delenv("AGENT_ARTIFACT_V2_ENABLED", raising=False)
    assert Settings(_env_file=None).agent_artifact_v2_enabled is False


def test_disabled_v2_returns_503_while_legacy_owner_read_remains_available(db, monkeypatch):
    client = TestClient(app)
    token = _login(db, client, "switch_owner")
    headers = {"Authorization": f"Bearer {token}"}
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_artifact_v2_enabled", True)
    created = client.post(
        "/api/agent/upload",
        headers=headers,
        files={"file": ("v2.txt", b"v2", "text/plain")},
    )
    assert created.status_code == 200, created.text
    artifact_id = created.json()["file_id"]

    legacy_id = "abcdef123456"
    agent_files._data_path(legacy_id, "txt").write_bytes(b"legacy")
    agent_files._save_meta(legacy_id, {
        "filename": "legacy.txt",
        "ext": "txt",
        "kind": "upload",
        "operated_by": "switch_owner",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    monkeypatch.setattr(settings, "agent_artifact_v2_enabled", False)
    disabled_message = "Artifact Delivery v2 已停用"
    for path in (
        f"/api/agent/files/{artifact_id}",
        f"/api/agent/files/{artifact_id}/preview",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code == 503
        assert response.json()["detail"] == disabled_message
    with pytest.raises(agent_files.ArtifactV2Disabled, match=disabled_message):
        agent_files.read_document(artifact_id, _verified_owner(db, "switch_owner"))

    denied_create = client.post(
        "/api/agent/upload",
        headers=headers,
        files={"file": ("disabled.txt", b"disabled", "text/plain")},
    )
    assert denied_create.status_code == 503
    assert denied_create.json()["detail"] == disabled_message

    assert client.get(f"/api/agent/files/{legacy_id}", headers=headers).status_code == 200
    assert client.get(f"/api/agent/files/{legacy_id}/preview", headers=headers).status_code == 200


def test_upload_persists_complete_ready_artifact_metadata(db):
    content = "可信制品".encode()
    owner = _verified_owner(db, "alice")
    result = agent_files.save_upload(content, "核验记录.txt", owner)

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
    assert artifact.sensitivity == "critical"
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
    assert agent_files.get_download(artifact.id, owner)[0].parent.name == "objects"


def test_upload_and_generated_output_are_immutable_distinct_artifacts(db):
    owner = _verified_owner(db, "alice")
    base = agent_files.save_upload(b"source", "source.txt", owner)
    base_path, _ = agent_files.get_download(base["file_id"], owner)
    original_hash = hashlib.sha256(base_path.read_bytes()).hexdigest()

    first = agent_files.write_excel(
        None,
        None,
        [{"row": 1, "col": "A", "value": "first"}],
        "result.xlsx",
        owner,
    )
    second = agent_files.write_excel(
        None,
        None,
        [{"row": 1, "col": "A", "value": "second"}],
        "result.xlsx",
        owner,
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
    owner = _verified_owner(db, "alice")
    base = agent_files.save_upload(buffer.getvalue(), "模板.xlsx", owner)

    result = agent_files.write_excel(
        base["file_id"],
        None,
        [{"row": 1, "col": "A", "value": "done"}],
        "已回填.xlsx",
        owner,
    )

    db.expire_all()
    artifact = db.get(AgentArtifact, result["artifact"]["id"])
    assert artifact is not None
    assert artifact.source_ids == [base["file_id"]]
    assert artifact.kind == "generated"


def test_service_write_rejects_cross_owner_and_revoked_source_artifacts(db):
    alice = security.UserContext(
        user_id="alice",
        role="boss",
        permissions=permissions.effective("boss", None),
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
    )
    bob = security.UserContext(
        user_id="bob",
        role="boss",
        permissions=permissions.effective("boss", None),
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
    )
    private_base = agent_files.save_upload(
        b"private", "private.txt", _owner_from_context(db, alice)
    )
    with pytest.raises(agent_files.FileError, match="无权引用来源制品"):
        agent_files.write_excel(
            private_base["file_id"], None, [{"row": 1, "col": "A", "value": 1}],
            "cross-owner.xlsx", _owner_from_context(db, bob),
        )

    generated = agent_files.write_report(
        "full", ["value"], [[1]], "full.xlsx",
        _owner_from_context(db, alice),
    )
    revoked = security.UserContext(
        user_id="alice",
        role="boss",
        permissions={key: False for key in permissions.ALL_KEYS},
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
    )
    with pytest.raises(agent_files.FileError, match="无权引用来源制品"):
        agent_files.write_excel(
            generated["file_id"], None, [{"row": 1, "col": "A", "value": 1}],
            "revoked.xlsx", _owner_from_context(db, revoked),
        )


def test_failed_atomic_publish_is_not_downloadable_and_cleans_temp(db, tmp_path, monkeypatch):
    class FailAfterStageStore(agent_files.LocalArtifactStore):
        def publish_bytes(self, storage_key, content, *, validator=None):
            def fail(_path):
                raise OSError("simulated validation failure")

            return super().publish_bytes(storage_key, content, validator=fail)

    store = FailAfterStageStore(tmp_path)
    monkeypatch.setattr(agent_files, "get_artifact_store", lambda: store)

    owner = _verified_owner(db, "failure-owner")
    with pytest.raises(agent_files.FileError, match="发布失败"):
        agent_files.save_upload(
            b"not published", "failure.txt", owner
        )

    db.expire_all()
    artifact = (
        db.query(AgentArtifact)
        .filter(AgentArtifact.owner_sub == "failure-owner")
        .order_by(AgentArtifact.created_at.desc())
        .first()
    )
    assert artifact is not None and artifact.status == "failed"
    with pytest.raises(agent_files.FileError):
        agent_files.get_download(artifact.id, owner)
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.glob("*.txt"))


def test_local_store_concurrent_publish_never_overwrites_existing_key(tmp_path, monkeypatch):
    store = agent_files.LocalArtifactStore(tmp_path)
    storage_key = "objects/11111111-1111-4111-8111-111111111111.txt"
    final_path = store.path_for(storage_key)
    barrier = threading.Barrier(2)
    original_exists = type(final_path).exists

    def synchronized_exists(path):
        if path == final_path:
            barrier.wait(timeout=5)
            return False
        return original_exists(path)

    monkeypatch.setattr(type(final_path), "exists", synchronized_exists)

    def publish(payload: bytes):
        try:
            return store.publish_bytes(storage_key, payload)
        except Exception as exc:  # noqa: BLE001 - result is asserted below
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(publish, (b"first", b"second")))

    successes = [item for item in outcomes if isinstance(item, agent_files.StoredObject)]
    failures = [item for item in outcomes if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], agent_files.FileError)
    assert final_path.read_bytes() in {b"first", b"second"}


def test_non_xlsx_upload_validates_only_store_tracked_part(db, monkeypatch):
    seen_names = []
    real_validate = agent_files._validate_staged_file

    def capture(path, ext):
        seen_names.append(path.name)
        return real_validate(path, ext)

    monkeypatch.setattr(agent_files, "_validate_staged_file", capture)

    agent_files.save_upload(b"tracked", "tracked.txt", _verified_owner(db, "tracked-owner"))

    assert len(seen_names) == 1
    assert seen_names[0].startswith("artifact-")
    assert seen_names[0].endswith(".part")
    assert "upload-check" not in seen_names[0]


def test_non_ready_expired_and_integrity_mismatch_are_denied(db):
    owner = _verified_owner(db, "alice")
    failed = agent_files.save_upload(b"failed", "failed.txt", owner)
    expired = agent_files.save_upload(b"expired", "expired.txt", owner)
    corrupted = agent_files.save_upload(b"trusted", "trusted.txt", owner)

    failed_row = db.get(AgentArtifact, failed["artifact"]["id"])
    expired_row = db.get(AgentArtifact, expired["artifact"]["id"])
    assert failed_row is not None and expired_row is not None
    failed_row.status = "failed"
    expired_row.created_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    expired_row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    with pytest.raises(agent_files.FileError):
        agent_files.get_download(failed["file_id"], owner)
    with pytest.raises(agent_files.FileError):
        agent_files.preview(expired["file_id"], owner)

    corrupt_path, _ = agent_files.get_download(corrupted["file_id"], owner)
    corrupt_path.write_bytes(b"tampered")
    with pytest.raises(agent_files.FileError, match="完整性"):
        agent_files.get_download(corrupted["file_id"], owner)


def test_storage_key_tampering_cannot_escape_store_root(db, tmp_path):
    owner = _verified_owner(db, "alice")
    result = agent_files.save_upload(b"safe", "safe.txt", owner)
    artifact = db.get(AgentArtifact, result["artifact"]["id"])
    assert artifact is not None
    artifact.storage_key = "../outside-secret.txt"
    db.commit()
    (agent_files._dir().parent / "outside-secret.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(agent_files.FileError):
        agent_files.get_download(result["file_id"], owner)


def test_download_uses_real_mime_hash_length_and_safe_unicode_filename(db):
    client = TestClient(app)
    token = _login(db, client, "artifact_alice")
    raw_name = "../报价\r\nX-Evil: yes.txt"
    result = agent_files.save_upload(
        "报价".encode(), raw_name, _verified_owner(db, "artifact_alice")
    )

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
    upload = agent_files.save_upload(
        b"owner only", "owner.txt", _verified_owner(db, "artifact_owner")
    )
    paths = [
        f"/api/agent/files/{upload['file_id']}",
        f"/api/agent/files/{upload['artifact']['id']}",
        f"/api/agent/files/{upload['file_id']}/preview",
        f"/api/agent/files/{upload['artifact']['id']}/preview",
    ]

    for path in paths:
        assert client.get(path, headers={"Authorization": f"Bearer {owner_token}"}).status_code == 200
        admin_response = client.get(path, headers={"Authorization": f"Bearer {admin_token}"})
        boss_response = client.get(path, headers={"Authorization": f"Bearer {boss_token}"})
        assert admin_response.status_code == 404
        assert boss_response.status_code == 404
        assert admin_response.json() == boss_response.json() == {
            "detail": "文件不存在或无权访问"
        }


@pytest.mark.parametrize("payload", ["=1+1", "+SUM(A1:A2)", "-2+3", "@cmd", "  =HYPERLINK(\"x\")"])
def test_write_excel_neutralizes_formula_injection(db, payload):
    owner = _verified_owner(db, "alice")
    result = agent_files.write_excel(
        None,
        None,
        [{"row": 1, "col": "A", "value": payload}],
        "safe.xlsx",
        owner,
    )
    path, _ = agent_files.get_download(result["file_id"], owner)
    workbook = load_workbook(path, data_only=False)
    cell = workbook.active["A1"]
    assert cell.value == f"'{payload}"
    assert cell.data_type != "f"
    workbook.close()


def test_write_report_neutralizes_formula_injection_in_all_text_fields(db):
    owner = _verified_owner(db, "alice")
    result = agent_files.write_report(
        "=TITLE",
        ["@HEADER"],
        [["+ROW"]],
        "report.xlsx",
        owner,
    )
    path, _ = agent_files.get_download(result["file_id"], owner)
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
        salesperson_name="Alice Sales",
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
        _owner_from_context(db, creator),
    )
    artifact = db.get(AgentArtifact, result["artifact"]["id"])
    assert artifact is not None
    assert artifact.access_scope["policy"] == "current_scope_dominates"
    assert "data_purchase_cost" in artifact.access_scope["required_permissions"]
    assert "data_profit" in artifact.access_scope["required_permissions"]
    assert artifact.access_scope["data_permissions"]["data_profit"] is True
    assert artifact.access_scope["page_permissions"]["page_chat"] is True
    assert artifact.sensitivity == "high"
    assert "profit_amount" in artifact.access_scope["visible_field_groups"]
    assert agent_files.access_allowed(result["file_id"], creator) is True

    downgraded = security.UserContext(
        user_id="scoped-user",
        role="sales",
        salesperson_name="Alice Sales",
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
        "全量结果", ["值"], [[1]], "all.xlsx",
        _owner_from_context(db, creator),
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
        salesperson_name="Alice Sales",
        is_authenticated=True, authn="sys_user", has_stable_subject=True
    )
    own_result = agent_files.write_report(
        "本人结果", ["值"], [[1]], "own.xlsx",
        _owner_from_context(db, own_creator),
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


def test_own_customer_artifact_is_bound_to_canonical_salesperson_subject(db):
    own_permissions = permissions.effective("sales", None)

    def ctx(salesperson_name: str | None, *, own: bool = True):
        return security.UserContext(
            user_id="row-owner",
            role="sales",
            salesperson_name=salesperson_name,
            permissions={**own_permissions, "own_customers_only": own},
            is_authenticated=True,
            authn="sys_user",
            has_stable_subject=True,
        )

    creator_a = ctx("  Ａlice   Sales  ")
    artifact_a = agent_files.write_report(
        "本人客户", ["值"], [[1]], "own-a.xlsx",
        _owner_from_context(db, creator_a),
    )
    assert agent_files.artifact_info(artifact_a["file_id"])["status"] == "ready"
    assert agent_files.access_allowed(artifact_a["file_id"], ctx("Alice Sales")) is True
    assert agent_files.access_allowed(artifact_a["file_id"], ctx("Bob Sales")) is False
    assert agent_files.access_allowed(artifact_a["file_id"], ctx("Bob Sales", own=False)) is True

    creator_without_subject = ctx(None)
    with pytest.raises(agent_files.FileError, match="销售主体"):
        agent_files.snapshot_access_scope(creator_without_subject)


def test_upload_is_explicit_owner_only_even_if_data_permissions_change(db):
    upload = agent_files.save_upload(
        b"private input", "input.txt", _verified_owner(db, "alice")
    )
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


def test_public_generated_write_is_always_server_classified(db):
    result = agent_files.write_report(
        "classified", ["value"], [[1]], "classified.xlsx", _verified_owner(db, "alice")
    )
    owner = security.UserContext(
        user_id="alice", role="admin", permissions=permissions.effective("admin", None),
        is_authenticated=True, authn="sys_user", has_stable_subject=True,
    )
    artifact = db.get(AgentArtifact, result["file_id"])
    assert artifact is not None
    assert artifact.access_scope["policy"] == "current_scope_dominates"
    assert agent_files.access_allowed(result["file_id"], owner) is True


def test_v2_creation_rejects_missing_or_unauthenticated_owner(db):
    with pytest.raises(agent_files.FileError, match="已验证身份"):
        agent_files.save_upload(b"no owner", "owner.txt", None)
    with pytest.raises(agent_files.FileError, match="已验证身份"):
        agent_files.write_report("x", ["h"], [[1]], "x.xlsx", None)
    with pytest.raises(agent_files.FileError, match="已验证身份"):
        agent_files.save_upload(b"spoof", "spoof.txt", "alice")
    with pytest.raises(TypeError):
        agent_files.write_report(
            "forged", ["h"], [[1]], "forged.xlsx", _verified_owner(db, "alice"),
            access_scope={"policy": "owner_only"},
        )
    unauthenticated = security.UserContext(
        user_id="alice", role="sales", permissions=permissions.effective("sales", None)
    )
    with pytest.raises(agent_files.FileError, match="实名系统账号"):
        agent_files.snapshot_access_scope(unauthenticated)


def test_sensitive_file_service_methods_do_not_offer_actorless_reads(db):
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    owner = _verified_owner(db, "service-owner")
    artifact = agent_files.save_upload(buffer.getvalue(), "private.xlsx", owner)
    fid = artifact["file_id"]

    for call in (
        lambda: agent_files.inspect_file(fid),
        lambda: agent_files.read_rows(fid, None, 1, 10),
        lambda: agent_files.preview(fid),
        lambda: agent_files.read_document(fid),
        lambda: agent_files.get_download_info(fid),
        lambda: agent_files.get_download(fid),
    ):
        with pytest.raises(TypeError):
            call()


def test_verified_owner_is_derived_from_active_current_sys_user(db):
    current_permissions = permissions.effective("sales", None)
    user = SysUser(
        username="db-owner",
        role="sales",
        salesperson_name="Alice Sales",
        password_hash=hash_password("pw123456"),
        permissions=current_permissions,
        token_version=3,
        is_active=True,
    )
    db.add(user)
    db.commit()
    matching = security.UserContext(
        user_id="db-owner",
        role="sales",
        salesperson_name="Alice Sales",
        permissions=current_permissions,
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
        token_version=3,
    )
    forged = security.UserContext(
        user_id="db-owner",
        role="admin",
        permissions=permissions.effective("admin", None),
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
        token_version=3,
    )

    with pytest.raises(agent_files.FileError, match="当前账号事实"):
        agent_files.verified_artifact_owner(db, forged)

    owner = agent_files.verified_artifact_owner(db, matching)
    revoked_permissions = {key: False for key in permissions.ALL_KEYS}
    user.permissions = revoked_permissions
    user.token_version = 4
    db.commit()
    with pytest.raises(agent_files.FileError, match="当前账号事实"):
        agent_files.verified_artifact_owner(db, matching)
    with pytest.raises(agent_files.FileError, match="登录状态"):
        agent_files.save_upload(b"stale", "stale.txt", owner)

    current_after_revoke = security.UserContext(
        user_id="db-owner",
        role="sales",
        salesperson_name="Alice Sales",
        permissions=revoked_permissions,
        is_authenticated=True,
        authn="sys_user",
        has_stable_subject=True,
        token_version=4,
    )
    revoked_owner = agent_files.verified_artifact_owner(db, current_after_revoke)
    user.is_active = False
    db.commit()
    with pytest.raises(agent_files.FileError, match="登录状态"):
        agent_files.save_upload(b"revoked", "revoked.txt", revoked_owner)


def test_shared_or_unstable_subject_cannot_create_or_reopen_artifacts(db):
    upload = agent_files.save_upload(b"private", "private.txt", _verified_owner(db, "alice"))
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
    admin = SysUser(
        username="admin", role="admin", password_hash=hash_password("pw123456"),
        permissions=permissions.effective("admin", None),
    )
    db.add(admin)
    db.commit()
    existing = agent_files.save_upload(b"private", "private.txt", _verified_owner(db, "admin"))
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post(
        "/api/agent/upload",
        headers=headers,
        files={"file": ("private.txt", b"payload", "text/plain")},
    )

    assert response.status_code == 403, response.text
    denied = client.get(f"/api/agent/files/{existing['file_id']}", headers=headers)
    assert denied.status_code == 404
    assert denied.json() == {"detail": "文件不存在或无权访问"}
    assert db.query(AgentArtifact).filter(AgentArtifact.owner_sub == "admin").count() == 1


def test_upload_access_audit_never_records_raw_filename(db, monkeypatch):
    from app.api import agent as agent_api

    client = TestClient(app)
    token = _login(db, client, "audit_owner")
    other_token = _login(db, client, "audit_other")
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
    artifact_id = response.json()["file_id"]
    assert calls == [("upload", "agent_file", {
        "outcome": "success",
        "artifact_id": artifact_id,
        "size_bytes": len(b"payload"),
    })]
    assert secret_name not in repr(calls)

    calls.clear()
    denied = client.get(
        f"/api/agent/files/{artifact_id}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert denied.status_code == 404
    assert calls == [("download", "agent_file", {
        "outcome": "denied",
        "artifact_id": artifact_id,
        "reason_code": "not_found_or_forbidden",
    })]
    assert "payload" not in repr(calls)


def test_upload_identity_gate_denies_and_audits_before_reading_body(monkeypatch):
    from app.api import agent as agent_api

    class UnreadBody:
        filename = "customer-secret.txt"
        read_called = False

        async def read(self):
            self.read_called = True
            raise AssertionError("body must not be read")

    body = UnreadBody()
    shared = security.UserContext(
        user_id="shared-admin",
        role="admin",
        permissions=permissions.effective("admin", None),
        is_authenticated=True,
        authn="shared",
        has_stable_subject=False,
    )
    calls = []
    monkeypatch.setattr(
        agent_api,
        "record_access_log",
        lambda _ctx, action, resource, detail=None: calls.append(
            (action, resource, detail)
        ),
    )

    with pytest.raises(HTTPException) as caught:
        asyncio.run(agent_api.upload(file=body, db=None, role="admin", ctx=shared))

    assert caught.value.status_code == 403
    assert body.read_called is False
    assert calls == [("upload", "agent_file", {
        "outcome": "denied",
        "reason_code": "unstable_identity",
    })]
    assert body.filename not in repr(calls)


def test_download_audit_distinguishes_success_expired_and_missing_object(db, monkeypatch):
    from app.api import agent as agent_api

    client = TestClient(app)
    token = _login(db, client, "audit_lifecycle")
    headers = {"Authorization": f"Bearer {token}"}
    calls = []
    monkeypatch.setattr(
        agent_api,
        "record_access_log",
        lambda _ctx, action, resource, detail=None: calls.append(
            (action, resource, detail)
        ),
    )

    first = client.post(
        "/api/agent/upload", headers=headers,
        files={"file": ("first.txt", b"first-secret", "text/plain")},
    ).json()["file_id"]
    calls.clear()
    assert client.get(f"/api/agent/files/{first}", headers=headers).status_code == 200
    assert calls == [("download", "agent_file", {
        "outcome": "success", "artifact_id": first,
    })]

    row = db.get(AgentArtifact, first)
    assert row is not None
    row.status = "expired"
    db.commit()
    calls.clear()
    assert client.get(f"/api/agent/files/{first}", headers=headers).status_code == 404
    assert calls == [("download", "agent_file", {
        "outcome": "denied", "artifact_id": first, "reason_code": "expired",
    })]

    second = client.post(
        "/api/agent/upload", headers=headers,
        files={"file": ("second.txt", b"second-secret", "text/plain")},
    ).json()["file_id"]
    second_row = db.get(AgentArtifact, second)
    assert second_row is not None
    agent_files.get_artifact_store().path_for(second_row.storage_key).unlink()
    calls.clear()
    assert client.get(f"/api/agent/files/{second}", headers=headers).status_code == 404
    assert calls == [("download", "agent_file", {
        "outcome": "denied", "artifact_id": second, "reason_code": "object_missing",
    })]
    assert "first-secret" not in repr(calls)
    assert "second-secret" not in repr(calls)


def test_ready_commit_fault_after_object_write_preserves_reconcilable_object(db, monkeypatch):
    owner = "fault-ready-commit"
    monkeypatch.setattr(
        agent_files,
        "_mark_artifact_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("commit failed")),
    )

    with pytest.raises(agent_files.FileError, match="发布失败"):
        agent_files.save_upload(b"fault", "fault.txt", _verified_owner(db, owner))

    db.expire_all()
    artifact = db.query(AgentArtifact).filter(AgentArtifact.owner_sub == owner).one()
    assert artifact.status == "failed"
    assert not agent_files._meta_path(artifact.id).exists()
    assert agent_files.get_artifact_store().path_for(artifact.storage_key).read_bytes() == b"fault"


def test_reconciler_ready_interleaving_is_idempotent_and_never_deletes_object(
    db, tmp_path, monkeypatch
):
    """A reconciler may prove the object ready between publish and publisher CAS."""
    from app.db import SessionLocal
    from app.services import agent_artifact_reconcile

    now = datetime.now(timezone.utc)

    class ReconcileBeforeReturnStore(agent_files.LocalArtifactStore):
        def publish_bytes(self, storage_key, content, *, validator=None):
            stored = super().publish_bytes(storage_key, content, validator=validator)
            with SessionLocal.begin() as session:
                row = session.query(AgentArtifact).filter_by(storage_key=storage_key).one()
                row.created_at = now - timedelta(hours=2)
            result = agent_artifact_reconcile.reconcile_agent_artifacts(
                apply=True,
                grace_period=timedelta(hours=1),
                session_factory=SessionLocal,
                now=now,
                artifact_root=self.root,
            )
            assert result["applied"]["recover_ready"] == 1
            return stored

    store = ReconcileBeforeReturnStore(tmp_path)
    monkeypatch.setattr(agent_files, "get_artifact_store", lambda: store)

    result = agent_files.save_upload(
        b"durable interleaving", "interleaving.txt", _verified_owner(db, "race-owner")
    )

    db.expire_all()
    artifact = db.get(AgentArtifact, result["file_id"])
    assert artifact is not None and artifact.status == "ready"
    assert store.path_for(artifact.storage_key).read_bytes() == b"durable interleaving"


def test_legacy_short_id_sidecar_and_url_remain_readable(db):
    owner = _verified_owner(db, "alice")
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

    path, filename = agent_files.get_download(legacy_id, owner)

    assert path.read_bytes() == b"legacy"
    assert filename == "legacy.txt"
    assert agent_files._owner_of_unchecked(legacy_id) == "alice"


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
