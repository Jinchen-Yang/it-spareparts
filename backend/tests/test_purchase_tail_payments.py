"""采购导出上传到排行摘要的尾款契约。"""
import json

from app.etl import pipeline
from tests.boss_board_helpers import client_for
from tests.test_order_head_source_fidelity import PURCHASE_LINE_ID, _purchase_row, _write_xlsx


def test_tail_payment_cards_use_order_snapshots_and_keep_unknown(db, tmp_path):
    def upload(name, rows, mode="skip"):
        batch = pipeline.run_import(
            db, _write_xlsx(tmp_path / name, rows), name, mode=mode
        )
        db.commit()
        assert batch.status == "success"
        return batch

    boss = client_for(db, username="tail-boss", role="boss")
    no_cost = client_for(
        db, username="tail-no-cost", role="boss", overrides={"data_purchase_cost": False}
    )

    def cube(client=boss, **filters):
        reply = client.get(
            "/api/purchases/analytics/cube", params={"row": "buyer", "metric": "orders", **filters}
        )
        assert reply.status_code == 200, reply.text
        return reply.json()["total"]

    old = _purchase_row(**{
        "采购单号(必填)": "CGDD-LEGACY", "数据ID(不可修改)": "PUR-LEGACY",
        PURCHASE_LINE_ID: "PL-LEGACY",
    })
    upload("legacy.xlsx", [old])
    assert cube()["tail_payments"] == {
        "paid": {"amount": None, "missing_orders": 1},
        "pending": {"amount": None, "missing_orders": 1},
    }

    upload("initial.xlsx", [
        _purchase_row(已付尾款=42, 待付尾款=-5, **{"定金/首付(必填)": 9}),
        _purchase_row(已付尾款=42, 待付尾款=-5, **{PURCHASE_LINE_ID: "PL-2", "明细.序号": 2}),
    ])
    assert cube()["tail_payments"] == {
        "paid": {"amount": 42, "missing_orders": 1},
        "pending": {"amount": -5, "missing_orders": 1},
    }
    assert "tail_payments" not in cube(no_cost)
    assert "tail_payments" not in cube(scope=json.dumps({"pn": "TEST-PN-1"}))
    assert "tail_payments" not in cube(category="未分类")

    upload("changed.xlsx", [_purchase_row(已付尾款=0, 待付尾款=None)])
    upload("older-template.xlsx", [_purchase_row(**{"采购金额": 117})], mode="upsert")
    assert cube()["tail_payments"] == {
        "paid": {"amount": 0, "missing_orders": 1},
        "pending": {"amount": None, "missing_orders": 2},
    }

    upload("one-column.xlsx", [_purchase_row(待付尾款=17)])
    assert cube()["tail_payments"] == {
        "paid": {"amount": 0, "missing_orders": 1},
        "pending": {"amount": 17, "missing_orders": 1},
    }

    upload("nonfinite.xlsx", [
        _purchase_row(已付尾款="+NaN", 待付尾款=500),
        {**old, "已付尾款": 25, "待付尾款": 8},
    ])
    assert cube()["tail_payments"] == {
        "paid": {"amount": 25, "missing_orders": 0},
        "pending": {"amount": 25, "missing_orders": 0},
    }

    upload("conflict.xlsx", [
        _purchase_row(已付尾款=900, 待付尾款=0),
        _purchase_row(已付尾款=800, 待付尾款=0, **{PURCHASE_LINE_ID: "PL-2", "明细.序号": 2}),
        {**old, "已付尾款": 25, "待付尾款": 5},
    ], mode="upsert")
    assert cube()["tail_payments"] == {
        "paid": {"amount": 25, "missing_orders": 0},
        "pending": {"amount": 22, "missing_orders": 0},
    }
