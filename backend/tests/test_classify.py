"""轻量确定性分类（轻量 C）：优先级冲突黄金用例 + 三大件 + 整机/FRU。"""
import pytest

from app.services.classify import classify_part, is_whole_system

# §四+§六 冲突黄金用例（"whole" = 整机本体）
GOLDEN = [
    ("Dell PowerEdge R750 Server", "whole"),
    ("Dell PowerEdge R750 System Board", "0301"),
    ("HPE D3700 Disk Enclosure", "whole"),
    ("HPE D3700 Power Supply", "06"),
    ("IBM TS4300 Tape Library", "whole"),
    ("IBM TS4300 Fan Assembly", "08"),
    ("NetApp DS2246 Disk Shelf", "whole"),
    ("LSI MegaRAID RAID Battery BBU", "07"),    # 含 RAID 但归电池
    ("CPU Heatsink for Xeon", "08"),            # 含 CPU 但归散热
    ("Fan Power Cable", "0902"),                # 含 Fan/Power 但归线缆（形态决定词）
    ("NVMe Backplane", "0302"),                 # 含 NVMe 但归背板
    ("SAS HBA IT Mode", "0402"),                # 含 SAS 但归卡
    ("FC SFP 32G", "0901"),                     # 含 FC 但归光模块
    ("Memory Riser", "0499"),                   # 含 Memory 但归其他适配卡
]


@pytest.mark.parametrize("desc,expect", GOLDEN)
def test_priority_golden_cases(desc, expect):
    r = classify_part(desc)
    assert r is not None, f"{desc} → None"
    if expect == "whole":
        assert r.get("whole_system") is True, f"{desc} → {r}"
    else:
        assert (r.get("l2_code") == expect or r.get("l1_code") == expect), f"{desc} → {r}"


# 三大件真实描述（取自生产抽样）
@pytest.mark.parametrize("desc,l2", [
    ("32GB 2Rx8 DDR4-3200AA", "0102"),
    ("HP 16GB DDR3 1333MHz CL9 ECC RDIMM Memory Module", "0103"),
    ("三星（SAMSUNG）2TB SSD固态硬盘 SATA3.0接口 870 EVO", "0205"),
    ("Dell 1.92TB SAS 12G RI SSD PM1653", "0206"),
    ("Samsung PM9A3 U.2 1.92T 固态硬盘SSD", "0207"),
    ("Huawei HDD 8T 7.2K 3.5 SATA 硬盘", "0204"),
    ("Huawei HDD 900GB 10K 2.5 12G SAS", "0201"),
])
def test_three_big_parts(desc, l2):
    r = classify_part(desc)
    assert r and r.get("l2_code") == l2, f"{desc} → {r}"


def test_unknown_returns_none():
    assert classify_part("神秘物品 xyz123 一批") is None        # 无关键词 → 交人工


def test_disk_without_interface_defers_to_human():
    # "disk shelf" 是整机；但一个接口不明的盘应返回 None（不硬猜）
    assert classify_part("某品牌 1.2TB 盘 disk") is None


def test_is_whole_system_fru_distinction():
    assert is_whole_system("Dell PowerEdge R750 Server") is True
    assert is_whole_system("Dell R750 System Board") is False   # FRU 不算整机
    assert is_whole_system("HPE D3700 Power Supply") is False
