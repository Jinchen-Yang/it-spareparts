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
    # 中文交换机（接入/PoE/端口）也判整机
    assert is_whole_system("华为 CloudEngine S5735S 24×GE PoE+ + 4×GE SFP 三层接入交换机")
    assert is_whole_system("Cisco Switch 48 Ethernet 10/100 ports 4 SFP-based Gigabit")
    # 交换机的 FRU 部件（电源/风扇）不是整机
    assert not is_whole_system("Cisco Nexus Switch Power Supply 750W AC")


# ── 二轮审计(2026-07-06, 1225 复核)暴露的 SFP/卡/电池边界错分——逐条钉死防回归 ──
AUDIT_FIXES_R2 = [
    # 带 SFP+/QSFP 口的以太网/IB 网卡不再被吞成光模块（最大错分簇 ~81 个）
    ("HP Ethernet 10gb 2-port 530sfp+adapter", "0403"),
    ("IBM INTEL X520 DUAL PORT 10GBE SFP+ EMBEDDED ADAPTER", "0403"),
    ("Mellanox Ethernet Card 100GbE 1x QSFP28 port PCIe3.0 ConnectX-5", "0403"),
    ("DELL Broadcom 57810S Dual Port 10gb SFP PCIe Card Adapter", "0403"),
    ("Mellanox ConnectX-3 QDR QSFP+ InfiniBand 10 LP MCX", "0403"),
    ("Mellanox InfiniBand HCA", "0403"),
    # 超微 AOC- 加装卡不再被「AOC(有源光缆)」决定词吞成线缆
    ("超微 LSI 2108 RAID阵列卡6GB AOC-SAS2LP-H8IR", "0401"),
    ("SuperMicro Raid Card LSI2308 8 Ports 6Gb/s PCIe AOC-S2308L-L8I", "0401"),
    # 真 SFP+ 光收发器不再被 FC 速率当成光纤卡
    ("Finisar SFP 8G FC SWL 850nm SFP+ Transceiver Module", "0901"),
    ("Emulex 1 Port 8G FC Short Wave Optical – LC SFP+", "0901"),
    ("DELL 8Gb/s Fiber Channel 850NM SFP Optical Transceiver", "0901"),
    # 缓存电池带线缆本体是电池，不被 cable 决定词抢
    ("HP 96W Smart Storage Cache Battery with 145MM Cable", "07"),
    ("HP Battery 95W W/ Cable 145mm", "07"),
    # System I/O board 是主板，不被 E5-xxxx 吞成 CPU
    ("HP System I/O board Support E5-2600 series v3", "0301"),
    # 中文电源线是线缆不是电源
    ("华为 电源线 2m", "0902"),
    # 不回归：真光模块型 SFP / 真 BBU / 真 FC HBA / 纯 CPU
    ("FC SFP 32G", "0901"),
    ("LSI MegaRAID RAID Battery BBU", "07"),
    ("QLogic QLE2560 8Gb Fibre Channel HBA", "0405"),
    ("Intel Xeon Gold 6248R Processor", "0501"),
]


@pytest.mark.parametrize("desc,expect", AUDIT_FIXES_R2)
def test_audit_r2_sfp_card_battery_fixes(desc, expect):
    r = classify_part(desc)
    got = (r.get("l2_code") or r.get("l1_code")) if r else None
    assert got == expect, f"{desc!r} → {got}，期望 {expect}"


# ── 三轮审计(2026-07-06, 2000 抽样)残留 + 甲方口径「RAID 卡自带电容/电池归卡」──
AUDIT_FIXES_R3 = [
    # RAID 卡本体（带 supercap/battery/FBWC/include Cable/For 机型）→ 卡（甲方定）
    ("华为阵列卡 SR430C 2GB LSI 3108 RAID CARD RAID0,1,5,6,10,50,60", "0401"),
    ("Huawei LSI3108 1GB RAID Card SuperCap(4GB,include Cable)", "0401"),
    ("Huawei SR450C SAS/SATA RAID 卡 2GB Cache(Avago3508) For RH5288 V5", "0401"),
    ("Intel RAID RS2PI008 6GB SAS Controller Card w/ Battery", "0402"),  # 卡(HBA)，不落电池
    ("HP Raid Card SMART ARRAY / 4GB CACHE SAS For P431", "0401"),
    # 电源自带风扇 → 电源（不落风扇）
    ("IBM Power Supply 600W AC hot-plug and fan For DS4700", "06"),
    # 网卡带 DAC/SFP → 卡（DAC 只是配套线型）
    ("Intel Adapter 2-Port 10GbE Ethernet Server Adapter SFP+ DAC", "0403"),
    # 交换机线卡/IO 模块/接口板/网关模块 → 卡（其他适配卡）
    ("Cisco Catalyst 4500-X 8 Port 10GE Network Module", "0499"),
    ("36端口40GE以太网光接口板 QSFP+", "0499"),
    ("Huawei 4-Port GE SFP Front Optical Interface Card", "0403"),  # 带 SFP 口→网卡，仍是卡
    ("华为 4 port SmartIO I/O module(SFP+,16Gb FC)", "0499"),
    ("Juniper SRX5000 Series 16x 1GB SFP Services Gateway Module", "0499"),
    # 收发器 → 光模块（shortwave/单模）
    ("Cisco 4Gbps Fibre Channel Shortwave SFP LC", "0901"),
    ("Finisar Fibre Channel 4GB 10km 单模 SFP", "0901"),
    # 交换机（词界 switch, / 型号无 switch 字样）→ 整机
    ("H3C S6300-52QF L2 Ethernet Switch,48*XG Ports,4 QSFP+ Ports", "whole"),
    ("HUAWEI S5720S-52P 48 Ethernet 10/100/1000 ports,4 Gig SFP,AC", "whole"),
    # 不回归：Cache…For 模块 / 纯 BBU / 真光模块 / 纯风扇 / 纯 DAC 线
    ("Avago Cache for 9361 RAID controller cards", "07"),
    ("LSI MegaRAID RAID Battery BBU", "07"),
    ("Cisco 10GBASE-SR SFP+ Module", "0901"),
    ("Mellanox Fan module w/rear to front airflow fan", "08"),
    ("Mellanox MCP1700 40G QSFP+ Passive DAC Copper Cable 3m", "0902"),
]


@pytest.mark.parametrize("desc,expect", AUDIT_FIXES_R3)
def test_audit_r3_raid_card_psu_module_fixes(desc, expect):
    r = classify_part(desc)
    if expect == "whole":
        assert r and r.get("whole_system") is True, f"{desc!r} → {r}"
    else:
        got = (r.get("l2_code") or r.get("l1_code")) if r else None
        assert got == expect, f"{desc!r} → {got}，期望 {expect}"
