"""维保项目可选字段导出：白名单、权限与 XLSX 契约。"""

from datetime import date
from decimal import Decimal
from io import BytesIO
import uuid

import pytest
from openpyxl import load_workbook

from app.config import get_settings
from app.models.maintenance_project import MaintenanceProjectContract
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectExpenseAttribution,
)
from app.services import maintenance_project_export
from tests.boss_board_helpers import boss_client, make_project


@pytest.fixture(autouse=True)
def _flag_on():
    settings = get_settings()
    original = settings.maintenance_boss_dashboard_enabled
    settings.maintenance_boss_dashboard_enabled = True
    try:
        yield
    finally:
        settings.maintenance_boss_dashboard_enabled = original


def test_export_options_return_whitelist_and_exact_default_fields(db):
    response = boss_client(db, username="project-export-options").get(
        "/api/maintenance/boss-board/projects/export/options"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["default_fields"] == [
        "project_name",
        "period_from",
        "period_to",
        "contract_nos",
        "contract_amount_inc_tax",
        "collection_received_inc_tax",
    ]
    keys = [field["key"] for field in payload["fields"]]
    assert len(keys) == len(set(keys))
    assert set(payload["default_fields"]) <= set(keys)
    assert all(set(field) == {"key", "label", "group", "default_selected"}
               for field in payload["fields"])


def test_project_note_is_optional_and_exported_as_independent_plain_text(db):
    project = make_project(db, code="项目备注导出", lifecycle="ongoing")
    project.project_note = "=现场改造\n联系甲方"
    project.no_return_note = "免返标注保持独立"
    db.commit()
    client = boss_client(db, username="project-export-note")
    options = client.get("/api/maintenance/boss-board/projects/export/options").json()
    note = next(field for field in options["fields"] if field["key"] == "project_note")
    assert note["label"] == "项目备注"
    assert note["default_selected"] is False
    assert "project_note" not in options["default_fields"]
    response = client.post("/api/maintenance/boss-board/projects/export", json={
        "fields": ["project_name", "project_note"], "lifecycle": "ongoing",
    })
    assert response.status_code == 200, response.text
    workbook = load_workbook(BytesIO(response.content), read_only=True, data_only=False)
    try:
        rows = list(workbook["维保项目清单"].iter_rows(values_only=True))
        assert rows[0] == ("项目名称", "项目备注")
        assert rows[1][1] == "=现场改造\n联系甲方"
        assert workbook["维保项目清单"]["B2"].data_type == "s"
    finally:
        workbook.close()


@pytest.mark.parametrize(("note", "expected"), [
    (None, None),
    ("硬盘免返\n其他备件按合同核对", "硬盘免返\n其他备件按合同核对"),
    ("旧\x0b标注\ufffe\uffff\n联系甲方", "旧标注\n联系甲方"),
    ("=提醒内容", "=提醒内容"),
])
def test_no_return_note_is_optional_and_exports_safe_independent_text(db, note, expected):
    project = make_project(db, code="免返标注导出", lifecycle="ongoing")
    project.project_note = "独立项目备注"
    project.no_return_note = note
    db.commit()
    client = boss_client(db, username="project-export-no-return-note")

    response = client.get("/api/maintenance/boss-board/projects/export/options")
    assert response.status_code == 200, response.text
    options = response.json()
    fields = {field["key"]: field for field in options["fields"]}
    assert fields["no_return_note"] == {
        "key": "no_return_note", "label": "免返标注", "group": "项目基础",
        "default_selected": False,
    }
    assert "no_return_note" not in options["default_fields"]

    response = client.post("/api/maintenance/boss-board/projects/export", json={
        "fields": ["project_name", "project_note", "no_return_note"],
        "lifecycle": "ongoing",
    })
    assert response.status_code == 200, response.text
    workbook = load_workbook(BytesIO(response.content), read_only=True, data_only=False)
    try:
        rows = list(workbook["维保项目清单"].iter_rows(max_col=3, values_only=True))
        assert rows[0] == ("项目名称", "项目备注", "免返标注")
        assert rows[1] == ("免返标注导出", "独立项目备注", expected)
        if expected is not None:
            assert workbook["维保项目清单"]["C2"].data_type == "s"
    finally:
        workbook.close()


@pytest.mark.parametrize("value", [
    "-48V",
    "-48V 电源免返",
    "+5V",
    "+5V 板卡免返",
    "- 硬盘免返",
    "-1",
    "\n第二行开头",
    "\t制表开头",
    "\r回车开头",
    "@备注",
    "=SUM(A1)",
    '=HYPERLINK("https://example.invalid","提醒")',
    "#N/A",
])
def test_export_preserves_text_verbatim_without_formulas(db, value):
    project = make_project(db, code="导出文本往返", lifecycle="ongoing")
    project.display_name = value
    project.project_note = value
    project.no_return_note = value
    db.commit()

    response = boss_client(db, username="project-export-verbatim").post(
        "/api/maintenance/boss-board/projects/export",
        json={
            "fields": ["project_name", "project_note", "no_return_note"],
            "lifecycle": "ongoing",
        },
    )
    assert response.status_code == 200, response.text
    workbook = load_workbook(BytesIO(response.content), data_only=False)
    try:
        worksheet = workbook["维保项目清单"]
        assert worksheet.max_row == 2
        assert [cell.value for cell in worksheet[1]] == ["项目名称", "项目备注", "免返标注"]
        for cell in worksheet[2]:
            assert cell.value == value
            assert cell.data_type == "s"
    finally:
        workbook.close()


@pytest.mark.parametrize(("value", "expected_status"), [
    ("=" + "汉" * 32_766, 200),
    ("\x0b=" + "汉" * 32_766, 200),
    ("=" + "汉" * 32_767, 422),
], ids=["at-limit", "strip-control-before-limit", "over-limit"])
def test_export_enforces_cell_length_after_control_character_cleanup(db, value, expected_status):
    project = make_project(db, code="历史长文本", lifecycle="ongoing")
    # 历史数据可超过网页输入上限，导出仍须拒绝 Excel 会静默截断的内容。
    project.no_return_note = value
    db.commit()
    response = boss_client(db, username="project-export-length").post(
        "/api/maintenance/boss-board/projects/export",
        json={"fields": ["no_return_note"], "lifecycle": "ongoing"},
    )
    assert response.status_code == expected_status, response.text
    if expected_status == 422:
        assert response.json()["detail"] == {
            "code": "project_export_too_large",
            "message": "导出内容超过 Excel 单元格长度上限",
        }
        return
    workbook = load_workbook(BytesIO(response.content), data_only=False)
    try:
        cell = workbook["维保项目清单"]["A2"]
        assert cell.value == "=" + "汉" * 32_766
        assert cell.data_type == "s"
    finally:
        workbook.close()


def test_export_options_and_post_filter_profit_fields_by_server_permission(db):
    client = boss_client(
        db,
        username="project-export-no-profit",
        with_profit=False,
    )
    options = client.get(
        "/api/maintenance/boss-board/projects/export/options"
    ).json()
    keys = {field["key"] for field in options["fields"]}

    assert "contract_amount_inc_tax" not in keys
    assert "collection_received_inc_tax" not in keys
    assert options["default_fields"] == [
        "project_name", "period_from", "period_to", "contract_nos"
    ]

    forbidden = client.post(
        "/api/maintenance/boss-board/projects/export",
        json={"fields": ["project_name", "contract_amount_inc_tax"]},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == {
        "code": "export_field_permission_denied",
        "message": "所选字段超出当前账号的数据权限",
        "fields": ["contract_amount_inc_tax"],
    }


def test_export_rejects_unknown_field_instead_of_resolving_orm_attribute(db):
    response = boss_client(db, username="project-export-unknown").post(
        "/api/maintenance/boss-board/projects/export",
        json={"fields": ["project_name", "__dict__"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "invalid_export_fields",
        "message": "包含服务端白名单之外的导出字段",
        "fields": ["__dict__"],
    }


def test_financial_state_fields_do_not_call_missing_values_complete():
    fields = {
        field.key: field
        for field in maintenance_project_export.EXPORT_FIELDS
    }
    empty = {
        "contract_amount_inc_tax": {"state": "ready", "value": None},
        "collection_preview_inc_tax": {"state": "ready", "value": None},
        "contract_incomplete": False,
    }
    partial = {
        "contract_amount_inc_tax": {"state": "partial", "value": Decimal("10")},
        "contract_incomplete": True,
    }

    assert fields["contract_amount_state"].accessor(empty) == "无有效合同"
    assert fields["collection_state"].accessor(empty) == "尚未上报"
    assert (
        fields["contract_amount_state"].accessor(partial)
        == "合同事实不完整（已知小计）"
    )


def test_export_xlsx_uses_current_contract_and_latest_confirmed_collection(db):
    project = make_project(db, code="=公式项目", lifecycle="ongoing")
    project.project_note = "-1"
    make_project(db, code="已结束项目", lifecycle="ended")
    relation = MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()),
        project_id=project.project_id,
        contract_id="contract-export-1",
        contract_no="XSDD-EXPORT-001",
        contract_amount=Decimal("1000.00"),
        amount_inc_tax=Decimal("1130.00"),
        contract_status="执行中",
        status_mapping_state="mapped",
        status_mapping_version="v1",
        included_in_total=True,
        effective_from=date(2026, 1, 1),
        source="synthetic_test",
        version=1,
    )
    db.add(relation)
    db.flush()
    db.add(MaintenanceProjectExpenseAttribution(
        expense_id="export-negative-expense",
        project_id=project.project_id,
        project_contract_id=relation.project_contract_id,
        expense_ref="合成冲销报销",
        expense_date=date(2026, 1, 1),
        tax_basis="inc",
        amount_inc_tax=Decimal("-48.00"),
        amount_ex_tax=Decimal("-42.48"),
        raw_status="已生效",
        status_mapping_state="mapped",
        normalized_status="approved",
        status_mapping_version="synthetic_test",
        ownership_mapping_state="mapped",
        version=1,
    ))
    db.add_all([
        MaintenanceCollectionSnapshot(
            collection_id=str(uuid.uuid4()),
            project_id=project.project_id,
            project_contract_id=relation.project_contract_id,
            report_month=date(2026, 7, 1),
            cumulative_amount=Decimal("300.00"),
            status="confirmed",
            source="direct_api",
            version=1,
        ),
        MaintenanceCollectionSnapshot(
            collection_id=str(uuid.uuid4()),
            project_id=project.project_id,
            project_contract_id=relation.project_contract_id,
            report_month=date(2026, 8, 1),
            cumulative_amount=Decimal("450.00"),
            status="confirmed",
            source="direct_api",
            version=1,
        ),
    ])
    db.commit()

    fields = [
        *maintenance_project_export.DEFAULT_FIELDS,
        "expense_cost_inc_tax", "project_note", "version",
    ]
    response = boss_client(db, username="project-export-xlsx").post(
        "/api/maintenance/boss-board/projects/export",
        json={"fields": fields, "lifecycle": "ongoing", "sort": "name"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert response.headers["x-export-row-count"] == "1"

    workbook = load_workbook(BytesIO(response.content), read_only=True, data_only=False)
    worksheet = workbook["维保项目清单"]
    rows = list(worksheet.iter_rows())
    table = [tuple(cell.value for cell in row) for row in rows]
    workbook.close()
    assert table[0] == (
        "项目名称",
        "维保起始时间",
        "维保终止时间",
        "销售单号（合同号）",
        "合同总额（含税）",
        "累计已回款（含税）",
        "报销成本（含税）",
        "项目备注",
        "主档版本",
    )
    assert table[1][0] == "=公式项目"  # 显式文本单元格防 Excel 公式注入。
    assert table[1][1].date() == date(2020, 1, 1)
    assert table[1][2].date() == date(2099, 12, 31)
    assert table[1][3:] == ("XSDD-EXPORT-001", 1130, 450, -48, "-1", 1)
    assert [cell.data_type for cell in rows[1]] == [
        "s", "d", "d", "s", "n", "n", "n", "s", "n",
    ]
