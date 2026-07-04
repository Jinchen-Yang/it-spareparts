"""确定性标准化黄金测试（甲方实施规格 §18）——把每个线上发现的错误钉死，禁止回归。"""
from app.services import standardize as S


def _s(pn, desc, brand=""):
    return S.standardize(pn, desc, brand)


# ── 硬盘三件（客户模板 2026-07-04：{容量} {接口} HDD {速率} {转速} {尺寸}，无品牌/缓存/-inch）──
def test_golden_AL15SEB18EQ():
    r = _s("AL15SEB18EQ", "1.8TB 12Gbps 10K 128MB Cache 2.5inch SAS HDD", "东芝")
    assert r["brand_norm"] == "Toshiba"                  # 品牌仍归一（单独字段），只是不进描述
    assert r["category_l2"] == "SAS-HDD-2.5"
    assert r["canonical_description"] == "1.8TB SAS HDD 12Gb 10K 2.5"
    assert r["review_status"] == "AUTO_OK"


def test_golden_ST10000NM0086():
    r = _s("ST10000NM0086", "10TB 6Gbps 7.2K 256MB Cache 3.5inch SATA HDD", "希捷")
    assert r["brand_norm"] == "Seagate"
    assert r["category_l2"] == "SATA-HDD-3.5"
    assert r["canonical_description"] == "10TB SATA HDD 6Gb 7.2K 3.5"


def test_golden_ST8000NM001A():
    r = _s("ST8000NM001A", "8TB 12Gbps 7.2K 256MB Cache 3.5inch SAS HDD", "希捷")
    assert r["brand_norm"] == "Seagate"
    assert r["category_l2"] == "SAS-HDD-3.5"
    assert r["canonical_description"] == "8TB SAS HDD 12Gb 7.2K 3.5"


# ── 内存（客户模板：{容量} {频率}{JEDEC等级} {Rank} {代际}，去 RDIMM/ECC/品牌）──
def test_golden_HMA84GR7CJR4N_no_guess_rdimm_ecc():
    r = _s("HMA84GR7CJR4N", "SK Hynix Memory 32GB 2R*4 PC4-2666V")
    assert r["brand_norm"] == "SK hynix"
    assert r["category_l2"] == "DDR4/PC4"
    assert r["canonical_description"] == "32GB 2666V 2Rx4 DDR4"
    assert "RDIMM" not in r["canonical_description"]
    assert "ECC" not in r["canonical_description"]


def test_memory_drops_rdimm_ecc_per_customer_template():
    # 客户模板不含模组类型/ECC：即便描述里有 ECC RDIMM，标准描述也只保留 容量/频率等级/Rank/代际
    r = _s("X", "Samsung 32GB 2Rx4 PC4-2666V ECC RDIMM")
    assert r["canonical_description"] == "32GB 2666V 2Rx4 DDR4"


# ── 客户模板逐条钉死（2026-07-04 主数据治理模板）──
def test_customer_template_hdd_exact():
    cases = [
        ("Seagate 4TB SATA 6Gb/s 7.2K 3.5inch 256MB HDD", "4TB SATA HDD 6Gb 7.2K 3.5"),
        ("6TB SATA 6Gbps 7200rpm 3.5 HDD", "6TB SATA HDD 6Gb 7.2K 3.5"),
        ("8TB SATA HDD 6Gb 7.2K 3.5", "8TB SATA HDD 6Gb 7.2K 3.5"),
        ("HPE 1.2TB SAS 12Gb/s 10K 2.5 SFF HDD", "1.2TB SAS HDD 12Gb 10K 2.5"),
        ("1.8TB SAS 12Gbps 10000rpm 2.5inch HDD", "1.8TB SAS HDD 12Gb 10K 2.5"),
    ]
    for desc, exp in cases:
        assert _s("", desc, "")["canonical_description"] == exp, desc


def test_customer_template_memory_exact():
    cases = [
        ("32GB DDR4 2133 2Rx4 RDIMM", "32GB 2133P 2Rx4 DDR4"),
        ("32GB DDR4-2400 2Rx4 ECC", "32GB 2400T 2Rx4 DDR4"),
        ("Samsung 32GB PC4-2666V 2Rx4 RDIMM ECC", "32GB 2666V 2Rx4 DDR4"),
        ("32GB DDR4 2933MHz 2Rx4 REG", "32GB 2933Y 2Rx4 DDR4"),
        ("32GB DDR4 3200 2Rx4 RDIMM", "32GB 3200A 2Rx4 DDR4"),
    ]
    for desc, exp in cases:
        assert _s("", desc, "")["canonical_description"] == exp, desc


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
    assert r["canonical_description"] == "Samsung 3.84TB PCIe 4.0 U.2 NVMe SSD"


def test_ssd_with_rpm_flagged():
    r = _s("X", "1.92TB 12Gb/s 2.5inch SAS SSD 7.2K")   # SSD 不该有 RPM → 校验报错
    assert "RPM" in " ".join(r["validation_errors"]) or r["validation_errors"]


# ─────────────────────────── CPU 黄金测试 ───────────────────────────
# 真实数据特性：描述是规格堆、型号常只在 PN 里；绝不编造型号/规格。
def test_cpu_spec_only_no_fabricated_model():
    """纯规格无型号 → 只渲染规格，绝不编个型号出来。"""
    r = S.standardize("", "Intel CPU 28 核心 56 线程 2.70 GHz 38.5 MB L3 Cache", "")
    assert r["object_type"] == "CPU"
    assert r["canonical_description"] == "Intel 28-Core 2.70GHz 38.5MB Cache CPU"
    assert "model" not in r["structured_specs"]            # 没证据 → 没型号
    assert r["category_l2"] == "Intel至强"


def test_cpu_model_from_pn():
    """型号在 PN 里(描述没有)→ 从 PN 抽出可靠身份。"""
    r = S.standardize("XEON.GOLD.5318Y", "Intel CPU 2.10GHz 24核心 48线程 36MB 165W", "英特尔（INTEL）")
    assert r["canonical_description"] == "Intel Xeon Gold 5318Y 24-Core 2.10GHz 36MB Cache 165W CPU"
    assert r["structured_specs"]["model"]["source"] == S.DICT
    assert r["category_l2"] == "Intel至强" and r["review_status"] == S.AUTO_OK


def test_cpu_amd_epyc_sparse_no_guess():
    """EPYC 型号在描述里，但缺频率/缓存/TDP → 不补那些字段。"""
    r = S.standardize("", "AMD EPYC 7452 32-Core 64-thread server processor", "")
    assert r["canonical_description"] == "AMD EPYC 7452 32-Core CPU"
    assert "base_freq" not in r["structured_specs"] and "tdp" not in r["structured_specs"]
    assert r["category_l2"] == "AMD"


def test_cpu_amd_full_tdp_range():
    """155/170W 配置 TDP → 取基准 155W；乱序规格也归一。"""
    r = S.standardize("", "AMD EPYC 7401P 64MB 155/170W 2.00Ghz SP3 24-Core CPU Processor", "")
    assert r["canonical_description"] == "AMD EPYC 7401P 24-Core 2.00GHz 64MB Cache 155W CPU"


def test_cpu_itanium_not_forced_to_xeon():
    """Itanium 是 Intel 但不是至强 → 归其他处理器，不硬塞 0501。"""
    r = S.standardize("", "HP Itanium2 9340 1.60ghz 20mb 2400mhz", "")
    assert "Itanium 9340" in r["canonical_description"]
    assert r["category_l2"] == "其他处理器"                # 0599，不是 Intel至强


def test_cpu_motherboard_not_misclassified_as_cpu():
    """含主板字样压过 Processor 关键词 → 主板，不是 CPU（线上发现的误判）。"""
    r = S.standardize("", "HP Mother Board BL460cG8 Intel E5-26xx Processor M", "")
    assert r["object_type"] != "CPU"


def test_cpu_xeon_e5_model_from_description():
    """E5-2680 v4 / E5645 等老 Xeon 型号在描述里 → 抽出。"""
    r = S.standardize("", "Intel Xeon E5-2680 v4 2.4GHz 14-Core", "")
    assert r["canonical_description"] == "Intel Xeon E5-2680 v4 14-Core 2.4GHz CPU"
    assert r["structured_specs"]["model"]["value"] == "E5-2680 v4"


# ── 品牌进标准描述：硬盘/内存按客户模板不含品牌，其余类目仍前缀 ──
def test_brand_omitted_for_hdd_memory_kept_for_others():
    # 客户模板 2026-07-04：硬盘/内存标准描述不含品牌（品牌仍归一到单独字段）
    r = _s("ST8000NM000A", "8TB 6Gbps 7.2K 256MB Cache 3.5inch SATA HDD", "希捷（Seagate）")
    assert r["canonical_description"] == "8TB SATA HDD 6Gb 7.2K 3.5"
    assert r["brand_norm"] == "Seagate"
    m = _s("", "Samsung 32GB 2Rx4 PC4-2666V RDIMM ECC", "三星（Samsung）")
    assert m["canonical_description"] == "32GB 2666V 2Rx4 DDR4"
    # 其余类目仍把品牌写进描述（主板渲染自带品牌，不重复）
    r2 = _s("", "IBM System Board For X3550M5", "联想（IBM）")
    assert r2["canonical_description"].count("Lenovo") == 1
    # 识别不了的品牌（占位「其他」）不猜、不前缀
    r3 = _s("PSU-450-001", "450W AC Power Supply Module", "其他")
    assert r3["canonical_description"] == "450W AC Power Supply"
