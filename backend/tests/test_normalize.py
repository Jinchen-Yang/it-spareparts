"""描述标准化（甲方 2026-06-30）：固定模板 + 单位统一(Gb/s、-inch) + 品牌归一 + 精确分类。"""
import pytest

from app.services import normalize as N
from app.services import spec_extract as se


def _c(desc, pn="", brand=""):
    return N.normalize_part(desc, pn, brand)


@pytest.mark.parametrize("desc,brand,canon,l2,bnorm", [
    ("1.8TB 12Gbps 10K 128MB Cache 2.5inch SAS HDD", "东芝",
     "1.8TB 12Gb/s 10K 128MB Cache 2.5-inch SAS HDD", "SAS-HDD-2.5", "Toshiba"),
    ("10TB 6Gbps 7.2K 256MB Cache 3.5inch SATA HDD", "希捷",
     "10TB 6Gb/s 7.2K 256MB Cache 3.5-inch SATA HDD", "SATA-HDD-3.5", "Seagate"),
    ("8TB 12Gbps 7.2K 256MB Cache 3.5inch SAS HDD", "希捷",
     "8TB 12Gb/s 7.2K 256MB Cache 3.5-inch SAS HDD", "SAS-HDD-3.5", "Seagate"),
    # SSD：缺转速/缓存 → 跳过不占位
    ("1.92TB 12Gbps 2.5inch SAS SSD", "三星",
     "1.92TB 12Gb/s 2.5-inch SAS SSD", "SAS-SSD", "Samsung"),
])
def test_disk_canonical_and_brand(desc, brand, canon, l2, bnorm):
    r = _c(desc, "PN", brand)
    assert r["canonical_description"] == canon
    assert r["category_l2"] == l2
    assert r["brand_norm"] == bnorm


def test_messy_row_fully_regularized():
    """最乱：裸G / 中文转速 / 中文单位 / 中文介质 / 品牌夹在描述里。"""
    r = _c("10T 6G 7200转 256M 3.5寸 SATA机械盘 希捷", "ST10000NM0086")
    assert r["canonical_description"] == "10TB 6Gb/s 7.2K 256MB Cache 3.5-inch SATA HDD"
    assert r["category_l2"] == "SATA-HDD-3.5"          # 分类也要对（机械盘 → HDD）
    assert r["brand_norm"] == "Seagate" and r["brand_zh"] == "希捷"


def test_memory_template_with_rank_ecc():
    r = _c("Samsung 32GB 2Rx4 PC4-2666V RDIMM", "M393A4K40BB1")
    assert r["canonical_description"] == "32GB DDR4-2666 RDIMM 2Rx4 ECC"
    assert r["category_l2"] == "DDR4/PC4"
    assert r["brand_norm"] == "Samsung"                # 从描述里识别


def test_memory_english_rank_notation():
    """Dual/Single Rank x4/x8 英文写法也要抽成 Rank（线上发现的缺口）。"""
    assert _c("SK Hynix 64GB DDR4-3200 RDIMM PC4-25600R Dual Rank x4 Module for Server",
              )["canonical_description"] == "64GB DDR4-3200 RDIMM 2Rx4 ECC"
    assert _c("Samsung 1x 64GB DDR5-4800 RDIMM PC5-38400R Dual Rank x4 Module",
              )["canonical_description"] == "64GB DDR5-4800 RDIMM 2Rx4 ECC"
    assert _c("16GB DDR4-2666 RDIMM Single Rank x8")["canonical_description"] == \
        "16GB DDR4-2666 RDIMM 1Rx8 ECC"


def test_structured_fields_exposed():
    r = _c("1.8TB 12Gbps 10K 128MB Cache 2.5inch SAS HDD")
    f = r["fields"]
    assert f["capacity"] == "1.8TB" and f["interface_speed"] == "12Gb/s"
    assert f["rpm"] == "10K" and f["cache"] == "128MB"
    assert f["form_factor"] == "2.5-inch" and f["interface_type"] == "SAS" and f["media_type"] == "HDD"


def test_sff_lff_and_integer_rpm():
    assert _c("600GB 12Gbps 10000RPM SFF SAS HDD")["fields"]["form_factor"] == "2.5-inch"
    assert {s["spec_key"]: s["spec_value"] for s in se.extract(
        "8TB 12Gbps 7200转 256MB 3.5inch SAS HDD")}.get("rpm") == "7.2K"


def test_unrenderable_defers_to_human():
    assert _c("某硬盘 1TB", "X")["canonical_description"] is None   # 接口不明


def test_brand_norm_keeps_unknown_chinese():
    n, zh = N.normalize_brand("某不知名品牌")
    assert n == "某不知名品牌" and zh == "某不知名品牌"


def test_brand_chuanyun_combined_format():
    """氚云品牌是「中文（English）」合并格式 —— 必须归一到英文（线上 brand 没生效的根因）。"""
    assert N.normalize_brand("三星（Samsung）")[0] == "Samsung"
    assert N.normalize_brand("海力士（SK hynix）")[0] == "SK hynix"
    assert N.normalize_brand("华为（HUAWEI）")[0] == "Huawei"
    assert N.normalize_brand("DELL/戴尔")[0] == "Dell"
    assert N.normalize_brand("HDS")[0] == "Hitachi"
    assert N.normalize_brand("某SHP公司")[0] == "某SHP公司"   # 短键 hp 不得误命中


def test_memory_real_world_messy_variants():
    c = lambda d: N.normalize_part(d)["canonical_description"]
    assert c("SAMSUNG 内存条 64G DDR5 RECC 2R×4 4800频率") == "64GB DDR5-4800 RDIMM 2Rx4 ECC"
    assert c("64G 2R*4 PC4-3200AA REG") == "64GB DDR4-3200 RDIMM 2Rx4 ECC"
    assert c("HPE 1x 32GB DDR4-2133 LRDIMM PC4-17000P-L Quad Rank x4") == \
        "32GB DDR4-2133 LRDIMM 4Rx4 ECC"
    assert c("Sun Memory 64GB DDR4-2666 Registered DIMM") == "64GB DDR4-2666 RDIMM ECC"


def test_pc_bandwidth_code_to_speed():
    from app.services import spec_extract as se
    sp = lambda d: {s["spec_key"]: s["spec_value"] for s in se.extract(d)}
    assert sp("2GB DDR3 PC3-8500 RDIMM")["frequency"] == "1066"    # 带宽码 → 速率
    assert sp("32GB DDR4 PC4-21300 RDIMM")["frequency"] == "2666"
