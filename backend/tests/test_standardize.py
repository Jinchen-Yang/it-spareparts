"""确定性标准化黄金测试（甲方实施规格 §18）——把每个线上发现的错误钉死，禁止回归。"""
from app.services import standardize as S


def _s(pn, desc, brand=""):
    return S.standardize(pn, desc, brand)


# ── 硬盘三件（品牌归一 + 接口链路速率保留 + 尺寸驱动分类）──
def test_golden_AL15SEB18EQ():
    r = _s("AL15SEB18EQ", "1.8TB 12Gbps 10K 128MB Cache 2.5inch SAS HDD", "东芝")
    assert r["brand_norm"] == "Toshiba"
    assert r["category_l2"] == "SAS-HDD-2.5"
    assert r["canonical_description"] == "1.8TB 12Gb/s 10K 128MB Cache 2.5-inch SAS HDD"
    assert r["review_status"] == "AUTO_OK"


def test_golden_ST10000NM0086():
    r = _s("ST10000NM0086", "10TB 6Gbps 7.2K 256MB Cache 3.5inch SATA HDD", "希捷")
    assert r["brand_norm"] == "Seagate"
    assert r["category_l2"] == "SATA-HDD-3.5"
    assert r["canonical_description"] == "10TB 6Gb/s 7.2K 256MB Cache 3.5-inch SATA HDD"


def test_golden_ST8000NM001A():
    r = _s("ST8000NM001A", "8TB 12Gbps 7.2K 256MB Cache 3.5inch SAS HDD", "希捷")
    assert r["brand_norm"] == "Seagate"
    assert r["category_l2"] == "SAS-HDD-3.5"
    assert r["canonical_description"] == "8TB 12Gb/s 7.2K 256MB Cache 3.5-inch SAS HDD"


# ── 内存：无证据不补 RDIMM/ECC ──
def test_golden_HMA84GR7CJR4N_no_guess_rdimm_ecc():
    r = _s("HMA84GR7CJR4N", "SK Hynix Memory 32GB 2R*4 PC4-2666V")
    assert r["brand_norm"] == "SK hynix"
    assert r["category_l2"] == "DDR4/PC4"
    assert r["canonical_description"] == "32GB DDR4-2666 2Rx4"   # 绝不自动加 RDIMM/ECC
    assert "RDIMM" not in r["canonical_description"]
    assert "ECC" not in r["canonical_description"]


def test_memory_keeps_rdimm_ecc_when_evidenced():
    r = _s("X", "Samsung 32GB 2Rx4 PC4-2666V ECC RDIMM")
    assert r["canonical_description"] == "32GB DDR4-2666 RDIMM 2Rx4 ECC"   # 有证据才补


# ── GPU：保留品牌/型号/显存，SXM 不得变 PCIe，HBM3e 大小写 ──
def test_golden_NVIDIA_H200_sxm_not_pcie():
    r = _s("H200X", "NVIDIA H200 SXM5 141GB HBM3e")
    assert r["category_l2"] == "显卡GPU"
    assert r["canonical_description"] == "NVIDIA H200 141GB HBM3e SXM5 GPU"
    assert "PCIe" not in r["canonical_description"]    # SXM 不得被改成 PCIe
    assert "Hbm3e" not in r["canonical_description"]   # 大小写固定 HBM3e
    assert not r["validation_errors"]


def test_gpu_a100_pcie_kept():
    r = _s("A100X", "NVIDIA A100 80GB HBM2e PCIe")
    assert r["canonical_description"] == "NVIDIA A100 80GB HBM2e PCIe GPU"


def test_gpu_rtx_no_guessed_pcie_brand_derived():
    r = _s("RTXX", "RTX4090")              # 只给型号：归一 RTX 4090、品牌型号库推导、不猜形态
    assert r["canonical_description"] == "NVIDIA RTX 4090 GPU"
    assert "PCIe" not in r["canonical_description"]


# ── 硬盘无尺寸证据：不得强行分类，转人工 ──
def test_hdd_unknown_size_not_forced():
    r = _s("ST4000NM0035", "4TB 7.2K SATA HDD")    # 描述里没有 2.5/3.5
    assert r["category_l2"] is None
    assert r["review_status"] == "REVIEW_REQUIRED"
    assert r["structured_specs"]["interface_type"]["value"] == "SATA"


# ── SSD 不默认 SATA ──
def test_ssd_nvme_not_defaulted_sata():
    r = _s("X", "Samsung PM9A3 3.84TB U.2 NVMe PCIe 4.0 SSD")
    assert r["category_l2"] == "NVMe/PCIe-SSD"
    assert r["canonical_description"] == "3.84TB PCIe 4.0 U.2 NVMe SSD"


def test_ssd_with_rpm_flagged():
    r = _s("X", "1.92TB 12Gb/s 2.5inch SAS SSD 7.2K")   # SSD 不该有 RPM → 校验报错
    assert "RPM" in " ".join(r["validation_errors"]) or r["validation_errors"]
