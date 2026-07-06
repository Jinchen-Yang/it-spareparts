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


# ── 大规模审计(2026-07-06, 405 个 Haiku)暴露的关键词碰撞错分——逐条钉死防回归 ──
AUDIT_FIXES = [
    # FC 硬盘不再被当光纤卡（最大错分模式 ~210 个）
    ("EMC HDD 600GB 15K 3.5 4G FC", "0208"),
    ("IBM HDD 146GB 15K 3.5-inch 4G FC Drive", "0208"),
    # M.2 SATA SSD 不再被当 NVMe
    ("480GB SATA 6Gb/s M.2 2280 SSD", "0205"),
    # GPU 短码不再撞连字符料号：Intel I350-T4 是网卡不是显卡
    ("Intel I350-T4 4-port 1GbE BASE-T PCIe NIC", "0403"),
    # 英文 "Raid Card" 归阵列卡（不因也含 HBA 而落到 HBA）
    ("LSI Raid Card 12Gb SAS PCI-E HBA", "0401"),
    # RAID 缓存/电池模块归电池，不归阵列卡
    ("Avago Cache for 9361 and 9380 SAS RAID controller cards", "07"),
    ("HP Cache 4GB For Smart Array P830i P430 缓存", "07"),
    # 不回归：真 FC HBA 卡 / 真 GPU / 带缓存的阵列卡本体 / 真 NVMe
    ("QLogic QLE2560 8Gb Fibre Channel HBA", "0405"),
    ("NVIDIA Tesla T4 16GB GPU", "0404"),
    ("MegaRAID 9361-8i 2GB Cache SAS RAID Controller", "0401"),
    ("Intel P4610 1.6TB NVMe U.2 SSD", "0207"),
]


@pytest.mark.parametrize("desc,expect", AUDIT_FIXES)
def test_audit_keyword_collision_fixes(desc, expect):
    r = classify_part(desc)
    got = r.get("l2_code") or r.get("l1_code") if r else None
    assert got == expect, f"{desc!r} → {got}，期望 {expect}"


def test_network_switch_is_whole_system():
    assert is_whole_system("Huawei switch with 48-ports 1000BASE-T, 4-ports 10GE SFP+")
    # KVM/PCIe/SAS switch 是备件组件，不是整机
    assert not is_whole_system("NVMe switch PCIe retimer card")
