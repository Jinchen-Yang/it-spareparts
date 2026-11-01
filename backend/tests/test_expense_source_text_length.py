"""D-0050：报销明细受限源文本不得静默截断（整批拒绝，不做部分导入）。

旧行为：``transform._clip`` 把 销售订单/单号/报销人员/报销类别/费用分类 钳到 64、
流程状态 钳到 16，超长源值被截断后照常入库——源事实变成另一个值。现口径：
合法值原样保留；任一受限字段超长则**整批**在 loader 之前拒绝，skip/upsert 都
不插入、不更新、不作废旧行，回执带源数据行、字段、实际长度与上限。

边界：恰好等于模型列宽接受；+1 拒绝。这里的行号是 transform 的数据行序号
（表头之后从 1 起），与既有行级错误回执一致。
"""
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.etl import pipeline
from app.etl.reader import ReaderError
from app.models.maintenance import FProjectExpense
from app.models.system import SysImportBatch

_COLUMNS = ["报销日期", "报销人员", "报销类别", "费用分类", "支出事由",
            "报销金额", "流程状态", "单号", "序号", "销售订单"]

_FIELDS = {
    "linked_sales_order_no": ("销售订单", 64, "销售订单"),
    "bxd_no": ("单号", 64, "单号"),
    "person": ("报销人员", 64, "报销人员"),
    "expense_type": ("报销类别", 64, "报销类别"),
    "fee_category": ("费用分类", 64, "费用分类"),
    "data_status": ("流程状态", 16, "流程状态"),
}


def _row(**overrides):
    row = {
        "报销日期": "2026-05-01", "报销人员": "张三", "报销类别": "维保费用",
        "费用分类": "备件", "支出事由": "外援", "报销金额": 100,
        "流程状态": "已结束", "单号": "B1", "序号": 1, "销售订单": "XSDD-A",
    }
    row.update(overrides)
    return row


def _workbook(tmp_path, name, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(_COLUMNS)
    for row in rows:
        ws.append([row.get(column) for column in _COLUMNS])
    path = tmp_path / name
    wb.save(str(path))
    return str(path)


def _seed(db, tmp_path):
    """先成功导入一笔旧事实，供 upsert 证明「整批拒绝 = 旧行不作废」。"""
    path = _workbook(tmp_path, "seed.xlsx", [_row(
        **{"报销日期": "2026-04-01", "报销金额": 1000, "单号": "OLD-1"})])
    batch = pipeline.run_import(db, path, "seed.xlsx")
    db.commit()
    assert batch.status == "success"
    old = db.scalars(select(FProjectExpense)).one()
    assert old.bxd_no == "OLD-1" and old.data_status == "已结束"
    return old.id


@pytest.mark.parametrize("field", sorted(_FIELDS))
def test_expense_source_field_exact_limit_is_preserved(db, tmp_path, field):
    column, limit, _label = _FIELDS[field]
    value = "X" * limit
    path = _workbook(tmp_path, f"ok-{field}.xlsx", [_row(**{column: value})])

    batch = pipeline.run_import(db, path, f"ok-{field}.xlsx")
    db.commit()

    assert batch.status == "success"
    fact = db.scalars(select(FProjectExpense)).one()
    assert getattr(fact, field) == value
    assert len(getattr(fact, field)) == limit


@pytest.mark.parametrize("mode", ["skip", "upsert"])
@pytest.mark.parametrize("field", sorted(_FIELDS))
def test_expense_source_field_over_limit_rejects_whole_batch(db, tmp_path, field, mode):
    column, limit, label = _FIELDS[field]
    old_id = _seed(db, tmp_path)
    overlong = "X" * (limit + 1)
    path = _workbook(tmp_path, f"bad-{field}-{mode}.xlsx", [
        _row(**{"报销日期": "2026-05-02", "报销金额": 200, "单号": "NEW-1", "序号": 1}),
        _row(**{"报销日期": "2026-05-03", "报销金额": 300, "单号": "NEW-2", "序号": 2,
                column: overlong}),
    ])

    with pytest.raises(ReaderError) as excinfo:
        pipeline.run_import(db, path, f"bad-{field}-{mode}.xlsx", mode=mode)
    db.commit()

    message = str(excinfo.value)
    assert excinfo.value.code == "expense_field_too_long"
    assert "第 2 行" in message
    assert f"「{label}」" in message
    assert f"长度 {limit + 1}" in message
    assert f"上限 {limit} 字符" in message

    batch = db.scalars(
        select(SysImportBatch).order_by(SysImportBatch.id.desc())).first()
    assert batch.status == "failed"
    assert batch.file_type == "expense"
    assert message in batch.report_json["error"]

    facts = list(db.scalars(select(FProjectExpense)))
    assert [fact.id for fact in facts] == [old_id]
    old = facts[0]
    assert old.bxd_no == "OLD-1"
    assert old.amount == Decimal("1000")
    assert old.data_status == "已结束"          # upsert 未执行缺行作废
    assert old.linked_sales_order_no == "XSDD-A"
    assert all(fact.bxd_no not in ("NEW-1", "NEW-2") for fact in facts)


def test_expense_overlong_field_precedes_missing_link(db, tmp_path):
    """超长校验在 missing_link 之前：带完整金额但缺 XSDD 的数据行同样整批拒绝，
    不允许「先按 missing_link 丢掉这行、其余行照常入库」绕开源文本保护。"""
    path = _workbook(tmp_path, "missing-link.xlsx", [_row(**{
        "销售订单": None, "报销人员": "X" * 65, "单号": "B9", "序号": 1,
    })])

    with pytest.raises(ReaderError) as excinfo:
        pipeline.run_import(db, path, "missing-link.xlsx")
    db.commit()

    assert excinfo.value.code == "expense_field_too_long"
    assert "「报销人员」" in str(excinfo.value)
    assert db.scalars(select(FProjectExpense)).all() == []


def test_expense_source_limit_table_matches_model_columns():
    """上限表与 SQLAlchemy 模型列宽同源，防止以后只改一侧。"""
    columns = FProjectExpense.__table__.columns
    for field, (_column, limit, _label) in _FIELDS.items():
        assert columns[field].type.length == limit
