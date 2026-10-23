"""Exercise actual two-app HTTPS delegation, PostgreSQL transactions and current PDP.

Never use a production URL: both HTTP origins are fixed loopback and the only DB
allowed is the dedicated synthetic acceptance DB. No mocked authority or token.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import ssl
from threading import Barrier
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    tls = ssl.create_default_context(cafile=config["tls_cert"])
    itsp = httpx.Client(base_url="https://127.0.0.1:28481", verify=tls, trust_env=False, timeout=20)
    shop = httpx.Client(base_url="http://127.0.0.1:28482", trust_env=False, timeout=20)
    password = {"username": "pn-local-editor", "password": config["password"]}
    report = {"checks": [], "production_access": False, "transport": "real loopback HTTPS", "media_providers": "not called"}
    def checked(name, value):
        assert value, name
        report["checks"].append(name)
        print("PASS " + name, flush=True)
    login = itsp.post("/api/auth/login", json=password)
    assert login.status_code == 200, login.text
    itsp.headers["Authorization"] = "Bearer " + login.json()["token"]
    login = shop.post("/api/auth/login", json=password)
    assert login.status_code == 200, login.text
    shop.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    checked("both applications authenticate real local accounts", login.json()["shared_pn"]["writable"])
    created = shop.post("/api/shared-pn/parts", json={"pn_display": "NET-" + str(uuid4())[:8]},
                        headers={"Idempotency-Key": str(uuid4())})
    assert created.status_code == 201, created.text
    pid = created.json()["part_id"]
    report["part_id"] = pid
    report["shop_pn_id"] = created.json()["pn_id"]
    path = f"/api/pn-catalog/v1/parts/{pid}"
    first = itsp.get(path)
    assert first.status_code == 200, first.text
    baseline = first.json()["data"]
    checked("Shop creates one authoritative PN and an exact linked empty inventory shell", baseline["part_id"] == pid)
    paths = [path, path + "/access", path + "/content/good", path + "/content/bad",
             path + "/attributes", path + "/gallery?status=gallery&limit=50",
             path + "/gallery?status=candidate&limit=50", "/api/pn-catalog/v1/capabilities"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        parallel = list(pool.map(shop.get, paths))
    assert all(reply.status_code == 200 for reply in parallel), [(p, r.status_code, r.text[:400]) for p, r in zip(paths, parallel) if r.status_code != 200]
    checked("eight concurrent page requests authenticate and load independently", True)
    def patch(client, key, changes, versions):
        return client.patch(path + "/profile", json={"expected_identity_revision": baseline["identity_revision"],
            "field_versions": versions, "changes": changes}, headers={"Idempotency-Key": str(key)})
    request_id = uuid4()
    changed = patch(shop, request_id, {"description": "Shop verified description"}, {"description": 0})
    assert changed.status_code == 200, changed.text
    checked("Shop write is immediately visible through ITSP", itsp.get(path).json()["data"]["profile"]["description"] == "Shop verified description")
    repeated = patch(shop, request_id, {"description": "Shop verified description"}, {"description": 0})
    checked("same Shop request replays the original audit and applied version", repeated.status_code == 200 and repeated.json()["meta"]["replayed"] and repeated.json()["meta"]["audit_id"] == changed.json()["meta"]["audit_id"])
    mismatch = patch(shop, request_id, {"description": "changed retry"}, {"description": 0})
    checked("reused request key with another body is rejected", mismatch.status_code == 409 and mismatch.json()["error"]["code"] == "request.idempotency_mismatch")
    barrier = Barrier(2)
    def competing(client, label):
        barrier.wait(timeout=5)
        return patch(client, uuid4(), {"brand": label}, {"brand": 0})
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(competing, client, label) for client, label in ((itsp, "ITSP"), (shop, "Shop"))]
        results = [task.result() for task in tasks]
    checked("concurrent same-field writers produce one success and one conflict", sorted(r.status_code for r in results) == [200, 409])
    barrier = Barrier(2)
    def independent(client, field):
        barrier.wait(timeout=5)
        return patch(client, uuid4(), {field: field + " value"}, {field: 0})
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(independent, itsp, "manufacturer"), pool.submit(independent, shop, "unit")]
        results = [task.result() for task in tasks]
    assert all(r.status_code == 200 for r in results), [(r.status_code, r.text) for r in results]
    checked("concurrent different-field changes are preserved", all(r.status_code == 200 for r in results))
    for grade, client in (("good", itsp), ("bad", shop)):
        result = client.patch(path + "/content/" + grade, json={"expected_identity_revision": baseline["identity_revision"],
            "expected_revision": 0, "changes": {"title": grade, "description": grade + " generic description"}},
            headers={"Idempotency-Key": str(uuid4())})
        assert result.status_code == 200, result.text
    checked("good and bad descriptions are shared but independently versioned", shop.get(path + "/content/good").json()["data"]["description"] == "good generic description" and itsp.get(path + "/content/bad").json()["data"]["description"] == "bad generic description")
    result = shop.patch(path + "/attributes", json={"expected_identity_revision": baseline["identity_revision"],
        "field_versions": {"custom": 0}, "changes": {"custom": {"operation": "set", "value": {"null": None, "typed": [True, 2, "文字"]}}}},
        headers={"Idempotency-Key": str(uuid4())})
    assert result.status_code == 200, result.text
    checked("typed custom attributes survive both transports", itsp.get(path + "/attributes").json()["data"]["items"]["custom"]["value"] == {"null": None, "typed": [True, 2, "文字"]})
    url = make_url(config["itsp_database"])
    assert url.host == "127.0.0.1" and url.database.startswith("pn_local_accept_")
    engine = create_engine(url)
    with engine.connect() as db:
        user_id = db.scalar(text("SELECT id FROM sys_user WHERE username='pn-local-editor'"))
        rows = db.execute(text("SELECT client_id, principal_id, actor FROM pn_catalog_audit WHERE part_id=:id"), {"id": pid}).mappings().all()
    checked("both entrypoints audit the same mapped person with distinct client provenance", {r["client_id"] for r in rows} == {"itsp", "shop"} and all(r["principal_id"] == f"account:{user_id}" for r in rows))
    with engine.begin() as db:
        original = db.scalar(text("SELECT actions FROM sys_authorization_grant WHERE user_id=:uid AND resource='part_master'"), {"uid": user_id})
        db.execute(text("UPDATE sys_authorization_grant SET actions='[\"read\"]'::jsonb, version=version+1 WHERE user_id=:uid AND resource='part_master'"), {"uid": user_id})
    try:
        revoked = patch(shop, request_id, {"description": "Shop verified description"}, {"description": 0})
        checked("current revocation blocks an otherwise valid prior write receipt replay", revoked.status_code == 403)
    finally:
        with engine.begin() as db:
            db.execute(text("UPDATE sys_authorization_grant SET actions=CAST(:actions AS jsonb), version=version+1 WHERE user_id=:uid AND resource='part_master'"), {"uid": user_id, "actions": json.dumps(original)})
    result = shop.get("/api/shared-pn/master/" + str(pid))
    checked("local inventory link remains exact after all shared writes", result.status_code == 200 and result.json()["pn_id"] == report["shop_pn_id"])
    (args.config.parent / "network-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    itsp.close()
    shop.close()
    engine.dispose()


if __name__ == "__main__":
    main()
