"""AB-3：报销/回款往返工作簿（F3 改判并入 v1）。

口径出处：`docs/releases/v1.23-addon-pack.md` AB-3 + REQUIREMENTS #8/#30/#31。
"""
import io
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from app import permissions
from app.auth import hash_password
from app.main import app
from app.models.maintenance import FProjectExpense
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectContract,
)
from app.models.maintenance_project_operations import MaintenanceCollectionSnapshot
from app.models.system import SysImportBatch, SysUser
from app.services import maintenance_expense_collection_workbook as wbk

_PASSWORD = "synthetic-wbk-password-1"
_BASE = "/api/maintenance/projects/stable"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _client(db, *, username, overrides) -> TestClient:
    base = permissions.effective("readonly", None)
    db.add(SysUser(
        username=username, role="readonly", display_name=username,
        password_hash=hash_password(_PASSWORD), is_active=True,
        template_code="readonly", template_version=1, template_perms=base,
        perm_overrides=overrides,
        permissions=permissions.effective_from_snapshot(base, overrides)))
    db.commit()
    client = TestClient(app)
    login = client.post("/api/auth/login",
                        json={"username": username, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    client.headers["Authorization"] = f"Bearer {login.json()['token']}"
    return client


def uploader(db, username="wbk-uploader") -> TestClient:
    return _client(db, username=username, overrides={
        "page_maintenance": True, "data_purchase_cost": True, "data_profit": True,
        "action_maintenance_expense_collection_upload": True})


def reader(db, username="wbk-reader") -> TestClient:
    """能看金额但不能传（只读对账）。"""
    return _client(db, username=username, overrides={
        "page_maintenance": True, "data_purchase_cost": True, "data_profit": True})


@pytest.fixture()
def project(db):
    proj = MaintenanceProject(project_id=str(uuid.uuid4()), project_code="合成项目A",
                              display_name="合成项目A", lifecycle_status="ongoing")
    db.add(proj)
    db.flush()
    db.add(MaintenanceProjectContract(
        project_contract_id=str(uuid.uuid4()), project_id=proj.project_id,
        contract_id="C-1", contract_no="HT-001",
        amount_inc_tax=Decimal("100000.00"), included_in_total=True,
        status_mapping_state="mapped", status_mapping_version="v1",
        effective_from=date(2026, 1, 1), source="ledger", version=1))
    db.commit()
    return proj


def _expense(db, *, raw_line_id="BXD-1#1", contract_no="HT-001",
             ex_tax="100.00"):
    batch = SysImportBatch(filename="e.xlsx", file_type="maintenance",
                           file_hash=uuid.uuid4().hex * 2, status="success")
    db.add(batch)
    db.flush()
    db.add(FProjectExpense(
        raw_line_id=raw_line_id, bxd_no="BXD-20260101-1", line_no=1,
        data_status="已结束", expense_date=date(2026, 7, 1), person="张三",
        expense_type="差旅", fee_category="交通", reason="现场维保",
        linked_sales_order_no=contract_no,
        amount=Decimal(ex_tax), amount_ex_tax=Decimal(ex_tax),
        amount_inc_tax=(Decimal(ex_tax) * Decimal("1.13")).quantize(Decimal("0.01")),
        tax_basis="ex", tax_rate_used=Decimal("0.13"), import_batch_id=batch.id))
    db.commit()


def _download(client, project) -> bytes:
    resp = client.get(f"{_BASE}/{project.project_id}/expense-collection-workbook.xlsx")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(_XLSX)
    return resp.content


def _upload(client, project, content: bytes, *, action="apply"):
    return client.post(
        f"{_BASE}/{project.project_id}/expense-collection-workbook/{action}",
        files={"file": ("wbk.xlsx", io.BytesIO(content), _XLSX)})


# ---------- 导出 ----------

def test_workbook_has_exactly_the_two_signed_sheets(db, project):
    _expense(db)
    wb = load_workbook(io.BytesIO(_download(uploader(db), project)))
    # AB-3「合二为一」：一个文件两张 sheet；不得顺带导出冻结的 v3 其余四表
    assert wb.sheetnames[:2] == [wbk.SHEET_EXPENSE, wbk.SHEET_COLLECTION]
    visible = [n for n in wb.sheetnames if wb[n].sheet_state == "visible"]
    assert visible == [wbk.SHEET_EXPENSE, wbk.SHEET_COLLECTION]
    assert [c.value for c in wb[wbk.SHEET_COLLECTION][1]][:4] == [
        "操作", "合同编号", "报告月份", "累计回款金额(含税)"]


def test_download_requires_profit_visibility_but_not_upload_action(db, project):
    """只读对账的人应当能把表拉下来核对，不必为此获得写权限。"""
    assert reader(db).get(
        f"{_BASE}/{project.project_id}/expense-collection-workbook.xlsx"
    ).status_code == 200
    # data_profit 在 readonly 模板里默认是 True，必须显式关掉才测得到这条门
    no_money = _client(db, username="wbk-nomoney",
                       overrides={"page_maintenance": True, "data_profit": False})
    assert no_money.get(
        f"{_BASE}/{project.project_id}/expense-collection-workbook.xlsx"
    ).status_code == 403


def test_unknown_project_is_404(db):
    assert uploader(db, "wbk-404").get(
        f"{_BASE}/{uuid.uuid4()}/expense-collection-workbook.xlsx"
    ).status_code == 404


# ---------- 报销：未税可改，含税由系统算 ----------

def test_expense_inc_tax_is_computed_not_accepted_from_the_sheet(db, project):
    """REQUIREMENTS #8：正式金额列=含税，由未税×1.13 算出，不接受人工直填。"""
    _expense(db, ex_tax="100.00")
    client = uploader(db)
    wb = load_workbook(io.BytesIO(_download(client, project)))
    ws = wb[wbk.SHEET_EXPENSE]
    ws.cell(row=2, column=8, value=200)        # 未税改成 200
    ws.cell(row=2, column=9, value=999999)     # 含税列乱填，应被忽略
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload(client, project, buf.getvalue())
    assert resp.status_code == 200, resp.text
    assert resp.json()["expense_updates"] == 1
    db.expire_all()
    expense = db.execute(select(FProjectExpense)).scalars().one()
    assert expense.amount_ex_tax == Decimal("200.00")
    assert expense.amount_inc_tax == Decimal("226.00")   # 200 × 1.13，不是 999999
    assert expense.tax_basis == "ex"


def test_expense_rows_cannot_be_invented_in_the_sheet(db, project):
    """铁律 1：报销单在源系统产生；本表只改金额，不能凭空新增行。"""
    _expense(db)
    client = uploader(db)
    wb = load_workbook(io.BytesIO(_download(client, project)))
    ws = wb[wbk.SHEET_EXPENSE]
    ws.append(["BXD-NEW", "2026-07-02", "李四", "差旅", "交通", "编的",
               "HT-001", 50, "", "已结束"])
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload(client, project, buf.getvalue())
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "expense_row_not_recognized"


def test_blank_amount_leaves_the_row_untouched(db, project):
    _expense(db, ex_tax="100.00")
    client = uploader(db)
    content = _download(client, project)
    assert _upload(client, project, content).json()["expense_updates"] == 0
    db.expire_all()
    assert db.execute(select(FProjectExpense)).scalars().one().amount_ex_tax \
        == Decimal("100.00")


# ---------- 回款：月度累计快照 ----------

def test_collection_create_appends_monthly_cumulative_snapshot(db, project):
    client = uploader(db)
    wb = load_workbook(io.BytesIO(_download(client, project)))
    ws = wb[wbk.SHEET_COLLECTION]
    ws.cell(row=2, column=1, value="CREATE")
    ws.cell(row=2, column=2, value="HT-001")
    ws.cell(row=2, column=3, value="2026-07")
    ws.cell(row=2, column=4, value=30000)
    ws.cell(row=2, column=5, value="SK-001")
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload(client, project, buf.getvalue())
    assert resp.status_code == 200, resp.text
    assert resp.json()["collection_creates"] == 1
    snapshot = db.execute(select(MaintenanceCollectionSnapshot)).scalars().one()
    assert snapshot.report_month == date(2026, 7, 1)
    assert snapshot.cumulative_amount == Decimal("30000.00")
    assert snapshot.status == "confirmed" and snapshot.source == "workbook"
    assert snapshot.import_batch_id is not None


def test_reupload_overwrites_the_same_month(db, project):
    """「上传覆盖」：同合同同月份再传一次是改写累计额，不是追加第二条。"""
    client = uploader(db)

    def _send(amount):
        wb = load_workbook(io.BytesIO(_download(client, project)))
        ws = wb[wbk.SHEET_COLLECTION]
        row = ws.max_row + 1 if ws.cell(row=2, column=2).value else 2
        ws.cell(row=row, column=1, value="CREATE")
        ws.cell(row=row, column=2, value="HT-001")
        ws.cell(row=row, column=3, value="2026-07")
        ws.cell(row=row, column=4, value=amount)
        buf = io.BytesIO()
        wb.save(buf)
        return _upload(client, project, buf.getvalue())

    assert _send(30000).status_code == 200
    assert _send(45000).status_code == 200
    snapshots = db.execute(select(MaintenanceCollectionSnapshot)).scalars().all()
    assert len(snapshots) == 1
    db.expire_all()
    assert snapshots[0].cumulative_amount == Decimal("45000.00")


def test_void_marks_snapshot_void_rather_than_deleting(db, project):
    """缺行 ≠ 删除：作废必须显式写 VOID，且留痕不物理删。"""
    contract = db.execute(select(MaintenanceProjectContract)).scalars().one()
    db.add(MaintenanceCollectionSnapshot(
        collection_id=str(uuid.uuid4()), project_id=project.project_id,
        project_contract_id=contract.project_contract_id,
        report_month=date(2026, 6, 1), cumulative_amount=Decimal("10000.00"),
        status="confirmed", source="legacy", version=1))
    db.commit()
    client = uploader(db)
    wb = load_workbook(io.BytesIO(_download(client, project)))
    ws = wb[wbk.SHEET_COLLECTION]
    ws.cell(row=2, column=1, value="VOID")
    buf = io.BytesIO()
    wb.save(buf)
    assert _upload(client, project, buf.getvalue()).status_code == 200
    db.expire_all()
    snapshot = db.execute(select(MaintenanceCollectionSnapshot)).scalars().one()
    assert snapshot.status == "void"          # 仍在库，只是作废


def test_contract_outside_the_project_is_rejected(db, project):
    client = uploader(db)
    wb = load_workbook(io.BytesIO(_download(client, project)))
    ws = wb[wbk.SHEET_COLLECTION]
    ws.cell(row=2, column=1, value="CREATE")
    ws.cell(row=2, column=2, value="HT-别人家的")
    ws.cell(row=2, column=3, value="2026-07")
    ws.cell(row=2, column=4, value=100)
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload(client, project, buf.getvalue())
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "contract_not_found"


def test_one_bad_row_rejects_the_whole_file(db, project):
    """整份拒绝：静默跳过一行 = 操作者以为改了、实际没改，比报错难发现。"""
    _expense(db, ex_tax="100.00")
    client = uploader(db)
    wb = load_workbook(io.BytesIO(_download(client, project)))
    wb[wbk.SHEET_EXPENSE].cell(row=2, column=8, value=250)      # 合法改动
    ws = wb[wbk.SHEET_COLLECTION]
    ws.cell(row=2, column=1, value="CREATE")
    ws.cell(row=2, column=2, value="HT-001")
    ws.cell(row=2, column=3, value="不是月份")                   # 非法
    ws.cell(row=2, column=4, value=100)
    buf = io.BytesIO()
    wb.save(buf)
    assert _upload(client, project, buf.getvalue()).status_code == 422
    db.expire_all()
    # 合法的那一行也必须没有落库
    assert db.execute(select(FProjectExpense)).scalars().one().amount_ex_tax \
        == Decimal("100.00")


def test_validate_is_side_effect_free(db, project):
    _expense(db, ex_tax="100.00")
    client = uploader(db)
    wb = load_workbook(io.BytesIO(_download(client, project)))
    wb[wbk.SHEET_EXPENSE].cell(row=2, column=8, value=777)
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload(client, project, buf.getvalue(), action="validate")
    assert resp.status_code == 200 and resp.json()["expense_updates"] == 1
    db.expire_all()
    assert db.execute(select(FProjectExpense)).scalars().one().amount_ex_tax \
        == Decimal("100.00")


# ---------- 权限 ----------

def test_upload_requires_the_dedicated_action_key(db, project):
    """WBDD-only 账号传不了（AB-3 权限要求）。"""
    content = _download(uploader(db, "wbk-up2"), project)
    assert _upload(reader(db, "wbk-read2"), project, content).status_code == 403
    wbdd_only = _client(db, username="wbk-wbddonly", overrides={
        "page_maintenance": True, "action_maintenance_wbdd_import": True})
    assert _upload(wbdd_only, project, content).status_code == 403


def test_unauthenticated_is_401(db, project):
    anon = TestClient(app)
    assert anon.get(
        f"{_BASE}/{project.project_id}/expense-collection-workbook.xlsx"
    ).status_code == 401


def test_non_xlsx_upload_is_415(db, project):
    client = uploader(db, "wbk-415")
    resp = client.post(
        f"{_BASE}/{project.project_id}/expense-collection-workbook/apply",
        files={"file": ("x.csv", io.BytesIO(b"a,b"), "text/csv")})
    assert resp.status_code == 415


# ---------- #47：报销备注列 ----------

def test_expense_sheet_has_editable_remark_column(db, project):
    _expense(db)
    wb = load_workbook(io.BytesIO(_download(uploader(db, "rmk-1"), project)))
    # 表头行末尾还有一列隐藏的行标识（值为空），所以按名字取位置而不是取 [-1]
    headers = [c.value for c in wb[wbk.SHEET_EXPENSE][1]]
    assert "备注" in headers
    remark_col = headers.index("备注") + 1
    assert remark_col == len(wbk._EXPENSE_HEADERS)
    # 黄底＝可编辑（与未税金额同色），与只读列区分
    ws = wb[wbk.SHEET_EXPENSE]
    assert ws.cell(row=1, column=remark_col).fill.fgColor.rgb \
        == ws.cell(row=1, column=8).fill.fgColor.rgb
    assert ws.cell(row=1, column=remark_col).fill.fgColor.rgb \
        != ws.cell(row=1, column=1).fill.fgColor.rgb


def test_remark_roundtrips_into_the_database(db, project):
    _expense(db)
    client = uploader(db, "rmk-2")
    wb = load_workbook(io.BytesIO(_download(client, project)))
    ws = wb[wbk.SHEET_EXPENSE]
    ws.cell(row=2, column=len(wbk._EXPENSE_HEADERS), value="客户确认可报")
    buf = io.BytesIO()
    wb.save(buf)
    resp = _upload(client, project, buf.getvalue())
    assert resp.status_code == 200, resp.text
    assert resp.json()["expense_updates"] == 1
    db.expire_all()
    assert db.execute(select(FProjectExpense)).scalars().one().remark == "客户确认可报"


def test_existing_remark_is_exported_back(db, project):
    _expense(db)
    expense = db.execute(select(FProjectExpense)).scalars().one()
    expense.remark = "上一轮写的备注"
    db.commit()
    wb = load_workbook(io.BytesIO(_download(uploader(db, "rmk-3"), project)))
    ws = wb[wbk.SHEET_EXPENSE]
    assert ws.cell(row=2, column=len(wbk._EXPENSE_HEADERS)).value == "上一轮写的备注"


def test_remark_only_edit_does_not_touch_amounts(db, project):
    """只改备注不填金额也是合法回填：金额三列一个都不能动。"""
    _expense(db, ex_tax="100.00")
    client = uploader(db, "rmk-4")
    wb = load_workbook(io.BytesIO(_download(client, project)))
    ws = wb[wbk.SHEET_EXPENSE]
    ws.cell(row=2, column=8).value = None                      # 未税留空
    ws.cell(row=2, column=len(wbk._EXPENSE_HEADERS), value="只改备注")
    buf = io.BytesIO()
    wb.save(buf)
    assert _upload(client, project, buf.getvalue()).status_code == 200
    db.expire_all()
    expense = db.execute(select(FProjectExpense)).scalars().one()
    assert expense.remark == "只改备注"
    assert expense.amount_ex_tax == Decimal("100.00")
    assert expense.amount_inc_tax == Decimal("113.00")


def test_clearing_remark_writes_null(db, project):
    _expense(db)
    expense = db.execute(select(FProjectExpense)).scalars().one()
    expense.remark = "待清空"
    db.commit()
    client = uploader(db, "rmk-5")
    wb = load_workbook(io.BytesIO(_download(client, project)))
    wb[wbk.SHEET_EXPENSE].cell(row=2, column=len(wbk._EXPENSE_HEADERS)).value = None
    buf = io.BytesIO()
    wb.save(buf)
    assert _upload(client, project, buf.getvalue()).status_code == 200
    db.expire_all()
    assert db.execute(select(FProjectExpense)).scalars().one().remark is None


def test_unchanged_remark_is_not_a_write(db, project):
    _expense(db)
    expense = db.execute(select(FProjectExpense)).scalars().one()
    expense.remark = "没动过"
    db.commit()
    client = uploader(db, "rmk-6")
    content = _download(client, project)
    assert _upload(client, project, content).json()["expense_updates"] == 0
