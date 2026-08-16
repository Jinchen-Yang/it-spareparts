"""R2：项目总表六 sheet / 单 sheet / 全项目备件行级表的下载与上传覆盖。

口径出处：REQUIREMENTS #38（三处下载点、在哪下载就在哪上传）、#40（一张总表改所有
数据）、表 6 六 sheet 规格。
"""
import io
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app.business_time import business_today
from app.models.maintenance import FMaintenanceLine, MaintenanceManualCostOverride
from app.services import maintenance_expense_collection_workbook as ec
from app.services import maintenance_project_master_workbook as master
from tests.boss_board_helpers import assign, import_wbdd, make_project
from tests.test_maintenance_expense_collection_workbook import (  # noqa: F401
    _XLSX, uploader, reader,
)

_BASE = "/api/maintenance/projects/stable"
_GLOBAL = "/api/maintenance/spare-part-lines"


@pytest.fixture()
def project_with_lines(db, tmp_path):
    from app.models.maintenance_project import MaintenanceProjectContract

    proj = make_project(db)
    db.add(MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()), project_id=proj.project_id,
        contract_id="C-1", contract_no="HT-001",
        amount_inc_tax=Decimal("100000.00"), included_in_total=True,
        status_mapping_state="mapped", status_mapping_version="v1",
        effective_from=date(2026, 1, 1), source="ledger", version=1))
    orders = import_wbdd(db, tmp_path, orders=1, lines_per_order=2)
    assign(db, orders[0], proj)
    db.commit()
    return proj


def _download_master(client, project, sheets=None):
    params = {"sheets": sheets} if sheets else None
    resp = client.get(f"{_BASE}/{project.project_id}/master-workbook.xlsx",
                      params=params)
    assert resp.status_code == 200, resp.text
    return resp.content


def _upload_master(client, project, content, *, action="apply"):
    return client.post(
        f"{_BASE}/{project.project_id}/master-workbook/{action}",
        files={"file": ("m.xlsx", io.BytesIO(content), _XLSX)})


# ---------- 项目总表：六 sheet ----------

def test_master_workbook_has_all_six_sheets(db, project_with_lines):
    wb = load_workbook(io.BytesIO(
        _download_master(uploader(db), project_with_lines)))
    visible = [n for n in wb.sheetnames if wb[n].sheet_state == "visible"]
    assert visible == list(master.ALL_SHEETS)


def test_single_sheet_download_returns_only_that_sheet(db, project_with_lines):
    """各 tab 单 sheet 下载（#38）。"""
    wb = load_workbook(io.BytesIO(_download_master(
        uploader(db, "m-single"), project_with_lines, sheets=master.SHEET_PARTS)))
    visible = [n for n in wb.sheetnames if wb[n].sheet_state == "visible"]
    assert visible == [master.SHEET_PARTS]


def test_unknown_sheet_name_is_422(db, project_with_lines):
    resp = uploader(db, "m-badsheet").get(
        f"{_BASE}/{project_with_lines.project_id}/master-workbook.xlsx",
        params={"sheets": "99_不存在"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "unknown_sheet"


def test_unknown_project_is_404(db):
    assert uploader(db, "m-404").get(
        f"{_BASE}/{uuid.uuid4()}/master-workbook.xlsx").status_code == 404


# ---------- 03 备件订单：缺价补录 ----------

def test_cost_refill_writes_manual_override_and_computes_inc_tax(db,
                                                                 project_with_lines):
    """#40 缺价补录：未税人工填，含税系统算；落既有人工成本覆盖表（零迁移）。"""
    client = uploader(db, "m-refill")
    wb = load_workbook(io.BytesIO(_download_master(client, project_with_lines)))
    ws = wb[master.SHEET_PARTS]
    ws.cell(row=2, column=12, value=200)          # 未税单位成本
    ws.cell(row=2, column=14, value="补录依据：合同附件")
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload_master(client, project_with_lines, buf.getvalue())
    assert resp.status_code == 200, resp.text
    assert resp.json()["cost_refills"] == 1

    override = db.execute(select(MaintenanceManualCostOverride)).scalars().one()
    assert override.unit_cost_ex_tax == Decimal("200.00")
    assert override.unit_cost_inc_tax == Decimal("226.00")
    assert override.reason == "补录依据：合同附件"
    assert override.active is True


def test_reupload_overwrites_the_same_line(db, project_with_lines):
    client = uploader(db, "m-refill2")

    def _send(amount):
        wb = load_workbook(io.BytesIO(_download_master(client, project_with_lines)))
        wb[master.SHEET_PARTS].cell(row=2, column=12, value=amount)
        buf = io.BytesIO()
        wb.save(buf)
        return _upload_master(client, project_with_lines, buf.getvalue())

    assert _send(200).status_code == 200
    assert _send(300).status_code == 200
    overrides = db.execute(select(MaintenanceManualCostOverride)).scalars().all()
    assert len(overrides) == 1                     # 覆盖，不是追加
    db.expire_all()
    assert overrides[0].unit_cost_ex_tax == Decimal("300.00")


def test_blank_cost_leaves_line_untouched(db, project_with_lines):
    client = uploader(db, "m-blank")
    content = _download_master(client, project_with_lines)
    assert _upload_master(client, project_with_lines, content).json()["cost_refills"] == 0
    assert db.execute(select(MaintenanceManualCostOverride)).scalars().all() == []


def test_invented_part_row_is_rejected(db, project_with_lines):
    """需求单只能由氚云导入，本表只补价（铁律 1）。"""
    client = uploader(db, "m-invent")
    wb = load_workbook(io.BytesIO(_download_master(client, project_with_lines)))
    wb[master.SHEET_PARTS].append(["编的单号", "2026-07-01", "HT-001", "项目",
                                   "PN-X", "描述", 1, 0, "", "", "", 10, "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload_master(client, project_with_lines, buf.getvalue())
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "line_not_recognized"


# ---------- 04/05 复用 AB-3 ----------

def test_master_upload_also_applies_expense_and_collection(db, project_with_lines):
    """总表一次上传同时落 03/05——三类改动同一事务（#40 一张总表改所有数据）。"""
    from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot

    client = uploader(db, "m-all")
    wb = load_workbook(io.BytesIO(_download_master(client, project_with_lines)))
    wb[master.SHEET_PARTS].cell(row=2, column=12, value=150)
    ws = wb[master.SHEET_COLLECTION]
    ws.cell(row=2, column=1, value="CREATE")
    ws.cell(row=2, column=2, value="HT-001")
    ws.cell(row=2, column=3, value="2026-07")
    ws.cell(row=2, column=4, value=50000)
    buf = io.BytesIO()
    wb.save(buf)
    body = _upload_master(client, project_with_lines, buf.getvalue()).json()
    assert body["cost_refills"] == 1 and body["collection_creates"] == 1
    assert db.execute(select(MaintenanceCollectionSnapshot)).scalars().one() \
        .cumulative_amount == Decimal("50000.00")


def test_one_bad_row_rejects_the_whole_file(db, project_with_lines):
    """整份拒绝：合法的补价也不能落库。"""
    client = uploader(db, "m-atomic")
    wb = load_workbook(io.BytesIO(_download_master(client, project_with_lines)))
    wb[master.SHEET_PARTS].cell(row=2, column=12, value=150)      # 合法
    ws = wb[master.SHEET_COLLECTION]
    ws.cell(row=2, column=1, value="CREATE")
    ws.cell(row=2, column=2, value="HT-001")
    ws.cell(row=2, column=3, value="不是月份")                     # 非法
    ws.cell(row=2, column=4, value=1)
    buf = io.BytesIO()
    wb.save(buf)
    assert _upload_master(client, project_with_lines, buf.getvalue()).status_code == 422
    assert db.execute(select(MaintenanceManualCostOverride)).scalars().all() == []


def test_validate_is_side_effect_free(db, project_with_lines):
    client = uploader(db, "m-validate")
    wb = load_workbook(io.BytesIO(_download_master(client, project_with_lines)))
    wb[master.SHEET_PARTS].cell(row=2, column=12, value=777)
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload_master(client, project_with_lines, buf.getvalue(),
                          action="validate")
    assert resp.status_code == 200 and resp.json()["cost_refills"] == 1
    assert db.execute(select(MaintenanceManualCostOverride)).scalars().all() == []


# ---------- 主页全局备件行级表 ----------

def test_range_presets(db):
    today = business_today()
    assert master.resolve_range("today", None, None) == (today, today)
    y = today - timedelta(days=1)
    assert master.resolve_range("yesterday", None, None) == (y, y)
    assert master.resolve_range("this_week", None, None)[0] \
        == today - timedelta(days=today.weekday())
    assert master.resolve_range("this_month", None, None)[0] == today.replace(day=1)
    assert master.resolve_range("custom", date(2026, 1, 1), date(2026, 2, 1)) \
        == (date(2026, 1, 1), date(2026, 2, 1))


def test_custom_range_requires_both_ends(db):
    with pytest.raises(ec.WorkbookError) as excinfo:
        master.resolve_range("custom", date(2026, 1, 1), None)
    assert excinfo.value.code == "invalid_range"


def test_global_download_and_refill_roundtrip(db, project_with_lines):
    client = uploader(db, "g-refill")
    resp = client.get(f"{_GLOBAL}.xlsx",
                      params={"range": "custom", "from": "2026-01-01",
                              "to": "2026-12-31"})
    assert resp.status_code == 200
    wb = load_workbook(io.BytesIO(resp.content))
    ws = wb[master.GLOBAL_SHEET]
    assert ws.max_row >= 2                       # 有归属的行都在
    ws.cell(row=2, column=10, value=88)          # 未税单位成本
    buf = io.BytesIO()
    wb.save(buf)
    applied = client.post(f"{_GLOBAL}/apply",
                          files={"file": ("g.xlsx", io.BytesIO(buf.getvalue()),
                                          _XLSX)})
    assert applied.status_code == 200, applied.text
    assert applied.json()["cost_refills"] == 1
    override = db.execute(select(MaintenanceManualCostOverride)).scalars().one()
    assert override.unit_cost_ex_tax == Decimal("88.00")


def test_global_export_is_scoped_by_range(db, project_with_lines):
    """今天没有单 → 表里只有表头（不报错、不塞别的期间的行）。"""
    client = uploader(db, "g-range")
    resp = client.get(f"{_GLOBAL}.xlsx", params={"range": "today"})
    ws = load_workbook(io.BytesIO(resp.content))[master.GLOBAL_SHEET]
    assert ws.max_row == 1


def test_bad_range_is_422(db):
    resp = uploader(db, "g-badrange").get(f"{_GLOBAL}.xlsx",
                                          params={"range": "last_century"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_range"


# ---------- 权限 ----------

def test_download_needs_profit_upload_needs_action_key(db, project_with_lines):
    content = _download_master(uploader(db, "m-perm-up"), project_with_lines)
    # 只读账号能下载（对账），但传不了
    assert reader(db, "m-perm-read").get(
        f"{_BASE}/{project_with_lines.project_id}/master-workbook.xlsx"
    ).status_code == 200
    assert _upload_master(reader(db, "m-perm-read2"), project_with_lines,
                          content).status_code == 403
    assert reader(db, "m-perm-read3").post(
        f"{_GLOBAL}/apply",
        files={"file": ("g.xlsx", io.BytesIO(content), _XLSX)}).status_code == 403
