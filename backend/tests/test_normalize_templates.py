"""各类目标准描述模板（甲方 2026-06-30 全类目模板）——幂等复现 + 关键路由。"""
import pytest

from app.services import classify
from app.services import normalize as N

# (描述, 期望标准描述, 期望二级/一级码)
CASES = [
    ("Intel Xeon Gold 6248R 24C 3.0GHz 35.75MB 205W",
     "Intel Xeon Gold 6248R 24C 3.0GHz 35.75MB 205W", "0501"),
    ("AMD EPYC 7543 32C 2.8GHz 256MB 225W", "AMD EPYC 7543 32C 2.8GHz 256MB 225W", "0502"),
    ("Smart Array P408i-a 12Gb/s SAS 2GB Cache PCIe 3.0 RAID Controller",
     "Smart Array P408i-a 12Gb/s SAS 2GB Cache PCIe 3.0 RAID Controller", "0401"),
    ("MegaRAID 9361-8i 12Gb/s SAS 8-Port 1GB Cache PCIe 3.0 RAID Controller",
     "MegaRAID 9361-8i 12Gb/s SAS 8-Port 1GB Cache PCIe 3.0 RAID Controller", "0401"),
    ("LSI 9300-8i 12Gb/s SAS 8-Port PCIe 3.0 HBA",
     "LSI 9300-8i 12Gb/s SAS 8-Port PCIe 3.0 HBA", "0402"),
    ("Intel X710-DA2 10GbE 2-Port SFP+ PCIe NIC",
     "Intel X710-DA2 10GbE 2-Port SFP+ PCIe NIC", "0403"),
    ("Mellanox ConnectX-5 25GbE 2-Port SFP28 OCP 3.0 NIC",
     "Mellanox ConnectX-5 25GbE 2-Port SFP28 OCP 3.0 NIC", "0403"),
    ("QLogic QLE2692 16Gb 2-Port PCIe Fibre Channel HBA",
     "QLogic QLE2692 16Gb 2-Port PCIe Fibre Channel HBA", "0405"),
    ("NVIDIA Tesla T4 16GB GDDR6 PCIe GPU", "Tesla T4 16GB GDDR6 PCIe GPU", "0404"),
    ("NVIDIA A100 80GB HBM2e PCIe GPU", "A100 80GB HBM2e PCIe GPU", "0404"),
    ("10Gb SFP+ 850nm 300m MMF Optical Transceiver",
     "10Gb SFP+ 850nm 300m MMF Optical Transceiver", "0901"),
    ("100Gb QSFP28 LR4 10km SMF Optical Transceiver",
     "100Gb QSFP28 LR4 10km SMF Optical Transceiver", "0901"),
    ("Mini-SAS HD SFF-8643 to SFF-8643 0.8m Cable",
     "Mini-SAS HD SFF-8643 to SFF-8643 0.8m Cable", "0902"),
    ("QSFP28 to QSFP28 3m Passive DAC Cable", "QSFP28 to QSFP28 3m Passive DAC Cable", "0902"),
    ("800W AC 80 PLUS Platinum Hot-Plug Power Supply",
     "800W AC 80 PLUS Platinum Hot-Plug Power Supply", "06"),
    ("3V CMOS Battery", "3V CMOS Battery", "07"),
    ("Dell PowerEdge R740 System Board", "Dell PowerEdge R740 System Board", "0301"),
    ("Dell PowerEdge R740 8x2.5-inch SAS Drive Backplane",
     "Dell PowerEdge R740 8x2.5-inch SAS Drive Backplane", "0302"),
    ("Dell PowerEdge R740 Hot-Swap Fan Module", "Dell PowerEdge R740 Hot-Swap Fan Module", "08"),
]


@pytest.mark.parametrize("desc,canon,code", CASES)
def test_category_templates_idempotent(desc, canon, code):
    r = N.normalize_part(desc)
    assert r["canonical_description"] == canon, f"{desc}"
    c = classify.classify_part(desc) or {}
    assert (c.get("l2_code") == code or c.get("l1_code") == code), f"{desc} → {c}"


def test_nic_with_sfp_not_routed_to_optic():
    """NIC 带 SFP+ 连接器不得被误判为光模块（卡决胜）。"""
    assert classify.classify_part("Intel X710-DA2 10GbE SFP+ NIC")["l2_code"] == "0403"


def test_fc_not_routed_to_generic_hba():
    """'Fibre Channel HBA' 归 FC(0405) 而非泛 HBA(0402)。"""
    assert classify.classify_part("QLogic QLE2692 16Gb Fibre Channel HBA")["l2_code"] == "0405"


def test_messy_psu_variant():
    r = N.normalize_part("1600W 交流 80Plus Titanium 热插拔 电源")
    assert r["canonical_description"] == "1600W AC 80 PLUS Titanium Hot-Plug Power Supply"
