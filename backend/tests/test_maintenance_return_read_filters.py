"""只读品类筛选先于分页，筛出行不能缩小整单作废的提示范围。"""

from uuid import uuid4

from app.models.dimensions import DimPart
from tests.test_maintenance_project_master_v2_editable import _append_site_line, _site_issue
from tests.test_site_issue_v2_api import _client, _project
from tests.test_maintenance_return_receipts_api import _client as _receipt_client


def test_site_category_filter_pages_headers_and_returns_only_matching_lines(db):
    project = _project(db, project_id=str(uuid4()))
    disk = DimPart(pn_std="CURRENT-DISK", category_major="硬盘", category_minor="SAS")
    ram = DimPart(pn_std="CURRENT-RAM", category_major="内存", category_minor="DDR4")
    db.add_all([disk, ram])
    db.commit()
    first, disk_line = _site_issue(db, project, disk, issue_no="FILTER-A")
    _append_site_line(db, first, ram)
    second, _ = _site_issue(db, project, disk, issue_no="FILTER-B")
    _site_issue(db, project, ram, issue_no="UNMATCHED-NEWEST")
    client = _client(db)

    response = client.post("/api/maintenance/site-issues/search", json={
        "project_id": project.project_id, "workflow_statuses": ["confirmed"],
        "category_major": "硬盘", "category_minor": "SAS", "page_size": 1,
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 2  # 单数，不能变成行数或本页筛选后的单数。
    assert payload["rows"][0]["issue_id"] == second.issue_id
    response = client.post("/api/maintenance/site-issues/search", json={
        "project_id": project.project_id, "workflow_statuses": ["confirmed"],
        "category_major": "硬盘", "category_minor": "SAS", "page": 2, "page_size": 1,
    })
    row = response.json()["rows"][0]
    assert row["issue_id"] == first.issue_id
    assert [line["issue_line_id"] for line in row["lines"]] == [disk_line.issue_line_id]
    assert row["total_line_count"] == 2


def test_receipt_category_filter_counts_before_paging_and_keeps_unknown_rows(db):
    project = _project(db, project_id=str(uuid4()))
    parts = [
        DimPart(pn_std="RECEIPT-DISK", category_major="硬盘", category_minor="SAS"),
        DimPart(pn_std="RECEIPT-RAM", category_major="内存", category_minor="DDR4"),
        DimPart(pn_std="RECEIPT-UNKNOWN", category_major="硬盘", category_minor="   "),
    ]
    db.add_all(parts)
    db.commit()
    client = _receipt_client(db, username="category-reader")
    base = f"/api/maintenance/projects/stable/{project.project_id}/return-receipts"
    ids = []
    for part in [parts[0], parts[0], parts[1], parts[2], None]:
        payload = {"pn": part.pn_std if part else "UNLINKED-PN", "qty": 1}
        if part:
            payload["part_id"] = part.id
        response = client.post(base, json=payload)
        assert response.status_code == 201, response.text
        ids.append(response.json()["receipt_id"])
    response = client.get(base, params={
        "category_major": "硬盘", "category_minor": "SAS", "page_size": 1,
    })
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    page2 = client.get(base, params={
        "category_major": "硬盘", "category_minor": "SAS", "page_size": 1, "page": 2,
    }).json()
    assert {response.json()["items"][0]["receipt_id"], page2["items"][0]["receipt_id"]} == set(ids[:2])
    unknown = client.get(base, params={"category_state": "unclassified"}).json()
    assert [item["receipt_id"] for item in unknown["items"]] == [ids[3]]
    unlinked = client.get(base, params={"category_state": "unlinked"}).json()
    assert [item["receipt_id"] for item in unlinked["items"]] == [ids[4]]
    assert client.get(base, params={"category_state": "invalid"}).status_code == 422
    assert client.get(base, params={"category_state": "unlinked", "category_major": "硬盘"}).status_code == 422


def test_site_unknown_classification_and_inactive_matching_lines_keep_read_scope(db):
    project = _project(db, project_id=str(uuid4()))
    disk = DimPart(pn_std="INACTIVE-DISK", category_major="硬盘", category_minor="SAS")
    unknown = DimPart(pn_std="UNKNOWN-MINOR", category_major="硬盘")
    ram = DimPart(pn_std="UNCHANGED-RAM", category_major="内存", category_minor="DDR4")
    db.add_all([disk, unknown, ram])
    db.commit()
    issue, active = _site_issue(db, project, unknown, issue_no="UNKNOWN-ISSUE")
    _append_site_line(db, issue, ram)
    _, inactive = _site_issue(db, project, disk, issue_no="INACTIVE-MATCH")
    inactive.is_active = False
    db.commit()
    client = _client(db)
    base = {"project_id": project.project_id, "workflow_statuses": ["confirmed"]}
    response = client.post("/api/maintenance/site-issues/search", json=base)
    row = next(row for row in response.json()["rows"] if row["issue_id"] == issue.issue_id)
    assert len(row["lines"]) == 2  # 不传筛选保留整单行集合。
    selected = client.post("/api/maintenance/site-issues/search", json={
        **base, "category_major": "硬盘", "category_state": "unclassified",
    }).json()
    assert selected["total"] == 1
    assert [line["issue_line_id"] for line in selected["rows"][0]["lines"]] == [active.issue_line_id]
    assert client.post("/api/maintenance/site-issues/search", json={
        **base, "category_minor": "SAS",
    }).json()["total"] == 0  # 已失效的命中行不使单头入选。
    assert client.post("/api/maintenance/site-issues/search", json={
        **base, "category_state": "unlinked",
    }).json()["total"] == 0
    for extra in ({"category_state": "invalid"},
                  {"category_state": "unlinked", "category_major": "硬盘"}):
        assert client.post("/api/maintenance/site-issues/search", json={**base, **extra}).status_code == 422

