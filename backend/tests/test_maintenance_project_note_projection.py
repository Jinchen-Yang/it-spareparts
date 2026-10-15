"""Card summaries omit reminder text while project details keep it."""

from tests.test_maintenance_project_management_api import _admin_client


def test_cards_omit_long_note_but_overview_and_workspace_keep_it(db):
    client = _admin_client(db, "note_projection_admin")
    created = client.post("/api/maintenance/projects/stable", json={
        "project_code": "NOTE-PROJECTION", "display_name": "提醒投影项目",
        "reason": "建立合成项目",
    })
    assert created.status_code == 201, created.text
    pid = created.json()["project_id"]
    note = "中" * 2000
    changed = client.patch(f"/api/maintenance/projects/stable/{pid}", json={
        "version": 1, "no_return_note": note, "reason": "维护收货提醒",
    })
    assert changed.status_code == 200, changed.text

    cards = client.get("/api/maintenance/projects/stable/operations")
    assert cards.status_code == 200, cards.text
    assert [card["project_id"] for card in cards.json()["rows"]] == [pid]
    assert "no_return_note" not in cards.json()["rows"][0]

    for path in (f"/{pid}", f"/{pid}/workspace"):
        detail = client.get(f"/api/maintenance/projects/stable{path}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["project"]["no_return_note"] == note
    directory = client.get("/api/maintenance/projects/stable")
    assert directory.status_code == 200, directory.text
    assert directory.json()["rows"][0]["no_return_note"] == note
