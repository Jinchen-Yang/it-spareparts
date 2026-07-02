"""采购分析面板：清洗派生（含税判定/税率反推/来源分类）+ 聚合服务 + 含税未税 + RBAC。"""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import security
from app.etl import cleaner, loader
from app.models.dimensions import DimPart
from app.models.system import SysImportBatch
from app.services import purchase_analysis as pa
from tests import factories as f

_AS_OF = date(2026, 6, 25)


# ---------------- cleaner 派生单元 ----------------

def test_parse_tax_inclusive():
    assert cleaner.parse_tax_inclusive("含税") is True
    assert cleaner.parse_tax_inclusive("不含") is False     # '不含' 含子串 '含'，必须先判
    assert cleaner.parse_tax_inclusive("未税") is False
    assert cleaner.parse_tax_inclusive(None) is None
    assert cleaner.parse_tax_inclusive("") is None


def test_derive_tax_rate():
    # 含税单实测：不含税 1946.9 + 税金 253.1 → ~13%
    assert cleaner.derive_tax_rate(Decimal("1946.9"), Decimal("253.1")) == Decimal("0.1300")
    assert cleaner.derive_tax_rate(Decimal("0"), Decimal("0")) is None     # 除零保护
    assert cleaner.derive_tax_rate(None, Decimal("1")) is None
    assert cleaner.derive_tax_rate(Decimal("100"), Decimal("0")) == Decimal("0.0000")


def test_derive_tax_rate_bounded():
    # 脏数据(税金>不含税)使比值≥1 → 返回 None，避免溢出 Rate=Numeric(5,4) poison 整批导入
    assert cleaner.derive_tax_rate(Decimal("1"), Decimal("100")) is None
    assert cleaner.derive_tax_rate(Decimal("100"), Decimal("-5")) is None   # 负税率非法


def test_classify_channel_word_boundary():
    # 'inc' 是 'Vince' 子串，但整词匹配不误判；短名 → 个人
    assert cleaner.classify_source_channel("Vince", "Vince", None) == "个人"
    # 'Co.' 整词企业词 → 正规供应商
    assert cleaner.classify_source_channel("Abc Trading Co.", "Abc Trading Co.", None) == "正规供应商"


@pytest.mark.parametrize("raw,norm,stype,expect", [
    ("淘宝（采购）（质保一个月或者3个月）", "淘宝", None, "淘宝"),
    ("成都京东世纪贸易有限公司", "成都京东世纪贸易有限公司", None, "京东"),
    ("重庆个人-夏国庆", "重庆个人-夏国庆", None, "个人"),
    ("涂金彪", "涂金彪", None, "个人"),
    ("上海王龙实业有限公司（质保一年）", "上海王龙实业有限公司", None, "正规供应商"),
    # 供应商类型不再短路：底层回收商/维修商 也按名分类（实测人人都是回收商，不能当渠道）
    ("深圳鑫源回收有限公司", "深圳鑫源回收有限公司", "底层回收商", "正规供应商"),
    ("张三", "张三", "维修商", "个人"),
])
def test_classify_source_channel(raw, norm, stype, expect):
    assert cleaner.classify_source_channel(raw, norm, stype) == expect


# ---------------- 聚合服务 ----------------

@pytest.fixture()
def batch(db):
    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="h1")
    db.add(b)
    db.flush()
    return b


def _seed(db, batch):
    """3 单 ST（频发，混含税/不含、3 渠道）+ 1 单 MEM（偶发）+ 1 单指定采购 BULK。"""
    lines = [
        f.purchase_line("O1", "L1", "ST8000NM000A", qty="4", price="2260"),  # 含税单
        f.purchase_line("O2", "L2", "ST8000NM000A", qty="2", price="1900"),  # 不含单
        f.purchase_line("O3", "L3", "ST8000NM000A", qty="3", price="1750"),  # 不含单
        f.purchase_line("O4", "L4", "MEM-1", qty="5", price="100"),
        f.purchase_line("O5", "L5", "BULK-1", qty="10", price="50"),
    ]
    heads = {
        "O1": f.purchase_head("O1", on=date(2026, 6, 25), supplier="淘宝（采购）",
                              source_channel="淘宝", is_tax_inclusive=True,
                              tax_rate=Decimal("0.13")),
        "O2": f.purchase_head("O2", on=date(2026, 6, 24), supplier="鼎信科技",
                              source_channel="正规供应商", is_tax_inclusive=False),
        "O3": f.purchase_head("O3", on=date(2026, 6, 23), supplier="夏国庆个人",
                              source_channel="个人", is_tax_inclusive=False),
        "O4": f.purchase_head("O4", on=date(2026, 6, 22), supplier="正规乙",
                              source_channel="正规供应商", is_tax_inclusive=False),
        "O5": f.purchase_head("O5", on=date(2026, 6, 25), supplier="批量厂",
                              source_channel="正规供应商", source_type="指定采购",
                              is_tax_inclusive=False),
    }
    loader.load(db, f.purchase_result(heads, lines), batch.id, _AS_OF, mode="skip")
    db.commit()


def test_aggregation_basics(db, batch):
    _seed(db, batch)
    res = pa.analysis(db, None, days=7, freq_threshold=3, as_of=_AS_OF)
    rows = {r["pn_std"]: r for r in res["rows"]}
    # 指定采购默认剔除
    assert "BULK-1" not in rows
    assert res["kpi"]["part_count"] == 2
    assert res["kpi"]["order_count"] == 4
    # ST 频发排第一
    assert res["rows"][0]["pn_std"] == "ST8000NM000A"
    st = rows["ST8000NM000A"]
    assert st["buy_times"] == 3 and st["is_frequent"] is True
    assert st["advice"] == "批量补库"
    assert st["total_qty"] == 9
    assert rows["MEM-1"]["advice"] == "偶发"
    assert res["kpi"]["frequent_count"] == 1


def test_tax_inclusive_exclusive_prices(db, batch):
    """零计算口径：每单单价只落在自己的税口径列，另一侧留 None（不用税率反推）。"""
    _seed(db, batch)
    res = pa.analysis(db, None, days=7, as_of=_AS_OF)
    st = next(r for r in res["rows"] if r["pn_std"] == "ST8000NM000A")
    # 未税价只由不含税单贡献：O2=1900、O3=1750；O1(含税)不再反推未税
    assert st["price_ex_min"] == 1750.0 and st["price_ex_max"] == 1900.0
    # 最近一单(O1 06-25)是含税单 → 未税列最近价留空
    assert st["price_ex_last"] is None
    # 含税价只有含税单 O1 贡献（不含单留 None）
    assert st["price_inc_last"] == 2260.0
    assert st["price_inc_min"] == 2260.0 and st["price_inc_max"] == 2260.0
    # 价格趋势按未税序列(不含税单 1750→1900)：上行
    assert st["price_trend"] == "up"


def test_channel_split_and_composition(db, batch):
    _seed(db, batch)
    res = pa.analysis(db, None, days=7, as_of=_AS_OF)
    st = next(r for r in res["rows"] if r["pn_std"] == "ST8000NM000A")
    chans = {c["channel"]: c for c in st["channels"]}
    assert set(chans) == {"淘宝", "正规供应商", "个人"}
    assert chans["淘宝"]["times"] == 1 and chans["淘宝"]["price_inc_last"] == 2260.0
    assert chans["正规供应商"]["price_inc_last"] is None      # 不含单含税价留空
    # 来源构成（合计）：淘宝金额最高(9040) > 个人(5250) > 正规(3800)
    comp = {c["channel"]: c for c in res["source_composition"]}
    assert comp["淘宝"]["amount"] == 9040.0
    assert res["source_composition"][0]["channel"] == "淘宝"


def test_kpi_dual_totals_from_order_amounts(db, batch):
    """KPI/渠道双总额取订单级真实金额(零计算)：含税总额=Σamount_inc_tax、不含税=Σamount_ex_tax。"""
    lines = [
        f.purchase_line("A1", "AL1", "DUAL-X", qty="1", price="1130"),   # 含税单
        f.purchase_line("A2", "AL2", "DUAL-X", qty="1", price="1000"),   # 不含单
    ]
    heads = {
        "A1": f.purchase_head("A1", on=date(2026, 6, 25), source_channel="淘宝",
                              is_tax_inclusive=True, amount_ex_tax=Decimal("1000"),
                              amount_inc_tax=Decimal("1130")),
        "A2": f.purchase_head("A2", on=date(2026, 6, 24), source_channel="个人",
                              is_tax_inclusive=False, amount_ex_tax=Decimal("1000"),
                              amount_inc_tax=Decimal("1000")),
    }
    loader.load(db, f.purchase_result(heads, lines), batch.id, _AS_OF, mode="skip")
    db.commit()
    res = pa.analysis(db, None, days=7, as_of=_AS_OF)
    assert res["kpi"]["total_amount_inc"] == 2130.0   # 1130 + 1000
    assert res["kpi"]["total_amount_ex"] == 2000.0    # 1000 + 1000
    # 渠道拆分的双总额汇总回总额（渠道由供应商维度决定，此处两单同供应商归一渠道）
    comp = res["source_composition"]
    assert sum(c["amount_inc"] for c in comp) == 2130.0
    assert sum(c["amount_ex"] for c in comp) == 2000.0


def test_exclude_designated_toggle(db, batch):
    _seed(db, batch)
    incl = pa.analysis(db, None, days=7, exclude_designated=False, as_of=_AS_OF)
    assert any(r["pn_std"] == "BULK-1" for r in incl["rows"])
    assert incl["kpi"]["part_count"] == 3


def test_daily_sparkline_only_short_window(db, batch):
    _seed(db, batch)
    short = pa.analysis(db, None, days=7, as_of=_AS_OF)
    assert short["window"]["daily"] is True
    st = next(r for r in short["rows"] if r["pn_std"] == "ST8000NM000A")
    assert st["daily"] is not None and len(st["daily"]) == 7
    long = pa.analysis(db, None, days=90, as_of=_AS_OF)
    assert long["window"]["daily"] is False
    assert long["rows"][0]["daily"] is None


def test_drilldown_and_rbac_supplier_mask(db, batch):
    _seed(db, batch)
    part_id = db.scalar(select(DimPart.id).where(DimPart.pn_std == "ST8000NM000A"))
    drill = pa.part_purchases(db, None, part_id=part_id, days=7, as_of=_AS_OF)
    assert len(drill["items"]) == 3
    newest = drill["items"][0]                       # 06-25 含税单
    assert newest["is_tax_inclusive"] is True
    # 零计算：含税单只有含税价，未税价留空（不再 2260/1.13）
    assert newest["price_ex"] is None and newest["price_inc"] == 2260.0
    # 销售：供应商遮蔽（data_supplier=False），但进价可见（data_purchase_cost=True）
    sales = security.UserContext(user_id="liu", role="sales")
    masked = security.apply_field_visibility(drill, sales)
    assert all(it["supplier"] is None for it in masked["items"])
    assert masked["items"][0]["price_inc"] == 2260.0    # 成本对销售可见（甲方口径；此单为含税单）
    # 管理员：供应商可见
    admin = security.UserContext(user_id="admin", role="admin")
    assert security.apply_field_visibility(drill, admin)["items"][0]["supplier"] == "淘宝（采购）"


def test_source_channel_masked_for_sales(db, batch):
    _seed(db, batch)
    res = pa.analysis(db, None, days=7, as_of=_AS_OF)
    sales = security.UserContext(user_id="liu", role="sales")
    masked = security.apply_field_visibility(res, sales)
    # 来源渠道对销售遮蔽(data_supplier=False)，但金额仍可见(data_purchase_cost=True)
    assert all(c["channel"] is None for c in masked["source_composition"])
    assert any(c["amount"] is not None for c in masked["source_composition"])
    assert all(ch["channel"] is None for r in masked["rows"] for ch in r["channels"])
    # 管理员可见
    admin = security.UserContext(user_id="admin", role="admin")
    assert security.apply_field_visibility(res, admin)["source_composition"][0]["channel"] is not None


def test_drilldown_excludes_designated_by_default(db, batch):
    _seed(db, batch)
    bulk = db.scalar(select(DimPart.id).where(DimPart.pn_std == "BULK-1"))
    # 默认排除指定采购 → BULK-1 是指定采购 → 逐笔为空（与主排行口径一致）
    assert pa.part_purchases(db, None, part_id=bulk, days=7, as_of=_AS_OF)["items"] == []
    # 显式不排除 → 看得到
    incl = pa.part_purchases(db, None, part_id=bulk, days=7,
                             exclude_designated=False, as_of=_AS_OF)
    assert len(incl["items"]) == 1


def test_same_day_deterministic_last_price(db, batch):
    # 同日两单不同价：确定性排序后"最近价"稳定，不随 DB 行序漂移
    lines = [f.purchase_line("D1", "DL1", "SAMEDAY", qty="1", price="1900"),
             f.purchase_line("D2", "DL2", "SAMEDAY", qty="1", price="2100")]
    heads = {"D1": f.purchase_head("D1", on=date(2026, 6, 25), is_tax_inclusive=False),
             "D2": f.purchase_head("D2", on=date(2026, 6, 25), is_tax_inclusive=False)}
    loader.load(db, f.purchase_result(heads, lines), batch.id, _AS_OF, mode="skip")
    db.commit()
    g1 = next(r for r in pa.analysis(db, None, days=7, as_of=_AS_OF)["rows"] if r["pn_std"] == "SAMEDAY")
    g2 = next(r for r in pa.analysis(db, None, days=7, as_of=_AS_OF)["rows"] if r["pn_std"] == "SAMEDAY")
    assert g1["price_ex_last"] == g2["price_ex_last"]   # 两次一致 = 确定性
    assert g1["price_ex_last"] == 2100.0                # 后插入(更大 id)那单为"最近"
