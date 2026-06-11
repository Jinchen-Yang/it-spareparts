"""规格抽取规则 / 替代料方向 / 迁移链单头 / 规格条件查询。"""
import os

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import select

from app.services import spec_extract


def _specs(desc):
    return {s["spec_key"]: s for s in spec_extract.extract(desc)}


def test_disk_extraction():
    s = _specs("IBM HDD 1.8TB 10K 2.5 12GB SAS For V5000")
    assert s["part_type"]["spec_value"] == "HDD"
    assert s["capacity"]["spec_value"] == "1.8TB" and float(s["capacity"]["numeric_value"]) == 1800
    assert s["interface"]["spec_value"] == "SAS"
    assert float(s["rpm"]["numeric_value"]) == 10000
    assert s["form_factor"]["spec_value"] == "2.5"


def test_ssd_gb_capacity():
    s = _specs("Intel SSD DC S4510 480GB SATA 2.5")
    assert s["part_type"]["spec_value"] == "SSD"
    assert float(s["capacity"]["numeric_value"]) == 480


def test_interface_speed_not_capacity():
    s = _specs("HDS 6TB SATA 6G 3.5")
    assert float(s["capacity"]["numeric_value"]) == 6000, "6G 是接口速率不是容量"


def test_controller_guard():
    assert spec_extract.extract(
        "HP Smart Array P840ar/2GB FBWC 12GB 2-port Internal SAS Controller") == []


def test_memory_extraction():
    s = _specs("Samsung 16GB DDR4-3200 RDIMM PC4-25600R Dual Rank x8 Module")
    assert s["part_type"]["spec_value"] == "RAM"
    assert float(s["capacity"]["numeric_value"]) == 16
    assert s["generation"]["spec_value"] == "DDR4"
    assert float(s["frequency"]["numeric_value"]) == 3200, "PC4-25600 带宽码应换算 3200MHz"
    assert s["form_factor"]["spec_value"] == "RDIMM"


def test_memory_pc_code_mts():
    s = _specs("Samsung Memory 16GB 2RX4 PC4-2666V-RB2 DDR4")
    assert float(s["frequency"]["numeric_value"]) == 2666


def test_unknown_category_empty():
    assert spec_extract.extract("NVIDIA Tesla V100 32G GPU") == []
    assert spec_extract.extract(None) == []


def test_alembic_single_head():
    """防多头：thirsty 分支将来若并行加迁移，合并后此测试会失败提醒。"""
    cfg = AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location",
                        os.path.join(os.path.dirname(__file__), "..", "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"alembic 多头: {heads}"


def test_substitute_direction(db):
    from datetime import date

    from app.etl import loader
    from app.models.system import SysImportBatch
    from app.services import part_overview, substitute
    from tests import factories as f

    b = SysImportBatch(filename="t.xlsx", file_type="purchase", file_hash="h6")
    db.add(b)
    db.flush()
    loader.load(db, f.purchase_result(
        {"O1": f.purchase_head("O1")},
        [f.purchase_line("O1", "L1", "PN-OLD"), f.purchase_line("O1", "L2", "PN-NEW")]),
        b.id, date(2026, 6, 1))
    db.commit()

    # PN-NEW 可替代 PN-OLD（单向）
    substitute.add_substitute(db, "PN-OLD", "PN-NEW", "新版兼容旧版", "t",
                              direction="one_way", substitute_type="compatible")
    subs_old = substitute.list_substitutes(db, "PN-OLD")
    assert subs_old[0]["direction"] == "incoming" and subs_old[0]["pn_std"] == "PN-NEW"
    subs_new = substitute.list_substitutes(db, "PN-NEW")
    assert subs_new[0]["direction"] == "outgoing"

    # 人工创建即生效 → 进入型号全景推荐
    from app.models.dimensions import DimPart
    old = db.scalar(select(DimPart).where(DimPart.pn_std == "PN-OLD"))
    ov = part_overview._substitutes(db, old.id)
    assert ov and ov[0]["relation"] == "可替代本型号"


def test_spec_search_filter(db):
    from datetime import date

    from app.etl import loader
    from app.models.system import SysImportBatch
    from app.services import master_data, part_overview
    from tests import factories as f

    b = SysImportBatch(filename="t.xlsx", file_type="sales", file_hash="h7")
    db.add(b)
    db.flush()
    loader.load(db, f.sales_result(
        {"S1": f.sales_head("S1")},
        [f.sales_line("S1", "L1", "DISK-BIG", description="Dell 8TB 7.2K SAS 3.5 硬盘"),
         f.sales_line("S1", "L2", "DISK-SMALL", description="Dell 1.2TB 10K SAS 2.5 硬盘"),
         f.sales_line("S1", "L3", "MEM-1", description="Samsung 32GB DDR4-2666 RDIMM")]),
        b.id, date(2026, 6, 1))
    db.commit()
    master_data.refresh(db)

    r = part_overview.search_parts(db, None, 1, 20, part_type="HDD", capacity_min=2000)
    assert [i["pn_std"] for i in r["items"]] == ["DISK-BIG"]
    r = part_overview.search_parts(db, None, 1, 20, part_type="RAM")
    assert [i["pn_std"] for i in r["items"]] == ["MEM-1"]
    assert r["items"][0]["specs"].get("generation") == "DDR4"
