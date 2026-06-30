"""GPU 信息守恒回归（甲方 2026-06-30 规格 v3）。

核心不变量：不乱补 且 不丢失。整板/整机不得塌缩成单卡；源已出现的条件关键字段
（显存/形态/数量/形态类型）必须保留；HGX 只判 platform 不判产品形态；DELTA-NEXT 类
未映射系列词保留进 product_family 并转人工，不静默丢。
"""
from app.services import standardize as S


def _s(pn, desc, brand=""):
    return S.standardize(pn, desc, brand)


def _sp(r):
    return {k: v["value"] for k, v in r["structured_specs"].items()}


# ── 回归1：A100 80G PCIE显卡（CJK 紧邻 + 无 B 显存）→ 信息守恒 AUTO_OK ──
def test_a100_80g_pcie_cjk_preserved():
    r = _s("A100PCIE", "Nvidia A100 80G PCIE显卡")
    assert r["canonical_description"] == "NVIDIA A100 80GB PCIe GPU"
    assert r["review_status"] == S.AUTO_OK
    sp = _sp(r)
    assert sp["memory_per_gpu"] == "80GB"        # 80G → 归一 80GB，不得丢
    assert sp["form_factor"] == "PCIe"           # PCIE显卡 边界，不得丢
    assert sp["product_type"] == "PCIE_GPU_CARD"


# ── 回归2：DELTA-NEXT HGX 8×H100 Baseboard（真实粘连 80GBSXM5）──
# DELTA-NEXT 是已核实的 NVIDIA HGX 家族(935-24287) → 识别 + AUTO_OK，不再误判人工
def test_delta_next_known_family_auto_ok():
    r = _s("DN", "NVIDIA DELTA-NEXT HGX GPU Baseboard,8 H100 80GBSXM5")
    assert r["canonical_description"] == "NVIDIA HGX 8× H100 80GB SXM5 GPU Baseboard"
    assert r["review_status"] == S.AUTO_OK         # 已知家族 → 放行
    sp = _sp(r)
    assert sp["product_type"] == "GPU_BASEBOARD"   # 绝不降级单卡
    assert sp["gpu_count"] == "8"
    assert sp["memory_per_gpu"] == "80GB"          # 粘连 80GBSXM5 仍抽出
    assert sp["form_factor"] == "SXM5"
    assert sp["platform"] == "HGX"
    assert sp["product_family"] == "DELTA-NEXT"


# ── PN 前缀识别家族：935-23587 → DELTA(8× A100)，无代号词也能识别 ──
def test_delta_recognized_by_part_number():
    r = _s("935-23587-0000-200", "NVIDIA BASEBOARD for 8x A100 GPUs - BOARD ONLY")
    sp = _sp(r)
    assert sp["product_family"] == "DELTA"
    assert sp["product_type"] == "GPU_BASEBOARD"
    assert sp["gpu_count"] == "8"
    assert r["review_status"] == S.AUTO_OK


# ── REDSTONE → 数量从词典推导(4×)，描述未写数量 ──
def test_redstone_count_derived_from_family():
    r = _s("RS", "NVIDIA REDSTONE HGX A100 GPU Baseboard SXM4")
    sp = _sp(r)
    assert sp["product_family"] == "REDSTONE"
    assert sp["gpu_count"] == "4"                  # 词典推导
    assert sp["product_type"] == "GPU_BASEBOARD"


# ── 未知代号（不在词典）仍保留 + 转人工 ──
def test_unknown_codename_still_reviews():
    r = _s("X", "ACME-FOO HGX H100 8-GPU Baseboard")
    sp = _sp(r)
    assert sp["product_family"] == "ACME-FOO"      # 保留，不静默丢
    assert r["review_status"] == S.REVIEW          # 未映射 → 人工
    assert sp["product_type"] == "GPU_BASEBOARD"


# ── 7 条塌缩用例：整板/整机不得变单卡 ──
def test_hgx_8gpu_baseboard_not_collapsed():
    r = _s("BB1", "HGX H100 8-GPU Baseboard")
    sp = _sp(r)
    assert sp["product_type"] == "GPU_BASEBOARD"
    assert sp["gpu_count"] == "8"
    assert "Baseboard" in r["canonical_description"]
    assert "8×" in r["canonical_description"]
    assert r["canonical_description"] != "NVIDIA H100 GPU"


def test_8x_a100_sxm4_baseboard():
    r = _s("BB2", "Nvidia 8x A100 SXM4 GPU Baseboard")
    assert r["canonical_description"] == "NVIDIA 8× A100 SXM4 GPU Baseboard"
    sp = _sp(r)
    assert sp["product_type"] == "GPU_BASEBOARD"
    assert sp["gpu_count"] == "8"
    assert sp["form_factor"] == "SXM4"


def test_chinese_baseboard_not_collapsed():
    r = _s("ZB", "NVIDIA H100 底板")
    assert r["canonical_description"] == "NVIDIA H100 GPU Baseboard"
    assert _sp(r)["product_type"] == "GPU_BASEBOARD"


def test_baseboard_board_only_8x_a100():
    r = _s("BO", "935-23587-0000-200 NVIDIA BASEBOARD for 8x A100 GPUs - BOARD ONLY")
    sp = _sp(r)
    assert sp["product_type"] == "GPU_BASEBOARD"
    assert sp["gpu_count"] == "8"
    assert "Baseboard" in r["canonical_description"]
    assert r["canonical_description"] != "NVIDIA A100 GPU"


# ── 整机服务器（8U）不得当作裸 baseboard 自动通过 → GPU_ASSEMBLY + 人工 ──
def test_built_server_is_assembly_review():
    r = _s("SVR", "Supermicro 8U X13 HGX H100 8GPU (Rear 1/0), CSE-GP801T")
    sp = _sp(r)
    assert sp["product_type"] == "GPU_ASSEMBLY"
    assert r["review_status"] == S.REVIEW
    assert r["canonical_description"] != "Supermicro H100 GPU"   # 绝不塌成单卡


# ── HGX 单独不得推 BASEBOARD（只判 platform）──
def test_hgx_alone_is_platform_not_baseboard():
    r = _s("HM", "GPU module for NVIDIA HGX platform, H100 SXM5")
    sp = _sp(r)
    assert sp.get("platform") == "HGX"
    assert sp["product_type"] != "GPU_BASEBOARD"   # 无 Board 证据、无明确多GPU → 不擅自定板


# ── 稀疏卡：源没给显存/形态 → 不强制 REVIEW（条件关键字段）──
def test_sparse_card_still_auto_ok():
    r = _s("RTX", "RTX4090")
    assert r["canonical_description"] == "NVIDIA RTX 4090 GPU"
    assert r["review_status"] == S.AUTO_OK


# ── 归一化语义：80G 与 80GB 同义，不算丢失 ──
def test_normalized_memory_equivalence():
    a = _sp(_s("a", "NVIDIA A100 80G PCIe"))
    b = _sp(_s("b", "NVIDIA A100 80GB PCIe"))
    assert a["memory_per_gpu"] == b["memory_per_gpu"] == "80GB"


# ── 假阳性边界：M10 的 "10" 不得被读成 10 个 GPU ──
def test_model_digits_not_misread_as_count():
    r = _s("M10", "NVIDIA Tesla M10 GPU 32G")
    sp = _sp(r)
    assert "gpu_count" not in sp                    # M10 不是 10 GPU
    assert sp["memory_per_gpu"] == "32GB"           # 32G 显存保留
    assert sp["product_type"] != "GPU_BASEBOARD"


# ── 现有 3 条不回归（单卡/单模块格式不变）──
def test_existing_h200_module_unchanged():
    r = _s("H200X", "NVIDIA H200 SXM5 141GB HBM3e")
    assert r["canonical_description"] == "NVIDIA H200 141GB HBM3e SXM5 GPU"
    assert not r["validation_errors"]


def test_existing_a100_pcie_unchanged():
    r = _s("A100X", "NVIDIA A100 80GB HBM2e PCIe")
    assert r["canonical_description"] == "NVIDIA A100 80GB HBM2e PCIe GPU"
