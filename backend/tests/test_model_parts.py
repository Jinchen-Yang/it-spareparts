"""双端页面的数据契约：仅在本仓 PostgreSQL 隔离测试库运行。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app.auth import _make_token
from app.config import get_settings
from app.main import app
from app.models.dimensions import DimPart, PartAlias
from app.models.system import SysUser

URL = "/api/model-parts"
EXPORT = "/api/integrations/model-parts/v1/catalog"
TOKEN = "model-parts-independent-synthetic-catalog-only"


def headers(role, name, *, read=True, write=False, fallback=False):
    token, _ = _make_token(
        role,
        name,
        name,
        fallback=fallback,
        authn="sys_user" if not fallback else None,
        perms={"page_parts": read, "page_master_data": write},
    )
    return {"Authorization": "Bearer " + token}


@pytest.fixture()
def setup(db, monkeypatch):
    db.add_all(
        [
            SysUser(
                username=name,
                role=role,
                password_hash="deliberately-unused",
                permissions={"page_parts": read, "page_master_data": write},
            )
            for name, role, read, write in [
                ("catalog-editor", "purchaser", True, True),
                ("catalog-reader", "readonly", True, False),
                ("catalog-hidden", "readonly", False, False),
            ]
        ]
    )
    a = DimPart(pn_std="TEST-P16", description="1600W 合成电源", category_major="电源")
    b = DimPart(pn_std="TEST-FAN", description="合成风扇")
    extra = DimPart(pn_std="OTHER-SYNTH")
    db.add_all([a, b, extra])
    db.flush()
    db.add(PartAlias(part_id=a.id, pn_raw="LAB-16", pn_std=a.pn_std, source="manual", status="active"))
    ids = str(a.id), str(b.id)
    db.commit()
    monkeypatch.setattr(
        get_settings(),
        "model_parts_catalog_token_sha256",
        SecretStr(hashlib.sha256(TOKEN.encode()).hexdigest()),
        raising=False,
    )
    with TestClient(app) as client:
        yield client, headers("purchaser", "catalog-editor", write=True), ids


def command(action, rev, **fields):
    return {"requestId": str(uuid4()), "expectedRevision": rev, "action": action, **fields}


def submit(client, h, cmd):
    res = client.post(URL + "/commands", headers=h, json=cmd)
    assert res.status_code == 200, res.text
    return res.json()


def row(i, raw, candidates, **kw):
    return {
        "line": i,
        "rawPn": raw,
        "position": "电源仓",
        "condition": "1600W配置",
        "source": "合成料表 V1",
        "candidateIds": candidates,
        **kw,
    }


def test_native_and_service_permissions(setup):
    client, editor, _ = setup
    reader = headers("readonly", "catalog-reader")
    weak = headers("admin", "catalog-reader", fallback=True)
    assert client.get(URL + "/catalog").status_code == 401
    assert (
        client.get(URL + "/catalog", headers=headers("readonly", "catalog-hidden", read=False)).status_code
        == 403
    )
    assert client.get(URL + "/catalog", headers=reader).json()["canMaintain"] is False
    assert client.get(URL + "/catalog", headers=editor).json()["canMaintain"] is True
    op = command("create", 0, brand="H3C", name="R6900 G3", aliases=[])
    for h in [reader, weak, {"Authorization": "Bearer " + TOKEN}]:
        assert client.post(URL + "/commands", headers=h, json=op).status_code in (401, 403)
    assert client.get(EXPORT, headers=editor).status_code == 401
    exported = client.get(EXPORT, headers={"Authorization": "Bearer " + TOKEN})
    assert exported.status_code == 200 and exported.headers["cache-control"] == "no-store"
    assert exported.json() == {
        "version": 1,
        "revision": 0,
        "models": [],
        "members": [],
        "parts": [],
        "pending": [],
    }


def test_catalog_memberships_and_provenance(setup):
    client, h, (a, b) = setup
    create = command("create", 0, brand="新华三", name="R6900 G3", aliases=["6900G3"])
    first = submit(client, h, create)
    assert submit(client, h, create) == first
    mid = first["modelId"]
    assert client.post(URL + "/commands", headers=h, json={**create, "name": "Other"}).status_code == 409
    duplicate = command("create", 1, brand="H3C", name="6900 G3", aliases=[])
    assert client.post(URL + "/commands", headers=h, json=duplicate).status_code == 409
    matches = client.post(
        URL + "/candidates", headers=h, json={"pns": ["lab-16", "TEST-P16 V1", "unknown"]}
    ).json()["parts"]
    assert [r["id"] for r in matches] == [a] and matches[0]["aliases"] == ["LAB-16"]
    assert client.post(URL + "/candidates", headers=h, json={"pns": ["TEST-P16 V1"]}).json() == {
        "parts": [],
        "matches": {"test-p16 v1": []},
    }
    op = command(
        "import",
        1,
        modelId=mid,
        rows=[
            row(2, "LAB-16", [a], partId=a),
            row(3, "TEST-P16", [a], partId=a),
            row(4, "TEST-FAN", [], reviewDecision="排除"),
            row(5, "现场样号", []),
        ],
    )
    assert submit(client, h, op) == {"revision": 2, "added": 1, "pending": 1, "skipped": 2}
    native = client.get(URL + "/catalog", headers=h).json()
    assert native["stockStatus"] == "unavailable"
    doc = native["catalog"]
    assert doc["revision"] == 2 and [p["id"] for p in doc["parts"]] == [a]
    assert doc["members"][0]["rawPns"] == ["LAB-16"] and doc["parts"][0]["good"] is None
    pending = doc["pending"][0]["id"]
    submit(client, h, command("resolve", 2, pendingId=pending, partId=a, source="售后手册第7页"))
    updated = client.get(EXPORT, headers={"Authorization": "Bearer " + TOKEN}).json()
    assert updated["pending"] == [] and updated["members"][0]["rawPns"] == ["LAB-16", "现场样号"]
    assert "售后手册" in updated["members"][0]["source"]
    assert submit(client, h, command("import", 3, modelId=mid, rows=[row(2, "现场样号", [])]))["skipped"] == 1
    second = submit(client, h, command("create", 3, brand="新华三", name="R6900 G5", aliases=[]))["modelId"]
    submit(client, h, command("import", 4, modelId=second, rows=[row(1, "TEST-P16", [a], partId=a)]))
    to_remove = next(
        m for m in client.get(URL + "/catalog", headers=h).json()["catalog"]["members"] if m["modelId"] == mid
    )
    submit(
        client,
        h,
        command("remove", 5, modelId=mid, partId=a, expectedMembershipVersion=to_remove["membershipVersion"]),
    )
    final = client.get(URL + "/catalog", headers=h).json()["catalog"]
    assert len(final["parts"]) == len(final["members"]) == 1 and final["members"][0]["modelId"] == second


def test_stale_preview_does_not_write_partial_import(setup):
    client, h, (a, b) = setup
    mid = submit(client, h, command("create", 0, brand="新品牌", name="X", aliases=[]))["modelId"]
    mismatch = command(
        "import", 1, modelId=mid, rows=[row(1, "TEST-P16", [a], partId=a), row(2, "TEST-FAN", [])]
    )
    assert client.post(URL + "/commands", headers=h, json=mismatch).status_code == 409
    res = client.get(URL + "/catalog", headers=h).json()["catalog"]
    assert res["revision"] == 1 and not res["members"] and not res["pending"]


def test_concurrent_create_and_replay(setup):
    client, h, _ = setup
    one = command("create", 0, brand="品牌", name="T1", aliases=[])

    def call(cmd):
        return client.post(URL + "/commands", headers=h, json=cmd)

    with ThreadPoolExecutor(2) as pool:
        replay = list(pool.map(call, [one, one]))
    assert [r.status_code for r in replay] == [200, 200] and replay[0].json() == replay[1].json()
    with ThreadPoolExecutor(2) as pool:
        replies = list(
            pool.map(call, [command("create", 1, brand="品牌", name=n, aliases=[]) for n in ["T2", "T3"]])
        )
    assert sorted(r.status_code for r in replies) == [200, 409]


def test_exact_pn_normalization_agrees_with_browser_across_database_locale(setup, db):
    client, h, (_, b) = setup
    part = DimPart(pn_std="İPN")
    db.add(part)
    db.flush()
    a = str(part.id)
    for raw in ["ΣΟΣ", "\tWH-17\t", "\u0085edge"]:
        db.add(PartAlias(part_id=int(b), pn_raw=raw, pn_std="TEST-FAN", status="active"))
    db.commit()
    pns = ["i\u0307pn", "ipn", "σος", "σοσ", "\ufeffWH-17", "\u0085edge", "edge"]
    data = client.post(URL + "/candidates", headers=h, json={"pns": pns}).json()
    assert data["matches"] == {
        "i\u0307pn": [a],
        "ipn": [],
        "σος": [b],
        "σοσ": [],
        "wh-17": [b],
        "\u0085edge": [b],
        "edge": [],
    }
    # A batch containing only ASCII keys must also reject a locale-dependent false positive.
    assert client.post(URL + "/candidates", headers=h, json={"pns": ["IPN"]}).json()["parts"] == []


def test_ambiguous_fullwidth_aliases_require_choice_and_partial_decisions_are_skipped(setup, db):
    client, h, (a, b) = setup
    for raw, pid in [("LAB-DUAL", a), ("ＬＡＢ－ＤＵＡＬ", b)]:
        part = db.get(DimPart, int(pid))
        db.add(PartAlias(part_id=part.id, pn_raw=raw, pn_std=part.pn_std, status="active"))
    db.commit()
    ids = sorted([a, b])
    mid = submit(client, h, command("create", 0, brand="H3C", name="T", aliases=[]))["modelId"]
    rows = [
        row(1, "lab-dual", ids),
        row(2, "TEST-FAN", [b], reviewDecision="待核对"),
        row(3, "TEST-FAN", [b], reviewDecision=None),
    ]
    result = submit(client, h, command("import", 1, modelId=mid, rows=rows))
    assert (result["added"], result["pending"], result["skipped"]) == (0, 1, 2)
    pending = client.get(URL + "/catalog", headers=h).json()["catalog"]["pending"][0]
    assert pending["reason"] == "ambiguous" and sorted(pending["candidateIds"]) == ids
    submit(client, h, command("resolve", 2, pendingId=pending["id"], partId=a, source="合成核查"))
    same = submit(client, h, command("import", 3, modelId=mid, rows=[row(1, "LAB-DUAL", ids)]))
    assert same == {"revision": 3, "added": 0, "pending": 0, "skipped": 1}
    other = submit(client, h, command("import", 3, modelId=mid, rows=[row(1, "LAB-DUAL", ids, partId=b)]))
    assert other["added"] == 1


def test_catalog_follows_rename_merge_and_unmerge_without_losing_evidence(setup, db):
    from app.models.model_parts import MachineModelCommand

    client, h, (a, b) = setup
    mid = submit(client, h, command("create", 0, brand="H3C", name="T", aliases=[]))["modelId"]
    submit(
        client, h, command("import", 1, modelId=mid, rows=[row(1, "LAB-16", [a]), row(2, "TEST-FAN", [b])])
    )
    first, second = db.get(DimPart, int(a)), db.get(DimPart, int(b))
    first.status = "merged"
    first.merged_into_id = second.id
    db.commit()
    resp = client.get(URL + "/catalog", headers=h).json()["catalog"]
    assert len(resp["members"]) == len(resp["parts"]) == 1
    assert resp["members"][0]["partId"] == b
    assert set(resp["members"][0]["rawPns"]) == {"LAB-16", "TEST-FAN"}
    first.status = "active"
    first.merged_into_id = None
    second.pn_std = "TEST-FAN-RENAMED"
    db.commit()
    doc = client.get(URL + "/catalog", headers=h).json()["catalog"]
    assert {r["pn"] for r in doc["parts"]} == {"TEST-P16", "TEST-FAN-RENAMED"}
    assert len(doc["members"]) == 2
    token = next(m["membershipVersion"] for m in doc["members"] if m["partId"] == b)
    removed = command("remove", 2, modelId=mid, partId=b, expectedMembershipVersion=token)
    submit(client, h, removed)
    record = db.scalar(select(MachineModelCommand).where(MachineModelCommand.action == "remove"))
    assert record.actor == "catalog-editor"
    assert record.before["members"][0]["rawPns"] == ["TEST-FAN"] and record.result["revision"] == 3


def test_credentials_cannot_be_reused_and_no_incomplete_credentials_open_export(setup, monkeypatch):
    client, _, _ = setup
    for digest, raw in [("", ""), (hashlib.sha256(TOKEN.encode()).hexdigest(), TOKEN)]:
        monkeypatch.setattr(get_settings(), "model_parts_catalog_token_sha256", SecretStr(digest))
        monkeypatch.setattr(get_settings(), "model_parts_shop_stock_token", SecretStr(raw))
        assert client.get(EXPORT, headers={"Authorization": "Bearer " + TOKEN}).status_code == 503


def test_reused_request_id_distinguishes_missing_review_column_from_empty_result(setup):
    client, h, (a, _) = setup
    mid = submit(client, h, command("create", 0, brand="A", name="T", aliases=[]))["modelId"]
    source = row(1, "TEST-P16", [a])
    op = command("import", 1, modelId=mid, rows=[{**source, "reviewDecision": None}])
    assert submit(client, h, op)["added"] == 0
    assert client.post(URL + "/commands", headers=h, json={**op, "rows": [source]}).status_code == 409


def test_existing_integration_keys_cannot_be_assigned_to_model_parts():
    from pydantic import ValidationError
    from app.config import Settings

    digest = hashlib.sha256(TOKEN.encode()).hexdigest()
    cases = [
        {"model_parts_catalog_token_sha256": digest, "pn_master_integration_token_sha256": digest},
        {"model_parts_shop_stock_token": TOKEN, "resale_catalog_token_sha256": digest},
        {"model_parts_shop_stock_token": TOKEN, "shop_gallery_token": TOKEN},
    ]
    for fields in cases:
        with pytest.raises(ValidationError, match="独立凭据"):
            Settings(_env_file=None, **fields)


def test_pending_returns_current_candidates_and_rechecks_before_confirming(setup, db):
    client, h, (a, b) = setup
    mid = submit(client, h, command("create", 0, brand="A", name="T", aliases=[]))["modelId"]
    submit(client, h, command("import", 1, modelId=mid, rows=[row(1, "LOOSE-RAW", [])]))
    known = db.get(DimPart, int(a))
    db.add(PartAlias(part_id=known.id, pn_raw="LOOSE-RAW", pn_std=known.pn_std, status="active"))
    db.commit()
    doc = client.get(URL + "/catalog", headers=h).json()["catalog"]
    pending = doc["pending"][0]
    assert pending["candidateIds"] == [a] and [p["id"] for p in doc["parts"]] == [a]
    stale = command("resolve", 2, pendingId=pending["id"], partId=b, source="合成证据")
    assert client.post(URL + "/commands", headers=h, json=stale).status_code == 409
    submit(client, h, command("resolve", 2, pendingId=pending["id"], partId=a, source="合成证据"))


def test_confirmation_during_master_merge_stays_with_selected_identity_on_undo(setup, db):
    client, h, (a, b) = setup
    mid = submit(client, h, command("create", 0, brand="A", name="T"))["modelId"]
    submit(client, h, command("import", 1, modelId=mid, rows=[row(1, "LAB-16", [a]), row(2, "FIELD", [])]))
    first = db.get(DimPart, int(a))
    first.status, first.merged_into_id = "merged", int(b)
    db.commit()
    doc = client.get(URL + "/catalog", headers=h).json()["catalog"]
    submit(
        client, h, command("resolve", 2, pendingId=doc["pending"][0]["id"], partId=b, source="指定规范 PN")
    )
    first.status, first.merged_into_id = "active", None
    db.commit()
    after = client.get(URL + "/catalog", headers=h).json()["catalog"]
    assert {m["partId"]: m["rawPns"] for m in after["members"]} == {a: ["LAB-16"], b: ["FIELD"]}


def test_stale_remove_cannot_remove_a_newly_merged_origin(setup, db):
    client, h, (a, b) = setup
    mid = submit(client, h, command("create", 0, brand="A", name="T"))["modelId"]
    submit(
        client, h, command("import", 1, modelId=mid, rows=[row(1, "LAB-16", [a]), row(2, "TEST-FAN", [b])])
    )
    old = client.get(URL + "/catalog", headers=h).json()["catalog"]
    old_token = next(m["membershipVersion"] for m in old["members"] if m["partId"] == b)
    source = db.get(DimPart, int(a))
    source.status, source.merged_into_id = "merged", int(b)
    db.commit()
    changed = client.get(EXPORT, headers={"Authorization": "Bearer " + TOKEN}).json()
    token = changed["members"][0]["membershipVersion"]
    assert changed["revision"] == old["revision"] and token != old_token
    stale = command("remove", 2, modelId=mid, partId=b, expectedMembershipVersion=old_token)
    assert client.post(URL + "/commands", headers=h, json=stale).status_code == 409
    bare = command("remove", 2, modelId=mid, partId=b)
    assert client.post(URL + "/commands", headers=h, json=bare).status_code == 422
    submit(client, h, command("remove", 2, modelId=mid, partId=b, expectedMembershipVersion=token))
    assert not client.get(URL + "/catalog", headers=h).json()["catalog"]["members"]


def test_candidates_explicitly_map_merged_standard_name_even_if_active_alias_means_another_part(setup, db):
    client, h, (a, b) = setup
    first, second = db.get(DimPart, int(a)), db.get(DimPart, int(b))
    first.status, first.merged_into_id = "merged", second.id
    other = db.scalar(select(DimPart).where(DimPart.pn_std == "OTHER-SYNTH"))
    db.add(PartAlias(pn_raw=first.pn_std, pn_std=other.pn_std, part_id=other.id, status="active"))
    db.commit()
    result = client.post(
        URL + "/candidates", headers=h, json={"pns": ["ＴＥＳＴ－Ｐ１６", "OTHER-SYNTH", "未知", "  "]}
    ).json()
    assert result["matches"] == {"test-p16": [b], "other-synth": [str(other.id)], "未知": [], "": []}
    assert {p["id"] for p in result["parts"]} == {b, str(other.id)}
    assert "TEST-P16" not in next(p for p in result["parts"] if p["id"] == b)["aliases"]


def test_revoked_ambiguous_alias_choices_do_not_confine_pending_forever(setup, db):
    client, h, (a, b) = setup
    rows = [
        PartAlias(pn_raw=raw, pn_std=db.get(DimPart, int(pid)).pn_std, part_id=int(pid), status="active")
        for raw, pid in [("AMB-OLD", a), ("ＡＭＢ－ＯＬＤ", b)]
    ]
    db.add_all(rows)
    db.commit()
    mid = submit(client, h, command("create", 0, brand="X", name="T"))["modelId"]
    submit(client, h, command("import", 1, modelId=mid, rows=[row(1, "AMB-OLD", [a, b])]))
    for value in rows:
        value.status = "rejected"
    db.commit()
    view = client.get(URL + "/catalog", headers=h).json()["catalog"]
    p = view["pending"][0]
    assert p["reason"] == "unknown" and p["candidateIds"] == [] and view["parts"] == []
    other = db.scalar(select(DimPart.id).where(DimPart.pn_std == "OTHER-SYNTH"))
    submit(client, h, command("resolve", 2, pendingId=p["id"], partId=str(other), source="独立核实"))


def test_an_existing_pending_row_does_not_consume_valid_standard_pn_later_in_batch(setup, db):
    client, h, (a, _) = setup
    mid = submit(client, h, command("create", 0, brand="X", name="T"))["modelId"]
    submit(client, h, command("import", 1, modelId=mid, rows=[row(1, "WAS-UNKNOWN", [])]))
    part = db.get(DimPart, int(a))
    db.add(PartAlias(pn_raw="WAS-UNKNOWN", pn_std=part.pn_std, part_id=part.id, status="active"))
    db.commit()
    result = submit(
        client,
        h,
        command("import", 2, modelId=mid, rows=[row(1, "WAS-UNKNOWN", [a]), row(2, "TEST-P16", [a])]),
    )
    assert result == {"added": 1, "pending": 0, "skipped": 1, "revision": 3}
