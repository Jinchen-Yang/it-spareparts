"""服务器备件标准描述与分类（确定性系统，按甲方 2026-06-30 实施规格 v2）。

四原则：①先识别类型再抽该类型字段 ②标准描述只由结构化字段渲染（不让 LLM 自由生成）
③无可靠证据的字段不得猜测 ④分类由已抽字段决定，不为模板需要反向编造字段。

每字段带证据 {value, source, evidence}。分类与描述分离；缺分类必需字段（如硬盘尺寸）→
category_l2=None + REVIEW_REQUIRED，绝不强行分类。每类独立 renderer/validator，同输入恒同输出。

一期精确实现：硬盘(HDD)、固态(SSD)、内存(Memory)、显卡(GPU)。其余类目暂委托现有模板渲染。
"""
import re

from app.services import classify, normalize_templates
from app.services import taxonomy as T

# ── 字段证据来源 ──
EXPLICIT = "DESCRIPTION_EXPLICIT"   # 原描述明写
DERIVED = "DERIVED_SAFE"            # 可确定性推导（如 NL-SAS→7200转）
DICT = "MODEL_DICTIONARY"           # 已确认型号映射（如 RTX→NVIDIA）
UNKNOWN = "UNKNOWN"

AUTO_OK = "AUTO_OK"
REVIEW = "REVIEW_REQUIRED"

# ── 每类「允许字段 + 标准顺序」单一真值源（甲方规格 §4–7 的"允许字段"）。──
# 抽取/渲染只能用这些字段；超出此集的字段会被 _run 剔除（结构强制"字段不乱"）。
FIELD_SCHEMA = {
    "DRIVE_HDD": ["capacity", "interface_speed", "rpm", "cache", "form_factor",
                  "interface_type", "media_type"],
    "DRIVE_SSD": ["capacity", "interface_speed", "pcie_gen", "form_factor",
                  "interface_type", "media_type"],
    "MEMORY": ["capacity", "ddr_generation", "speed", "module_type", "rank", "ecc", "media_type"],
    "GPU": ["brand", "platform", "product_type", "gpu_model", "gpu_count",
            "memory_per_gpu", "memory_type", "form_factor", "product_family"],
    "CPU": ["brand", "family", "model", "cores", "base_freq", "l3_cache", "tdp"],
    "MAINBOARD": ["brand", "platform_model"],
    "BACKPLANE": ["bay_count", "interface_type", "form_factor", "subtype"],
    "RAID_HBA": ["brand", "model", "interface_type", "interface_speed", "port_count", "cache", "pcie_gen"],
    "NIC_FC": ["brand", "port_count", "speed", "connector", "card_type"],
    "OTHER_CARD": ["brand", "model", "card_type"],
    "PSU": ["wattage", "input_type", "efficiency", "redundancy"],
    "BATTERY": ["battery_type", "voltage"],
    "COOLING": ["brand", "cooling_type", "form_factor", "hot_plug"],
    "OPTICS": ["brand", "speed", "form_factor", "pmd", "wavelength", "distance", "connector", "fiber"],
    "CABLE": ["cable_type", "interface_speed", "connector", "length", "media_type"],
    "MISC": ["brand", "item_type"],
}

# ── 条件关键字段（§信息守恒）：恒关键 + "源说过才关键"。后者的判定在各 validator 里
# 按独立 source_signals 探测器与抽取字段做归一化语义比对（见 validate_gpu）。──
CRITICAL_FIELDS = {
    # gpu_model 恒关键；memory_per_gpu/form_factor/gpu_count/product_type 仅当原文出现才关键
    "GPU": ["gpu_model"],
}


def _F(value, source=EXPLICIT, evidence=None):
    return {"value": value, "source": source, "evidence": evidence or value}


def _val(specs, key):
    f = specs.get(key)
    return f["value"] if f else None


# ── 单位归一 ──
def _norm_capacity(desc):
    """容量 → NTB/NGB。容量是 TB(≤64) 或 GB(≥18)；排除链路速率 {1.5,3,6,12}G。"""
    for m in re.finditer(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*(TB|T|Tb|tb)\b", desc):
        v = float(m.group(1))
        if 0 < v <= 64 and not (len(m.group(1).split(".")[0]) > 1 and m.group(1).startswith("0")):
            n = m.group(1).rstrip("0").rstrip(".") if "." in m.group(1) else m.group(1)
            return _F(f"{n}TB", EXPLICIT, m.group(0))
    for m in re.finditer(r"(?<![A-Za-z0-9.])(\d{2,5})\s*(GB|G)\b", desc):
        v = int(m.group(1))
        if 18 <= v <= 999:                 # 企业盘最小 ~36GB；排除 6G/12G 链路
            return _F(f"{v}GB", EXPLICIT, m.group(0))
    return None


def _norm_rpm(desc):
    m = re.search(r"(\d+(?:\.\d+)?)\s*K\b", desc, re.I)
    if m and 4 <= float(m.group(1)) <= 16:
        k = m.group(1)
        return _F(f"{k.rstrip('.0') if '.' in k else k}K".replace("..", "."), EXPLICIT, m.group(0))
    m = re.search(r"(\d{4,5})\s*(?:转|RPM|R/MIN)", desc, re.I)
    if m and 4000 <= int(m.group(1)) <= 16000:
        return _F(f"{int(m.group(1))/1000:g}K", EXPLICIT, m.group(0))
    return None


_FORM_NOTATIONS = re.compile(r"(2\.5|3\.5)\s*(?:寸|英寸|inch|in|\"|″)?", re.I)


def _norm_form(desc):
    m = _FORM_NOTATIONS.search(desc)
    if m:
        return _F(f"{m.group(1)}-inch", EXPLICIT, m.group(0))
    if re.search(r"\bSFF\b", desc, re.I):
        return _F("2.5-inch", EXPLICIT, "SFF")
    if re.search(r"\bLFF\b", desc, re.I):
        return _F("3.5-inch", EXPLICIT, "LFF")
    return None                            # 缺尺寸 → 不猜


def _norm_link_speed(desc):
    """接口链路速率 → NGb/s。扫所有 Gb 匹配取首个 ∈{1.5,3,6,12,24}（跳过容量 500GB）。"""
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*Gb(?:ps|/?\s*s)?\b", desc, re.I):
        v = float(m.group(1))
        if v in (1.5, 3, 6, 12, 24):
            return _F(f"{v:g}Gb/s", EXPLICIT, m.group(0))
    for m in re.finditer(r"(?<![A-Za-z0-9])(\d{1,2})\s*G(?![Bb])\b", desc):  # 裸 6G/12G
        v = float(m.group(1))
        if v in (3, 6, 12, 24):
            return _F(f"{v:g}Gb/s", EXPLICIT, m.group(0))
    return None


def _norm_cache(desc):
    m = re.search(r"(\d+)\s*MB?\b(?!\s*/?\s*s)", desc, re.I)
    if m and 1 <= int(m.group(1)) <= 4096:
        return _F(f"{m.group(1)}MB", EXPLICIT, m.group(0))
    return None


# ─────────────────────────── 硬盘 HDD ───────────────────────────
def _hdd_interface(desc):
    low = desc.lower()
    if re.search(r"nl[\s-]?sas|nearline\s*sas", low):
        return _F("NL-SAS", EXPLICIT, "NL-SAS")
    if re.search(r"\bsas\b", low):
        return _F("SAS", EXPLICIT, "SAS")
    if re.search(r"\bsata\b|\bsata[\s-]?[23]", low):
        return _F("SATA", EXPLICIT, "SATA")
    if re.search(r"\bfc\b|fibre channel|fiber channel|fc-al", low):
        return _F("FC", EXPLICIT, "FC")
    return None


def extract_hdd(desc):
    specs = {}
    cap = _norm_capacity(desc)
    if cap:
        specs["capacity"] = cap
    itf = _hdd_interface(desc)
    if itf:
        # NL-SAS 在 canonical 里用 SAS 接口词，子类用近线语义
        specs["interface_type"] = _F("SAS" if itf["value"] == "NL-SAS" else itf["value"],
                                      itf["source"], itf["evidence"])
        specs["_nl"] = itf["value"] == "NL-SAS"
    spd = _norm_link_speed(desc)
    if spd:
        specs["interface_speed"] = spd
    rpm = _norm_rpm(desc)
    if rpm:
        specs["rpm"] = rpm
    elif specs.get("_nl"):                 # 近线盘恒 7.2K，可安全推导
        specs["rpm"] = _F("7.2K", DERIVED, "NL-SAS→7.2K")
    ff = _norm_form(desc)
    if ff:
        specs["form_factor"] = ff
    ca = _norm_cache(desc)
    if ca:
        specs["cache"] = ca
    specs["media_type"] = _F("HDD", EXPLICIT, "HDD")
    return specs


def render_hdd(specs):
    """{容量} {接口速率} {转速} {缓存} {尺寸} {接口} HDD —— 只渲染已抽到的字段。"""
    if not (_val(specs, "capacity") and _val(specs, "interface_type")):
        return None
    seg = [_val(specs, "capacity")]
    for k in ("interface_speed", "rpm"):
        if _val(specs, k):
            seg.append(_val(specs, k))
    if _val(specs, "cache"):
        seg.append(f"{_val(specs, 'cache')} Cache")
    if _val(specs, "form_factor"):
        seg.append(_val(specs, "form_factor"))
    seg += [_val(specs, "interface_type"), "HDD"]
    return " ".join(seg)


def classify_hdd_l2(specs):
    """接口 + 介质 + 尺寸 → 二级。缺尺寸 → None（转人工，绝不强分）。"""
    itf, ff = _val(specs, "interface_type"), _val(specs, "form_factor")
    if not ff:
        return None
    small = ff == "2.5-inch"
    if itf == "SAS":
        return "0201" if small else "0202"
    if itf == "SATA":
        return "0203" if small else "0204"
    if itf == "FC":
        return "0208"
    return None


def validate_hdd(l2, specs, desc):
    errs = []
    itf, ff = _val(specs, "interface_type"), _val(specs, "form_factor")
    want = {"0201": ("SAS", "2.5-inch"), "0202": ("SAS", "3.5-inch"),
            "0203": ("SATA", "2.5-inch"), "0204": ("SATA", "3.5-inch")}.get(l2)
    if want and (itf, ff) != want:
        errs.append(f"分类 {T.CATEGORY_NAMES.get(l2)} 要求 {want}，实抽 ({itf},{ff})")
    if re.search(r"\bnvme\b", desc, re.I):
        errs.append("HDD 描述出现 NVMe，疑似数据错误")
    return errs


# ─────────────────────────── 固态 SSD ───────────────────────────
_PCIE_GEN = re.compile(r"(?:pcie|gen)\s*([345])(?:\.0)?", re.I)
_SSD_FORM = re.compile(r"\b(U\.2|U\.3|M\.2|E1\.S|E1\.L|E3\.S|E3\.L|AIC|HHHL|2\.5|3\.5)\b", re.I)


def _ssd_interface(desc):
    low = desc.lower()
    if "nvme" in low or "u.2" in low or "u.3" in low or re.search(r"pcie\s*[345]", low):
        return _F("NVMe", EXPLICIT, "NVMe")
    if re.search(r"\bsas\b", low):
        return _F("SAS", EXPLICIT, "SAS")
    if re.search(r"\bsata\b", low):
        return _F("SATA", EXPLICIT, "SATA")
    return None                            # 看到 SSD 不默认 SATA


def extract_ssd(desc):
    specs = {"media_type": _F("SSD", EXPLICIT, "SSD")}
    cap = _norm_capacity(desc)
    if cap:
        specs["capacity"] = cap
    itf = _ssd_interface(desc)
    if itf:
        specs["interface_type"] = itf
    if itf and itf["value"] == "NVMe":
        m = _PCIE_GEN.search(desc)
        if m:
            specs["pcie_gen"] = _F(f"PCIe {m.group(1)}.0", EXPLICIT, m.group(0))
    else:
        spd = _norm_link_speed(desc)
        if spd:
            specs["interface_speed"] = spd
    m = _SSD_FORM.search(desc)
    if m:
        v = m.group(1)
        specs["form_factor"] = _F(f"{v}-inch" if v in ("2.5", "3.5") else v.upper(), EXPLICIT, v)
    return specs


def render_ssd(specs):
    itf = _val(specs, "interface_type")
    if not (_val(specs, "capacity") and itf):
        return None
    seg = [_val(specs, "capacity")]
    if itf == "NVMe":
        if _val(specs, "pcie_gen"):
            seg.append(_val(specs, "pcie_gen"))
        if _val(specs, "form_factor"):
            seg.append(_val(specs, "form_factor"))
        seg += ["NVMe", "SSD"]
    else:
        if _val(specs, "interface_speed"):
            seg.append(_val(specs, "interface_speed"))
        if _val(specs, "form_factor"):
            seg.append(_val(specs, "form_factor"))
        seg += [itf, "SSD"]
    return " ".join(seg)


def classify_ssd_l2(specs):
    itf = _val(specs, "interface_type")
    return {"SATA": "0205", "SAS": "0206", "NVMe": "0207"}.get(itf)


def validate_ssd(l2, specs, desc):
    errs = []
    if re.search(r"\d+(?:\.\d+)?\s*K\b|\d{4,5}\s*RPM|\d{4,5}\s*转", desc, re.I):
        errs.append("SSD 描述出现转速(RPM)，疑似数据错误")
    return errs


# ─────────────────────────── 内存 Memory ───────────────────────────
_PC_BW = {5300: 667, 6400: 800, 8500: 1066, 10600: 1333, 12800: 1600, 14900: 1866,
          17000: 2133, 19200: 2400, 21300: 2666, 23400: 2933, 25600: 3200, 28800: 3600,
          38400: 4800, 44800: 5600, 51200: 6400}
_RANK = re.compile(r"(\d)\s*R\s*[x×*]\s*(\d)", re.I)
_RANK_WORDS = re.compile(r"(Single|Dual|Quad|Octal)[\s-]*Rank[\s,]*[x×*]\s*(\d)", re.I)
_RANK_N = {"single": "1", "dual": "2", "quad": "4", "octal": "8"}
_MEM_FORM = re.compile(r"\b(LRDIMM|RDIMM|UDIMM|SODIMM|FB-DIMM)\b", re.I)


def extract_memory(desc):
    specs = {"media_type": _F("RAM", EXPLICIT, "RAM")}
    m = re.search(r"(?<![A-Z0-9-])(\d{1,3})\s*GB?\b", desc, re.I)
    if m and int(m.group(1)) in (1, 2, 4, 8, 16, 32, 48, 64, 96, 128, 192, 256, 512):
        specs["capacity"] = _F(f"{m.group(1)}GB", EXPLICIT, m.group(0))
    gen = None
    m = re.search(r"DDR([2-5])", desc, re.I)
    if m:
        gen, gev = m.group(1), m.group(0)
    else:
        m = re.search(r"PC([2-5])L?-", desc, re.I)
        if m:
            gen, gev = m.group(1), m.group(0)
    if gen:
        specs["ddr_generation"] = _F(f"DDR{gen}", EXPLICIT, gev)
    freq = None
    m = re.search(r"DDR[2-5][\s-](\d{3,4})\b", desc, re.I) or re.search(r"(\d{3,4})\s*MHZ", desc, re.I) \
        or re.search(r"(\d{3,4})\s*(?:频率|MT/?s)", desc, re.I)
    if m:
        freq = int(m.group(1))
    if freq is None:
        m = re.search(r"PC[2-5]L?-(\d{4,5})", desc, re.I)
        if m:
            code = int(m.group(1))
            freq = _PC_BW.get(code) or (code // 8 if code >= 10000 else code)
    if freq is None:
        m = re.search(r"\b([2-9]\d{3})(?:V|Y|W|U|T|P|R|AA|N|K)\b", desc)
        if m:
            freq = int(m.group(1))
    if freq and 200 <= freq <= 9000:
        specs["speed"] = _F(str(freq), EXPLICIT, str(freq))
    m = _MEM_FORM.search(desc)
    if m:
        specs["module_type"] = _F(m.group(1).upper(), EXPLICIT, m.group(0))
    elif re.search(r"\b(REG|RECC|REGISTERED)\b", desc, re.I):
        specs["module_type"] = _F("RDIMM", EXPLICIT, "REG")
    m = _RANK.search(desc)
    if m:
        specs["rank"] = _F(f"{m.group(1)}Rx{m.group(2)}", EXPLICIT, m.group(0))
    else:
        m = _RANK_WORDS.search(desc)
        if m:
            specs["rank"] = _F(f"{_RANK_N[m.group(1).lower()]}Rx{m.group(2)}", EXPLICIT, m.group(0))
    if re.search(r"\becc\b|recc", desc, re.I):          # 只在显式 ECC/RECC 时
        specs["ecc"] = _F("ECC", EXPLICIT, "ECC")
    return specs


def render_memory(specs):
    """{容量} {代际}-{速率} {模组} {Rank} {ECC} —— 无证据不补 RDIMM/ECC。"""
    if not (_val(specs, "capacity") and _val(specs, "ddr_generation")):
        return None
    gen = _val(specs, "ddr_generation")
    if _val(specs, "speed"):
        gen = f"{gen}-{_val(specs, 'speed')}"
    seg = [_val(specs, "capacity"), gen]
    for k in ("module_type", "rank", "ecc"):
        if _val(specs, k):
            seg.append(_val(specs, k))
    return " ".join(seg)


def classify_mem_l2(specs):
    return {"DDR5": "0101", "DDR4": "0102", "DDR3": "0103",
            "DDR2": "0104"}.get(_val(specs, "ddr_generation"), "0199")


def validate_memory(l2, specs, desc):
    errs = []
    gen = _val(specs, "ddr_generation")
    if gen and l2 in ("0101", "0102", "0103", "0104"):
        exp = {"0101": "DDR5", "0102": "DDR4", "0103": "DDR3", "0104": "DDR2"}[l2]
        if gen != exp:
            errs.append(f"分类 {l2} 与代际 {gen} 冲突")
    return errs


# ─────────────────────────── 显卡 GPU ───────────────────────────
# 设计（甲方规格 v3）：①extractor 高精度产出字段 ②_gpu_signals 高召回独立探测（只预警、
# 绝不回写；与 extractor 正则不复用）③product_type 确定性建模——HGX 只判 platform 不判形态，
# 整板/整机绝不渲成单卡 ④信息守恒按"归一化语义字段"比对（80G≡80GB），非字符串包含。
_GPU_MODEL = re.compile(
    r"(GeForce\s+RTX\s*\d{3,4}\w*|RTX\s*\d{3,4}\w*|Tesla\s+\w+|Quadro\s+\w+|"
    r"\bA100\b|\bA800\b|\bA30\b|\bA40\b|\bA2\b|\bH100\b|\bH200\b|\bH800\b|\bV100\b|"
    r"\bT4\b|\bL4\b|\bL40S?\b|\bL20\b|\bP100\b|\bP40\b|\bM10\b|"
    r"\bMI\d{2,3}\w*|Radeon\s+\w+|Instinct\s+\w+)", re.I)
_GPU_BRAND_BY_MODEL = [
    (re.compile(r"rtx|gtx|geforce|tesla|quadro|^a\d{1,3}|h[12]00|h800|v100|t4|l4|l40|l20|p100|p40|m10", re.I), "NVIDIA"),
    (re.compile(r"\bmi\d|radeon|instinct|firepro", re.I), "AMD"),
]
# 形态：不依赖 \b（吃 PCIE显卡 / 80GBSXM5 这类 CJK 紧邻或粘连）
_GPU_FORM = re.compile(r"(SXM5|SXM4|SXM2|SXM|OAM|PCIe|PCI-?e|Mezzanine)", re.I)
_GPU_VRAM_TYPE = re.compile(r"(HBM3e|HBM3|HBM2e|HBM2|GDDR6X|GDDR6|GDDR5X|GDDR5)\b", re.I)
# 显存(extractor 高精度)：2–4 位 + G/GB，容许粘连(80GBSXM5)，区间过滤排除链路速率
_GPU_MEM_EX = re.compile(r"(?<![A-Za-z0-9.])(\d{2,4})\s*G(?:B)?", re.I)
# 数量：8× / 8x A100 / 8xA100 / 8-GPU / 8 GPU / 8 H100；negative-lookbehind 防 M10、A100 被读成数量
_GPU_COUNT = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,2})\s*[×xX]\s*(?=[A-Za-z])"
    r"|(?<![A-Za-z])(\d{1,2})\s*[-\s]?GPU\b"
    r"|(?<![A-Za-z])(\d{1,2})\s+(?:H100|H200|A100|A800|H800|V100|L40S?|A30|A40|MI\d{2,3})", re.I)
_GPU_BOARD = re.compile(r"base\s*board|gpu\s*board|底板", re.I)
_GPU_SERVER = re.compile(r"\b\d+U\b|server|整机|superpod", re.I)
# 系列代号(DELTA-NEXT)：纯字母多段，剔除料号(935-23587)与规格串(V100-16G-PCIE)
_GPU_CODE = re.compile(r"\b([A-Z][A-Z0-9]{2,}(?:-[A-Z0-9]+)+)\b")
_GPU_CODE_KNOWN = re.compile(
    r"^(NVIDIA|AMD|INTEL|GPU|SXM\d?|OAM|PCIE|PCI-E|HBM\d?E?|GDDR\d?X?|HGX|DGX|"
    r"H100|H200|A100|A800|H800|V100|L40S?|L20|A30|A40|A2|T4|L4|P100|P40|M10|RTX|GTX|MI\d+)$", re.I)
_GPU_SPECISH = re.compile(r"^\d+G[B]?$|^PCIE$|^PCI-E$|^SXM\d?$|^OAM$|^\d+$", re.I)

# NVIDIA HGX baseboard 家族词典（联网核实 2026-06-30）：代号/PN前缀 → 配置。
# 已知家族 → 识别为 baseboard + 推导数量，不再当"未映射代号"转人工。
_NV_BB_FAMILY = {                       # 代号 → {数量, 形态}（型号仍取描述显式）
    "DELTA-NEXT": {"count": 8, "form": "SXM5"},   # 8× H100/H200 SXM5（935-24287）
    "REDSTONE":   {"count": 4, "form": "SXM4"},   # 4× A100 SXM4 低成本（935-22687）
    "UMBRIEL":    {"count": 8, "form": "SXM5"},   # 8× B200 Blackwell（935-26287）
    "DELTA":      {"count": 8, "form": "SXM4"},   # 8× A100 SXM4（935-23587）
}
_NV_BB_PN = {                           # NVIDIA baseboard PN 前缀 → 代号（无歧义）
    "935-24287": "DELTA-NEXT", "935-22687": "REDSTONE",
    "935-26287": "UMBRIEL", "935-23587": "DELTA",
}
_NV_BB_NAMES = sorted(_NV_BB_FAMILY, key=len, reverse=True)   # 长名先匹配(DELTA-NEXT 先于 DELTA)


def _gpu_family(desc, pn=""):
    """识别已知 HGX baseboard 家族（代号或 PN 前缀）。返回 (代号, 配置) 或 (None, None)。"""
    text = f"{pn or ''} {desc or ''}"
    compact = re.sub(r"[^0-9]", "", text)
    for pref, code in _NV_BB_PN.items():
        if pref in text or pref.replace("-", "") in compact:
            return code, _NV_BB_FAMILY[code]
    if re.search(r"base\s*board|底板|\bhgx\b|sxm", desc, re.I):   # 需 baseboard 语境，防 'DELTA' 误命中
        up = text.upper()
        for code in _NV_BB_NAMES:
            if re.search(rf"\b{re.escape(code)}\b", up):
                return code, _NV_BB_FAMILY[code]
    return None, None


def _plausible_vram(v):
    return 8 <= v <= 512            # 排除 6G/12G 链路速率与离谱值


def _norm_gpu_form(raw):
    raw = raw.upper()
    if raw.startswith("PCI"):
        return "PCIe"
    if raw.startswith("MEZ"):
        return "Mezzanine"
    return raw                      # SXM5/SXM4/SXM/OAM


def _form_family(v):
    """形态归一到族（比较用）：SXM5→SXM、PCIe→PCIe、OAM→OAM。"""
    if not v:
        return None
    v = v.upper()
    if v.startswith("PCI"):
        return "PCIe"
    if v.startswith("SXM"):
        return "SXM"
    if v.startswith("MEZ"):
        return "Mezzanine"
    return v


def _gpu_count(desc):
    m = _GPU_COUNT.search(desc)
    if m:
        for g in m.groups():
            if g:
                return int(g)
    return None


def _gpu_codename(desc):
    for tok in _GPU_CODE.findall(desc):
        segs = tok.split("-")
        if any(_GPU_CODE_KNOWN.match(s) or _GPU_SPECISH.match(s) or s.isdigit() for s in segs):
            continue
        if len([s for s in segs if s.isalpha() and len(s) >= 3]) >= 2:
            return tok
    return None


def _gpu_product_type(desc, count, form):
    """确定性形态判定。HGX 只产出 platform，绝不单独定 BASEBOARD。"""
    low = desc.lower()
    platform = "HGX" if re.search(r"\bhgx\b", low) else None
    board = bool(_GPU_BOARD.search(desc))
    dgx = bool(re.search(r"\bdgx\b", low))
    server = bool(_GPU_SERVER.search(desc))
    n = count or 0
    if dgx or (server and n >= 2):              # 整机/服务器(8U/整机/DGX) 优先 → 组件，转人工
        return "GPU_ASSEMBLY", platform
    if board or (platform and n >= 2):          # 明确板词，或 HGX+明确多GPU
        return "GPU_BASEBOARD", platform
    if n >= 2:                                   # 多GPU但无板词 → 不擅自定，转人工
        return "UNKNOWN", platform
    if form and (_form_family(form) in ("SXM", "OAM")):
        return "GPU_MODULE", platform
    if _form_family(form) == "PCIe":
        return "PCIE_GPU_CARD", platform
    return "UNKNOWN", platform


def extract_gpu(desc, brand_raw="", pn=""):
    """高精度抽取。只产出有可靠证据的字段（不猜测、不回写宽松探测值）。"""
    specs = {}
    m = _GPU_MODEL.search(desc)
    if m:
        model = re.sub(r"\s+", " ", m.group(1).strip())
        model = re.sub(r"RTX\s*(\d)", r"RTX \1", model, flags=re.I)   # RTX4090 → RTX 4090
        specs["gpu_model"] = _F(model, EXPLICIT, m.group(0))
    bn = T.recognize_brand(brand_raw) or T.recognize_brand(desc)
    if bn:
        specs["brand"] = _F(bn, EXPLICIT, bn)
    elif specs.get("gpu_model"):
        for rx, b in _GPU_BRAND_BY_MODEL:
            if rx.search(specs["gpu_model"]["value"]):
                specs["brand"] = _F(b, DICT, f"{specs['gpu_model']['value']}→{b}")
                break
    count = _gpu_count(desc)
    if count and count >= 2:
        specs["gpu_count"] = _F(str(count), EXPLICIT, str(count))
    for mm in _GPU_MEM_EX.finditer(desc):       # 显存：首个合理值（per-GPU 语义）
        v = int(mm.group(1))
        if _plausible_vram(v):
            specs["memory_per_gpu"] = _F(f"{v}GB", EXPLICIT, mm.group(0).strip())
            break
    m = _GPU_VRAM_TYPE.search(desc)
    if m:
        specs["memory_type"] = _F(m.group(1), EXPLICIT, m.group(0))   # 原大小写：HBM3e
    fm = _GPU_FORM.search(desc)
    form = None
    if fm:
        form = _norm_gpu_form(fm.group(1))
        specs["form_factor"] = _F(form, EXPLICIT, fm.group(0))
    ptype, platform = _gpu_product_type(desc, count, form)
    fam_code, fam = _gpu_family(desc, pn)        # 已知 HGX baseboard 家族（词典核实）
    if fam_code:
        ptype = "GPU_BASEBOARD"                  # 家族即整板，绝不降级
        platform = "HGX"
        specs["product_family"] = _F(fam_code, DICT, fam_code)   # DICT=已映射 → 不转人工
        if "gpu_count" not in specs:
            specs["gpu_count"] = _F(str(fam["count"]), DICT, f"{fam_code}→{fam['count']}GPU")
        if "form_factor" not in specs:
            specs["form_factor"] = _F(fam["form"], DICT, f"{fam_code}→{fam['form']}")
    else:
        code = _gpu_codename(desc)               # 未知代号：保留进 product_family + 转人工（不静默丢）
        if code:
            specs["product_family"] = _F(code, EXPLICIT, code)
    specs["product_type"] = _F(ptype, DERIVED, ptype)
    if platform:
        specs["platform"] = _F(platform, EXPLICIT, platform)
    return specs


def render_gpu(specs):
    """按 product_type 分叉：整板/整机带 Baseboard/Assembly 名词 + 数量；卡/模块仍为 {…} GPU。"""
    model = _val(specs, "gpu_model")
    if not model:
        return None
    pt = _val(specs, "product_type")
    brand, mem = _val(specs, "brand"), _val(specs, "memory_per_gpu")
    mtype, form = _val(specs, "memory_type"), _val(specs, "form_factor")
    count, platform = _val(specs, "gpu_count"), _val(specs, "platform")
    if pt in ("GPU_BASEBOARD", "GPU_ASSEMBLY"):
        seg = []
        if brand:
            seg.append(brand)
        if platform:
            seg.append(platform)
        if count:
            seg.append(f"{count}×")
        seg.append(model)
        for v in (mem, mtype, form):
            if v:
                seg.append(v)
        seg.append("GPU Baseboard" if pt == "GPU_BASEBOARD" else "GPU Assembly")
        return " ".join(seg)
    seg = [v for v in (brand, model, mem, mtype, form) if v]
    seg.append("GPU")
    return " ".join(seg)


# ── 独立 source_signals 探测器（高召回，仅信息守恒预警，绝不写入结果；正则与 extractor 不复用）──
_SIG_MEM = re.compile(r"(?<![A-Za-z0-9.])(\d{2,4})\s*G[Bb]?(?![A-Za-z0-9])")
_SIG_FORM = re.compile(r"(SXM[0-9]?|OAM|Mezzanine|PCI-?e)", re.I)


def _gpu_signals(desc):
    mems = [int(m.group(1)) for m in _SIG_MEM.finditer(desc)]
    plausible = [v for v in mems if _plausible_vram(v)]
    fm = _SIG_FORM.search(desc)
    return {
        "mem": plausible[0] if plausible else None,
        "mem_hit": bool(mems),
        "form": _form_family(fm.group(1)) if fm else None,
        "count": _gpu_count(desc),
        "board": bool(_GPU_BOARD.search(desc) or re.search(r"\bhgx\b|\bdgx\b", desc, re.I)),
    }


def validate_gpu(desc, specs):
    """信息损失校验（归一化语义比对）+ 矛盾校验。errs 非空 → REVIEW_REQUIRED。"""
    errs = []
    low = desc.lower()
    form = _val(specs, "form_factor")
    if re.search(r"\bsxm", low) and _form_family(form) == "PCIe":
        errs.append("原描述 SXM 不得变成 PCIe")
    if "pcie" in low and _form_family(form) == "SXM":
        errs.append("原描述 PCIe 不得变成 SXM")
    if not _val(specs, "gpu_model"):
        errs.append("GPU 型号丢失")
        return errs
    sig = _gpu_signals(desc)
    ex_mem = None
    if _val(specs, "memory_per_gpu"):
        mm = re.search(r"(\d+)", _val(specs, "memory_per_gpu"))
        ex_mem = int(mm.group(1)) if mm else None
    if sig["mem"] is not None and ex_mem != sig["mem"]:
        errs.append(f"显存丢失/不一致（源 {sig['mem']}GB）")
    elif sig["mem"] is None and sig["mem_hit"] and ex_mem is None:
        errs.append("疑似显存信号未确认（possible_missing_signal）")   # 不自动补，转人工
    if sig["form"] and _form_family(form) != sig["form"]:
        errs.append(f"形态丢失（源 {sig['form']}）")
    if (sig["count"] or 0) >= 2 and not _val(specs, "gpu_count"):
        errs.append("GPU 数量丢失")
    pt = _val(specs, "product_type")
    if pt == "GPU_ASSEMBLY":
        errs.append("整机/组件需人工确认（GPU_ASSEMBLY）")
    elif pt == "UNKNOWN" and (sig["count"] or 0) >= 2:
        errs.append("多GPU形态未确定（missing_product_type）")
    pf = specs.get("product_family")
    if pf and pf["source"] != DICT:              # 仅"未映射"代号转人工；已知家族(DICT)放行
        errs.append(f"未映射系列词 {pf['value']} 待人工（unmapped_series_codename）")
    return errs


# ─────────────────────────── 处理器 CPU ───────────────────────────
# 真实数据：描述多是"规格堆"(核心/频率/缓存/TDP)，型号身份常只在 PN 里(XEON.GOLD.5318Y)。
# 故型号优先取 PN(可靠身份)，规格取描述；无可靠型号绝不编造。
_CPU_CORES = re.compile(r"(\d{1,3})\s*(?:核心|核|[\- ]?[Cc]ores?)\b", re.I)
_CPU_FREQ = re.compile(r"(\d(?:\.\d{1,2})?)\s*GHz", re.I)        # 基频 GHz（FSB 是 MHz，不会误命中）
_CPU_MB = re.compile(r"(\d{1,3}(?:\.\d)?)\s*MB\b", re.I)         # 缓存 MB（多个取最大，L3 通常最大）
_CPU_TDP = re.compile(r"(\d{2,3})(?:\s*/\s*\d{2,3})?\s*W\b", re.I)   # 155/170W → 取 155
_CPU_FAMILY = [
    (re.compile(r"\bxeon\b|至强", re.I), "Xeon"),
    (re.compile(r"\bepyc\b", re.I), "EPYC"),
    (re.compile(r"\bopteron\b", re.I), "Opteron"),
    (re.compile(r"\bitanium2?\b", re.I), "Itanium"),
    (re.compile(r"\bpower\d\b", re.I), "POWER"),
    (re.compile(r"\bsparc\b", re.I), "SPARC"),
    (re.compile(r"\bcore\s+i[3579]\b", re.I), "Core"),
]
_CPU_BRAND_BY_FAMILY = {"Xeon": "Intel", "Itanium": "Intel", "Core": "Intel",
                        "EPYC": "AMD", "Opteron": "AMD", "POWER": "IBM", "SPARC": "Oracle"}
# PN 编码的型号（氚云常见格式）：XEON.GOLD.5318Y / XEON.SILVER.4314 / EPYC.7452
_PN_XEON = re.compile(r"XEON[._\s-]*(GOLD|SILVER|PLATINUM|BRONZE)[._\s-]*(\d{3,4}[A-Z]*)", re.I)
_PN_XEON_E = re.compile(r"XEON[._\s-]*(E[57])[\s._-]?(\d{4}[A-Z]?\d?)", re.I)
_PN_EPYC = re.compile(r"EPYC[._\s-]*(\d{3,4}[A-Z]*)", re.I)
# 描述里的型号：Xeon Gold 5318Y / E5-2680 v4 / E5645 / EPYC 7452 / Itanium2 9340 / POWER6
_D_TIER = re.compile(r"\b(Gold|Silver|Platinum|Bronze)\s+(\d{3,4}[A-Z]*)\b", re.I)
_D_XEON_E = re.compile(r"\b(E[357]-\d{4})(?:\s*(v\d))?\b", re.I)     # E5-2680 v4
_D_XEON_LEGACY = re.compile(r"\b([ELWX][357]\d{3})\b")              # E5645 / X5650 / L5640
_D_EPYC = re.compile(r"\bEPYC\s+(\d{3,4}[A-Z]*)\b", re.I)
_D_ITANIUM = re.compile(r"\bItanium2?\s+(\d{3,4})\b", re.I)
_D_POWER = re.compile(r"\b(POWER\d)\b", re.I)


def _cpu_family(text):
    for rx, fam in _CPU_FAMILY:
        if rx.search(text):
            return fam
    return None


def _cpu_model(desc, pn):
    """型号优先 PN（可靠身份），其次描述；都没有 → None（不猜）。返回 (型号字段, 推出的系列)。"""
    m = _PN_XEON.search(pn)
    if m:
        return _F(f"{m.group(1).title()} {m.group(2).upper()}", DICT, f"PN:{m.group(0)}"), "Xeon"
    m = _PN_XEON_E.search(pn)
    if m:
        return _F(f"{m.group(1).upper()}-{m.group(2).upper()}", DICT, f"PN:{m.group(0)}"), "Xeon"
    m = _PN_EPYC.search(pn)
    if m:
        return _F(m.group(1).upper(), DICT, f"PN:{m.group(0)}"), "EPYC"
    m = _D_TIER.search(desc)
    if m:
        return _F(f"{m.group(1).title()} {m.group(2).upper()}", EXPLICIT, m.group(0)), "Xeon"
    m = _D_XEON_E.search(desc)
    if m:
        model = m.group(1).upper() + (f" {m.group(2).lower()}" if m.group(2) else "")
        return _F(model, EXPLICIT, m.group(0)), "Xeon"
    m = _D_XEON_LEGACY.search(desc)
    if m:
        return _F(m.group(1).upper(), EXPLICIT, m.group(0)), "Xeon"
    m = _D_EPYC.search(desc)
    if m:
        return _F(m.group(1).upper(), EXPLICIT, m.group(0)), "EPYC"
    m = _D_ITANIUM.search(desc)
    if m:
        return _F(m.group(1), EXPLICIT, m.group(0)), "Itanium"
    m = _D_POWER.search(desc)
    if m:
        return _F(m.group(1).upper(), EXPLICIT, m.group(0)), "POWER"
    return None, None


def extract_cpu(desc, pn="", brand_raw=""):
    specs = {}
    model_f, fam_from_model = _cpu_model(desc, pn)
    family = fam_from_model or _cpu_family(f"{desc} {pn}")
    if model_f:
        specs["model"] = model_f
    if family:
        specs["family"] = _F(family, model_f["source"] if model_f else EXPLICIT, family)
    bnorm, _bzh = T.resolve_brand(brand_raw, desc)          # 显式品牌 → 描述
    if not bnorm and family:
        bnorm = _CPU_BRAND_BY_FAMILY.get(family)            # 再由系列确定性推导
    if bnorm:
        specs["brand"] = _F(bnorm, EXPLICIT if brand_raw else DICT, bnorm)
    m = _CPU_CORES.search(desc)
    if m:
        specs["cores"] = _F(f"{int(m.group(1))}-Core", EXPLICIT, m.group(0).strip())
    m = _CPU_FREQ.search(desc)
    if m:
        specs["base_freq"] = _F(f"{m.group(1)}GHz", EXPLICIT, m.group(0).strip())
    caches = [float(x) for x in _CPU_MB.findall(desc)]
    if caches:
        specs["l3_cache"] = _F(f"{max(caches):g}MB", EXPLICIT, f"{max(caches):g}MB")
    m = _CPU_TDP.search(desc)
    if m:
        specs["tdp"] = _F(f"{int(m.group(1))}W", EXPLICIT, m.group(0).strip())
    return specs


def render_cpu(specs):
    """{品牌} {系列} {型号} {核数}-Core {基频}GHz {缓存} Cache {TDP}W CPU —— 只拼已抽字段。"""
    if not (_val(specs, "model") or _val(specs, "cores")):
        return None        # 既无型号又无核数 = 无可辨识身份 → 转人工
    seg = [_val(specs, k) for k in ("brand", "family", "model", "cores", "base_freq") if _val(specs, k)]
    if _val(specs, "l3_cache"):
        seg.append(f"{_val(specs, 'l3_cache')} Cache")
    if _val(specs, "tdp"):
        seg.append(_val(specs, "tdp"))
    seg.append("CPU")
    return " ".join(seg)


def classify_cpu_l2(specs):
    """系列优先：Xeon→0501、EPYC/Opteron→0502、其余系列→0599；无系列证据按品牌兜底。"""
    fam = _val(specs, "family")
    if fam == "Xeon":
        return "0501"
    if fam in ("EPYC", "Opteron"):
        return "0502"
    if fam:                                    # Itanium/POWER/SPARC/Core → 其他处理器
        return "0599"
    brand = _val(specs, "brand")
    if brand == "Intel":
        return "0501"
    if brand == "AMD":
        return "0502"
    return "0599"


def validate_cpu(l2, specs, desc):
    errs = []
    fam, brand = _val(specs, "family"), _val(specs, "brand")
    if fam == "Xeon" and brand == "AMD":
        errs.append("Xeon 系列品牌应为 Intel")
    if fam == "EPYC" and brand == "Intel":
        errs.append("EPYC 系列品牌应为 AMD")
    if l2 == "0501" and brand == "AMD":
        errs.append("分类 Intel至强 但品牌为 AMD")
    if l2 == "0502" and brand == "Intel":
        errs.append("分类 AMD 但品牌为 Intel")
    return errs


# 在 FIELD_SCHEMA 里新增一条（放在 "CPU" 之后即可）：
#     "MAINBOARD": ["brand", "platform_model"],

# ─────────────────────────── 系统主板 MAINBOARD ───────────────────────────
# 真实数据：身份 = 品牌 + 平台/机型型号（DL380 Gen10 / R740 / X3650 M5 / RH2288 V5）。
# 规格极少；型号串里混大量 FRU/料号噪声（FRU 01GV937 / 0PHYDR / 03025BHC）必须剔除。
# 无可辨识平台/机型（如 "hp 主板"）→ 型号缺失 → 转人工，绝不拿品牌当型号。
_MB_TYPE = re.compile(
    r"(server\s*board|system\s*board|systemboard|mother\s*board|motherboard|"
    r"main\s*board|mainboard|planar\b|主逻辑板|系统板|服务器主板|整机主板|工作站主板|刀片\s*主板|主板)", re.I)
# 噪声/非机型 token（整词剔除）：装配/规格/插槽词，绝不当机型
_MB_NOISE_TOK = re.compile(
    r"^(FRU|P/?N|Memory|Upgrades?|includes?|Sub|for|supports?|unit|assembly|cage|"
    r"module|brd|board|dual|cpu|ddr[2-5]|dimm|chipset|socket|intel|amd|planar|series|"
    r"blade|server|expansion|controller|core|ghz|atx|i\d|v\d|m\d|gen\d?|g\d{1,2})$", re.I)
# 插槽/封装码（LGA2011 / FCLGA2011-3 / SP3）—— 非机型，剔除
_MB_SOCKET = re.compile(r"^(?:F?C?LGA\d+|SP\d+|FCBGA\d+)", re.I)
# 平台/机型型号 token：整词必须"含字母且含数字"（首段即带数字），可带连字符/点子段
_MB_MODEL_TOK = re.compile(r"^[A-Za-z][A-Za-z0-9]*\d[A-Za-z0-9]*(?:[.\-][A-Za-z0-9]+)*$")
# 紧跟机型的代际尾巴（Gen10/G35/V5/M5/i4）保留为型号一部分
_MB_GEN_TOK = re.compile(r"^(Gen\s*\d+|G\d{1,2}|V\d{1,2}|M\d{1,2}|i\d)$", re.I)
# 明显是料号(非机型)：0 开头长码 / 长十六进制 / 纯长数字 / 字母段+≥5 位连号（PBAG50768…）
_MB_PARTNO = re.compile(r"^(?:0[0-9A-Z]{4,}|[0-9A-F]{6,}|\d{5,}|[A-Z]{2,5}\d{5,})", re.I)
_MB_HAS_ALPHA = re.compile(r"[A-Za-z]")
# 品牌占位符（非真实品牌）→ 不渲染
_MB_BRAND_PLACEHOLDER = re.compile(r"^(其他|其它|未知|无|other|n/?a)$", re.I)
# 非本类信号（交换机时钟板/扩展器/控制器模块/装配件）→ 升人工
_MB_NOT = re.compile(
    r"时钟同步板|交换机|expander\s*motherboard|controller.*motherboard|"
    r"motherboard\s*cage|planar\s+and\s+cage", re.I)


def _mb_brand(brand_raw, desc):
    """品牌：先 resolve_brand；若回退成 raw「中文（English）」串，取括号内英文；占位符→None。"""
    bnorm, _bzh = T.resolve_brand(brand_raw, desc)
    if not bnorm:
        return None
    m = re.match(r"^[^（(]+[（(]([A-Za-z][\w .-]*)[)）]\s*\d?$", bnorm)
    if m:                                      # 字典未收的品牌(Sugon/Powerleader…)→ 取英文
        bnorm = m.group(1).strip()
    if _MB_BRAND_PLACEHOLDER.match(bnorm.strip()):     # "其他"/"未知" 不是品牌
        return None
    return bnorm


def _mb_platform(desc):
    """抽平台/机型型号：去类型后缀/品牌/CJK → 整词扫描，首个"字母+数字"机型 + 代际尾巴。无 → None。"""
    s = _MB_TYPE.sub(" ", desc)                # 去主板类型词
    s = re.sub(r"[（(][A-Za-z][\w .-]*[)）]", " ", s)   # 去品牌括注
    s = re.sub(r"[一-鿿]", " ", s)             # 去 CJK（避免黏连导致整词判定失效）
    s = re.sub(r"\(.*?\)", " ", s)             # 去括号内容 (5462)
    toks = re.split(r"[\s,/]+", s)
    for i, tok in enumerate(toks):
        tok = tok.strip(".-")
        if not tok or len(tok) < 2:
            continue
        if _MB_NOISE_TOK.match(tok) or _MB_SOCKET.match(tok) or _MB_PARTNO.match(tok):
            continue
        if not _MB_MODEL_TOK.match(tok) or not _MB_HAS_ALPHA.search(tok):
            continue                           # 必须含字母+数字的机型形态
        model = tok
        if i + 1 < len(toks):                  # 紧邻代际尾巴（M2/V3/Gen8）并入型号
            nxt = toks[i + 1].strip(".-")
            if _MB_GEN_TOK.match(nxt) and not re.search(r"Gen\d|[GVM]\d|i\d$", tok, re.I):
                model = f"{tok} {nxt}"
        model = re.sub(r"[.\-](Gen\s*\d+)$", r" \1", model, flags=re.I)   # DL360p.Gen8→DL360p Gen8
        model = re.sub(r"\s+", " ", model).strip()
        model = re.sub(r"Gen\s*(\d)", r"Gen\1", model, flags=re.I)
        return _F(model, EXPLICIT, tok)
    return None


def extract_mainboard(desc, pn="", brand_raw=""):
    specs = {}
    b = _mb_brand(brand_raw, desc)
    if b:
        specs["brand"] = _F(b, EXPLICIT if brand_raw else DICT, b)
    plat = _mb_platform(desc)
    if plat:
        specs["platform_model"] = plat
    return specs


def render_mainboard(specs):
    """{品牌} {平台/机型型号} System Board —— 缺平台型号则无身份 → None（转人工）。"""
    if not _val(specs, "platform_model"):
        return None
    seg = []
    if _val(specs, "brand"):
        seg.append(_val(specs, "brand"))
    seg.append(_val(specs, "platform_model"))
    seg.append("System Board")
    return " ".join(seg)


def classify_mainboard_l2(specs):
    """主板无子码：识别字段（平台型号）齐 → 固定 0301；否则 None（转人工）。"""
    if _val(specs, "platform_model"):
        return "0301"
    return None


def validate_mainboard(l2, specs, desc):
    errs = []
    if _MB_NOT.search(desc):
        errs.append("疑似非系统主板（交换机时钟板/扩展器/控制器模块/装配件），应转人工")
    return errs


# 验证结论：四函数 + FIELD_SCHEMA 增项逻辑正确，无需修改（原样回传）。
# 已独立复跑确认：黄金 8/8 过（经真实 _run 集成路径）；真实桶 130 行用候选自身管线得 AUTO_OK 112/130 = 86.2%，与声称一致；
# 无字段编造、无 evidence-free 渲染、无单位漂移、classify_l2 缺证据不硬认子类。
# 唯一需落地的是 header 已声明的集成（见 issues#1），不是四函数本身的缺陷，故不改函数体。
#
# FIELD_SCHEMA 增项（standardize.py 顶部 FIELD_SCHEMA 字典）：
#   "BACKPLANE": ["bay_count", "interface_type", "form_factor", "subtype"],



_BP_BAY_CJK = re.compile(r"(\d{1,3})\s*(?:盘位|盘|槽|口)")
_BP_BAY_EN = re.compile(r"(?<![\d.])(\d{1,3})\s*[-\s]?(?:Slots?|Solts?|Bays?|Ports?|SFF|HDDs?)\b", re.I)
_BP_BAY_X = re.compile(r"(?<![\d.])(\d{1,3})\s*[xX*]\s*(?:2\.5|3\.5)(?![\d.])")
_BP_NVME = re.compile(r"NVMe", re.I)
_BP_SASSATA = re.compile(r"SAS\s*[/-]\s*SATA|SATA\s*[/-]\s*SAS", re.I)
_BP_SAS = re.compile(r"(?<![A-Za-z0-9])SAS(?![A-Za-z0-9])|[x*]\s*SAS\b", re.I)
_BP_SATA = re.compile(r"(?<![A-Za-z0-9])SATA(?![A-Za-z0-9])|[/x*]\s*SATA\b", re.I)
_BP_SCSI = re.compile(r"\bSCSI\b|\bU320\b|Ultra320", re.I)
_BP_PATA = re.compile(r"\bPATA\b", re.I)
_BP_POWER = re.compile(r"\bPower\s*Back\s*Plane\b|电源背板", re.I)
_BP_MID = re.compile(r"\bMidplane\b|\bCenterplane\b|中间板", re.I)
_BP_IO = re.compile(r"\bI/?O\s*Back\s*plane\b|\bPCI\s*I/?O\b", re.I)
_BP_SYS = re.compile(r"\bSystem\s*Back\s*plane\b", re.I)
_BP_MEDIA = re.compile(r"\bMedia\s*Back\s*plane\b|\bOptical\s*Device\b|Diskette", re.I)
_BP_DRIVE = re.compile(r"\bDisk\s*Back\s*Plane\b|\bDrive\s*Back\s*plane\b|\bHard\s*Disk\b|"
                       r"\bHard\s*Drive\b|硬盘|HDD\b", re.I)
_BP_ANY = re.compile(r"\bBack\s*plane\b|背板|\bMidplane\b|\bCenterplane\b", re.I)
_BP_NOISE = re.compile(r"连接线|的SAS线|Riser\s*Card|扣卡.*线", re.I)


def _bp_form(desc):
    m = re.search(r"(2\.5|3\.5)\s*(?:寸|英寸|inch|in|\"|″)", desc, re.I)
    if m:
        return _F(f"{m.group(1)}-inch", EXPLICIT, m.group(0))
    if re.search(r"\bSFF\b", desc, re.I):
        return _F("2.5-inch", EXPLICIT, "SFF")
    if re.search(r"\bLFF\b", desc, re.I):
        return _F("3.5-inch", EXPLICIT, "LFF")
    m = re.search(r"\d{1,3}\s*[xX*]\s*(2\.5|3\.5)\b", desc)
    if m:
        return _F(f"{m.group(1)}-inch", EXPLICIT, m.group(0))
    m = re.search(r"(?<![\d.])(2\.5|3\.5)(?![\d])", desc)
    if m:
        return _F(f"{m.group(1)}-inch", EXPLICIT, m.group(0))
    return None


def _bp_bay(desc):
    m = _BP_BAY_CJK.search(desc)
    if m and 1 <= int(m.group(1)) <= 100:
        return _F(f"{int(m.group(1))}-Bay", EXPLICIT, m.group(0))
    m = _BP_BAY_X.search(desc)
    if m and 1 <= int(m.group(1)) <= 100:
        return _F(f"{int(m.group(1))}-Bay", EXPLICIT, m.group(0))
    m = _BP_BAY_EN.search(desc)
    if m and 1 <= int(m.group(1)) <= 100:
        return _F(f"{int(m.group(1))}-Bay", EXPLICIT, m.group(0))
    return None


def _bp_interface(desc):
    nvme, sasfam = bool(_BP_NVME.search(desc)), bool(_BP_SAS.search(desc) or _BP_SATA.search(desc))
    if nvme and sasfam:
        return None
    if nvme:
        return _F("NVMe", EXPLICIT, "NVMe")
    if _BP_SASSATA.search(desc):
        return _F("SAS/SATA", EXPLICIT, "SAS/SATA")
    if _BP_SAS.search(desc) and _BP_SATA.search(desc):
        return _F("SAS/SATA", EXPLICIT, "SAS+SATA")
    if _BP_SAS.search(desc):
        return _F("SAS", EXPLICIT, "SAS")
    if _BP_SATA.search(desc):
        return _F("SATA", EXPLICIT, "SATA")
    if _BP_SCSI.search(desc):
        return _F("SCSI", EXPLICIT, "SCSI")
    if _BP_PATA.search(desc):
        return _F("PATA", EXPLICIT, "PATA")
    return None


def _bp_subtype(desc):
    """功能子型：仅在出现明确功能/硬盘词时返回；都没有 → None（不猜）。"""
    if _BP_POWER.search(desc):
        return _F("Power", EXPLICIT, "Power Backplane")
    if _BP_IO.search(desc):
        return _F("I/O", EXPLICIT, "I/O Backplane")
    if _BP_SYS.search(desc):
        return _F("System", EXPLICIT, "System Backplane")
    if _BP_MEDIA.search(desc):
        return _F("Media", EXPLICIT, "Media Backplane")
    if _BP_MID.search(desc):
        return _F("Mid", EXPLICIT, "Midplane")
    if _BP_DRIVE.search(desc):
        return _F("Drive", EXPLICIT, "Disk Backplane")
    return None


def extract_backplane(desc):
    specs = {}
    if _BP_NOISE.search(desc) or not _BP_ANY.search(desc):
        return specs
    sub = _bp_subtype(desc)
    if sub and sub["value"] != "Drive":
        specs["subtype"] = sub
        return specs
    bay = _bp_bay(desc)
    if bay:
        specs["bay_count"] = bay
    itf = _bp_interface(desc)
    if itf:
        specs["interface_type"] = itf
    ff = _bp_form(desc)
    if ff:
        specs["form_factor"] = ff
    if sub:
        specs["subtype"] = sub
    elif bay or ff or (itf and itf["value"] != "SCSI"):
        sig = (bay or ff or itf)["evidence"]
        specs["subtype"] = _F("Drive", DERIVED, f"背板+{sig}→Drive")
    return specs


def render_backplane(specs):
    """{盘位} {尺寸} {接口} Drive Backplane —— 只渲染抽到的结构字段；缺子型(无身份)→None。"""
    sub = _val(specs, "subtype")
    if not sub:
        return None
    seg = []
    if sub == "Drive":
        for k in ("bay_count", "form_factor", "interface_type"):
            if _val(specs, k):
                seg.append(_val(specs, k))
        seg.append("Drive Backplane")
    else:
        seg.append({"Power": "Power Backplane", "I/O": "I/O Backplane",
                    "System": "System Backplane", "Media": "Media Backplane",
                    "Mid": "Midplane"}[sub])
    return " ".join(seg)


def classify_backplane_l2(specs):
    """0302 背板无子码：确证是背板(抽到 subtype)即 0302，否则 None 转人工。"""
    return "0302" if _val(specs, "subtype") else None


def validate_backplane(l2, specs, desc):
    errs = []
    sub = _val(specs, "subtype")
    # 注：此条恒不触发——extract_backplane 对非 Drive 子型 early-return，bay/interface 不会与之共存。冗余护栏。
    if sub and sub != "Drive" and (_val(specs, "bay_count") or _val(specs, "interface_type")):
        errs.append(f"{sub} 背板不应带盘位/接口字段（疑似抽错）")
    if re.search(r"\bRAID\b|阵列卡", desc, re.I) and not _BP_ANY.search(desc):
        errs.append("疑似 RAID 卡误入背板")
    return errs


# ─────────────────────────── 存储控制器 阵列卡RAID(0401) / HBA卡(0402) ───────────────────────────
# 真实数据(118行)归纳：①0402桶里大量其实是 FC HBA(光纤卡)→属 0405，本器只认 SAS/SATA 存储控制器，
# FC-only 卡作 REVIEW_REQUIRED 不强渲。②可靠字段：型号(P440ar/9300-8i/H730P)、接口速率(12Gb/s,
# 卡上常写 12GB 实指 12Gb/s)、端口(8i/16e 内外部记法 或 2-Port)、缓存(1GB/2GB Cache，仅 RAID)、PCIe 代。
# ③RAID vs HBA：见 RAID/Smart Array/MegaRAID/PERC/ServeRAID/缓存 → 0401；纯 SAS/SATA 直通(Tri-Mode/
# HBA330/93xx-Ni) → 0402。无 SAS/SATA 存储身份(纯 FC、NIC、SSD、整机) → None 转人工。
# FIELD_SCHEMA 追加：
#   "RAID_HBA": ["brand", "model", "interface_type", "interface_speed", "port_count", "cache", "pcie_gen"],

# 型号：Smart Array Pxxx / PERC Hxxx / MegaRAID / ServeRAID / 裸 93xx-Ni / 94xx-Ne / 430-Ni /
# HBA330+ / HBA355i / H240 / H221 / SAS3408（芯片号）。只取可靠身份串，取不到不编造。
# 修正：H/P-三位数字分支加 (?!-[A-Za-z0-9]) 机箱后缀负向先行，挡掉 H460-B1-F 这类整机/机箱代号
# 被误当成控制器型号编造（H460-B1-F 是新华三刀片整机，非 HBA 卡身份）。真实控制器型号(H710P/
# H730P/H240/H221/P440ar/P244br)不带 -B1-F 这种机箱后缀，不受影响。
_SC_MODEL = re.compile(
    r"(Smart\s+Array\s+[A-Z]\d{3}\w*|MegaRAID\s+[\w-]+|PERC\s+[HM]\d{3}\w*|Perc\s+[HM]\d{3}\w*|"
    r"ServeRAID\s+[\w-]+|HBA3\d{2}\w*\+?|"
    r"\b[HP]\d{3}[a-z]{0,3}\b(?!-[A-Za-z0-9])|"      # P440ar / H710P / H730 / H240 / H221（排除 H460-B1-F 机箱后缀）
    r"\b9\d{3}-\d{1,2}[ie](?:\d{1,2}[ie])?\b|"        # 9300-8i / 9206-16e / 9212-4i4e
    r"\b430-\d{1,2}[iIeE]\b|"                         # 430-8i / 430-16I
    r"\bSAS3\d{3}\b)", re.I)                           # SAS3408 芯片

# 端口：8i/16e(内外部记法) 或 N-Port/N端口/Single|Dual Port。8i=8内部口
_PORT_NI = re.compile(r"(?<![\w-])(\d{1,2})\s*([ie])\b", re.I)         # 8i 16e（带数字）
_PORT_NPORT = re.compile(r"(\d{1,2})\s*[-\s]*(?:port|端口|口)\b", re.I)
_PORT_WORD = re.compile(r"\b(single|dual|quad)[\s-]*port\b", re.I)
_PORT_WORD_N = {"single": "1", "dual": "2", "quad": "4"}

# 接口速率：12Gb/s / 6Gbps / 6Gbp/s(typo) / 12GB/s(卡上 GB 实指 Gb/s) / 裸 6G SAS。取 SAS/SATA 链路速率。
_SC_SPEED = re.compile(r"(?<![\w.])(\d{1,2})\s*Gb(?:ps?|/?\s*s)?(?![A-Za-z])", re.I)
_SC_SPEED_BARE = re.compile(r"(?<![\w.])(\d{1,2})\s*G\s+SAS\b", re.I)   # 6G SAS（裸 G 紧跟 SAS）
_PCIE_GEN_SC = re.compile(r"PCIe?\s*(?:GEN\s*)?([3-6])(?:\.\d)?|GEN\s*([3-6])\b", re.I)
_CACHE_SC = re.compile(r"(\d+(?:\.\d+)?)\s*GB?\s*Cach", re.I)          # 1GB Cache / 1GB Cach(typo)

_RAID_SIGNAL = re.compile(r"\bRAID\b|Smart\s+Array|MegaRAID|PERC|Perc|ServeRAID", re.I)
_TRIMODE = re.compile(r"tri[\s-]?mode", re.I)
_FC_ONLY = re.compile(r"\bFC\b|fibre\s*channel|fiber\s*channel", re.I)
_PASSTHRU = re.compile(r"直通|it\s*mode|不支持RAID|jbod", re.I)


def _sc_interface(desc):
    low = desc.lower()
    if _TRIMODE.search(desc):
        return _F("Tri-Mode", EXPLICIT, "Tri-Mode")
    has_sas = bool(re.search(r"\bsas\b", low))
    has_sata = bool(re.search(r"\bsata\b", low))
    if has_sas and has_sata:
        return _F("SAS/SATA", EXPLICIT, "SAS/SATA")
    if has_sas:
        return _F("SAS", EXPLICIT, "SAS")
    if has_sata:
        return _F("SATA", EXPLICIT, "SATA")
    return None                                  # 纯 FC / SCSI / NIC → 无 SAS/SATA 存储身份，不猜


def extract_raid_hba(desc, pn="", brand_raw=""):
    specs = {}
    bn = T.recognize_brand(brand_raw) or T.recognize_brand(desc)   # 仅已确认规范品牌入渲染，未识别不塞 中文（）原文
    if bn:
        specs["brand"] = _F(bn, EXPLICIT if T.recognize_brand(brand_raw) else DICT, bn)
    m = _SC_MODEL.search(desc)
    if m:
        model = re.sub(r"\s+", " ", m.group(1).strip())
        specs["model"] = _F(model, EXPLICIT, m.group(0))
    itf = _sc_interface(desc)
    if itf:
        specs["interface_type"] = itf
    elif specs.get("model") and re.match(r"9\d{3}-|HBA3\d{2}|SAS3\d{3}", specs["model"]["value"], re.I):
        # LSI/Broadcom 93xx/94xx、Dell HBA3xx、SAS3xxx 芯片确定性为 SAS 控制器（型号字典安全推导）
        specs["interface_type"] = _F("SAS", DICT, f"{specs['model']['value']}→SAS")
    for mm in _SC_SPEED.finditer(desc):
        v = int(mm.group(1))
        if v in (3, 6, 12, 24):                  # SAS/SATA 链路速率（排除 8G/16G 等 FC 速率与容量）
            specs["interface_speed"] = _F(f"{v}Gb/s", EXPLICIT, mm.group(0).strip())
            break
    if "interface_speed" not in specs:
        mm = _SC_SPEED_BARE.search(desc)
        if mm and int(mm.group(1)) in (3, 6, 12, 24):
            specs["interface_speed"] = _F(f"{int(mm.group(1))}Gb/s", EXPLICIT, mm.group(0).strip())
    # 端口：优先型号自带的 -Ni/-Ne 内外部记法(9311-8i→8i)，再扫描描述里的 8i/16e / N-Port / Single|Dual
    pmodel = re.search(r"-(\d{1,2})([ie])(?:(\d{1,2})([ie]))?$", _val(specs, "model") or "", re.I)
    if pmodel:
        pc = f"{pmodel.group(1)}{pmodel.group(2).lower()}"
        if pmodel.group(3):
            pc += f"{pmodel.group(3)}{pmodel.group(4).lower()}"
        specs["port_count"] = _F(pc, EXPLICIT, pmodel.group(0))
    elif _PORT_NI.search(desc):
        mp = _PORT_NI.search(desc)
        specs["port_count"] = _F(f"{mp.group(1)}{mp.group(2).lower()}", EXPLICIT, mp.group(0).strip())
    elif _PORT_NPORT.search(desc):
        mp = _PORT_NPORT.search(desc)
        specs["port_count"] = _F(f"{mp.group(1)}-Port", EXPLICIT, mp.group(0).strip())
    elif _PORT_WORD.search(desc):
        mp = _PORT_WORD.search(desc)
        specs["port_count"] = _F(f"{_PORT_WORD_N[mp.group(1).lower()]}-Port", EXPLICIT, mp.group(0).strip())
    mc = _CACHE_SC.search(desc)
    if mc and _RAID_SIGNAL.search(desc):         # 缓存只在 RAID 上下文取（HBA 无缓存；避开容量误读）
        n = mc.group(1).rstrip("0").rstrip(".") if "." in mc.group(1) else mc.group(1)
        specs["cache"] = _F(f"{n}GB", EXPLICIT, mc.group(0).strip())
    mg = _PCIE_GEN_SC.search(desc)
    if mg:
        g = mg.group(1) or mg.group(2)
        specs["pcie_gen"] = _F(f"PCIe {g}.0", EXPLICIT, mg.group(0).strip())
    # RAID 身份(分类用，非渲染字段)：控制器族名/RAID 字样即 RAID 证据；直通/IT 模式覆盖为 HBA
    if _RAID_SIGNAL.search(desc) and not _PASSTHRU.search(desc):
        specs["_raid"] = _F(True, EXPLICIT, "RAID")
    return specs


def render_raid_hba(specs):
    """{品牌} {型号} {接口} {接口速率} {端口} {缓存} {PCIe} <Controller> —— 只渲染已抽字段。
    需有 型号 或 (SAS/SATA接口) 作为可辨识存储身份；缺则 None 转人工。有 RAID 证据→RAID Controller。"""
    itf = _val(specs, "interface_type")
    if not (_val(specs, "model") or itf):
        return None
    seg = []
    for k in ("brand", "model", "interface_type", "interface_speed", "port_count"):
        if _val(specs, k):
            seg.append(_val(specs, k))
    if _val(specs, "cache"):
        seg.append(f"{_val(specs, 'cache')} Cache")
    if _val(specs, "pcie_gen"):
        seg.append(_val(specs, "pcie_gen"))
    seg.append("RAID Controller" if (_val(specs, "cache") or _val(specs, "_raid")) else "HBA")
    return " ".join(seg)


def classify_raid_hba_l2(specs):
    """RAID 证据(缓存/控制器族名/RAID 字样且非直通) → 0401。否则需 SAS/SATA 存储身份(接口或型号)
    才定 0402；无任何 SAS/SATA 存储身份(纯 FC/NIC/SSD) → None 转人工(本器不认 0405 光纤卡)。"""
    itf = _val(specs, "interface_type")
    if _val(specs, "cache") or _val(specs, "_raid"):
        return "0401"
    if itf in ("SAS", "SATA", "SAS/SATA", "Tri-Mode"):
        return "0402"
    return None


def validate_raid_hba(l2, specs, desc):
    errs = []
    cache = _val(specs, "cache")
    if l2 == "0401" and not cache and not _RAID_SIGNAL.search(desc):
        errs.append("分类 阵列卡RAID 但无 RAID/缓存证据")
    if l2 == "0402" and cache:
        errs.append("分类 HBA卡 但抽到缓存(应为 RAID)")
    if l2 == "0402" and _RAID_SIGNAL.search(desc) and not _PASSTHRU.search(desc):
        errs.append("分类 HBA卡 但描述含 RAID")
    itf = _val(specs, "interface_type")
    if itf in ("SAS", "SATA", "SAS/SATA", "Tri-Mode") and _FC_ONLY.search(desc) \
            and not re.search(r"\bsas\b|\bsata\b|sas[23]\b|sas-[23]", desc, re.I):
        errs.append("接口冲突：FC 描述抽出 SAS/SATA")
    return errs


# ─────────────────────────── 网卡 NIC / 光纤卡 FC（修正版）───────────────────────────
# 接线时需在 standardize.py 顶部 FIELD_SCHEMA 增本类条目，并在 standardize() 按 l2_code 分派：
#   FIELD_SCHEMA["NIC_FC"] = ["brand", "port_count", "speed", "connector", "card_type"]
#   if l2_code in ("0403", "0405"):
#       return _run(out, "NIC_FC", extract_nic_fc(desc, pn, brand),
#                   render_nic_fc, classify_nic_fc_l2, validate_nic_fc, desc)

_NICFC_PORT_NUM = re.compile(r"(\d+)\s*[-]?\s*(?:port\b|端口|口)", re.I)
_NICFC_PORT_WORD = re.compile(r"\b(single|dual|quad)[\s-]*(?:ports?)?\b", re.I)
_NICFC_PORT_CN = re.compile(r"([单双四])\s*(?:电|光)?\s*(?:端)?口")
_NICFC_PORT_WORD_N = {"single": 1, "dual": 2, "quad": 4}
_NICFC_PORT_CN_N = {"单": 1, "双": 2, "四": 4}
_NICFC_CONNECTOR = re.compile(
    r"(QSFP28|QSFP\+|QSFP-DD|QSFP|SFP28|SFP56|SFP\+|RJ-?45|1000BASE-T|BASE-T)", re.I)
# FIX#1：GFC 永远紧跟数字(16GFC/8GFC/4GFC)，digit 与 G 间无 \b 边界——原 \bGFC\b 是死分支。
# 改用 \dGFC 让数字前缀的 GFC 也登记为 FC 身份(否则 16GFC SR-Optic 会被误判为以太网卡)。
_NICFC_FC_RX = re.compile(
    r"fibre\s*channel|fiber\s*channel|\bFC\b|\bHBA\b|\d\s*GFC|"
    r"\bQL[EAMS]\d|\bLP[em]\d|光纤通道|光纤hba|(?<!网)光纤卡", re.I)
# 强以太信号——与 FC 信号并存即类型冲突(如 "LAN ... 10G FC 534FLR")。
_NICFC_STRONG_ETH = re.compile(
    r"\bLAN\b|flexiblelom|flexlom|ethernet|网卡|以太|\bGbE\b|\bGE\b|千兆|万兆|base-?t|connectx", re.I)
# 交换机/收发器/光模块/存储控制器——非适配卡，validator 标记转人工。
_NICFC_SWITCH_RX = re.compile(r"\bswitch\b|交换机|transceiver|switch module|swtich", re.I)
# FIX#2：SFP 光模块即便不写 "transceiver" 也有强信号(SR-Optic/波长nm/DOM/LC/SMF)。
_NICFC_OPTIC_RX = re.compile(r"sr-?optic|shortwave|longwave|\d{3,4}\s*nm|\bDOM\b|\bSMF\b|\bLC\b", re.I)
# FIX#3：存储阵列控制器/IO 模块(For <存储>)是控制器不是主机 HBA。
_NICFC_CTRL_RX = re.compile(r"raid\s*controller|i/?o\s*module|array\s*controller", re.I)
# 卡类专属品牌别名(取自 taxonomy.BRAND_ALIASES 已确认映射；DICT 证据，非猜测)。
_CARD_BRAND_ALIAS = {"mellanox": "NVIDIA", "qlogic": "Marvell", "emulex": "Broadcom",
                     "cavium": "Marvell", "broadcom": "Broadcom", "marvell": "Marvell",
                     "silicom": "Silicom", "brocade": "Broadcom", "atto": "ATTO",
                     "solarflare": "Xilinx"}
_CARD_BRAND_RX = re.compile(r"\b(" + "|".join(_CARD_BRAND_ALIAS) + r")\b", re.I)


def _nicfc_card_brand(brand_raw, desc):
    """① recognize_brand 认出的英文规范名(EXPLICIT/DICT) ② 卡类别名表(DICT) ③ 省略。"""
    bn = T.recognize_brand(brand_raw)
    if bn:
        return _F(bn, EXPLICIT, bn)
    bn = T.recognize_brand(desc)
    if bn:
        return _F(bn, DICT, bn)
    m = _CARD_BRAND_RX.search(f"{brand_raw} {desc}")
    if m:
        b = _CARD_BRAND_ALIAS[m.group(1).lower()]
        return _F(b, DICT, f"{m.group(1)}→{b}")
    return None


def _nicfc_ports(desc):
    """端口数 → N-Port。数字+口/port → 中文 单/双/四 → 英文 single/dual/quad。上限 8 拦交换机端口。"""
    m = _NICFC_PORT_NUM.search(desc)
    if m and 1 <= int(m.group(1)) <= 8:
        return _F(f"{int(m.group(1))}-Port", EXPLICIT, m.group(0).strip())
    m = _NICFC_PORT_CN.search(desc)
    if m:
        return _F(f"{_NICFC_PORT_CN_N[m.group(1)]}-Port", EXPLICIT, m.group(0))
    m = _NICFC_PORT_WORD.search(desc)
    if m:
        return _F(f"{_NICFC_PORT_WORD_N[m.group(1).lower()]}-Port", EXPLICIT, m.group(0).strip())
    return None


def _eth_speed(desc):
    """以太速率 → NGbE，归一到 {1,10,25,40,50,100,200}。结尾用 (?![A-Za-z0-9]) 而非 \\b——
    后接 CJK(如 25GE光口/100G单口)无 ASCII 边界。万兆→10GbE、千兆→1GbE 为确定性推导。"""
    for m in re.finditer(r"(\d{1,3})\s*G(?:b?E|E|bps|bit(?:/s)?|b/s|b)?(?![A-Za-z0-9])", desc, re.I):
        v = int(m.group(1))
        if v in (1, 10, 25, 40, 50, 100, 200):
            return _F(f"{v}GbE", EXPLICIT, m.group(0).strip())
    if re.search(r"千兆|1000base-?t|1000base", desc, re.I):
        return _F("1GbE", DERIVED, "千兆")
    if re.search(r"万兆", desc):
        return _F("10GbE", DERIVED, "万兆")
    return None


def _fc_speed(desc):
    """FC 速率 → NGFC，归一到 {2,4,8,16,32}。匹配 16G/8Gb/32Gb/s/16 Gigabit/4GFC/32Gb双口/16 Gbit。
    FIX#2：末尾负向先行 (?!…memory/内存/ram/缓存/cache)，防把 '2GB memory' 内存容量误读成链路速率。"""
    for m in re.finditer(
        r"(\d{1,2})\s*(?:G(?:FC|b(?:ps|it|/s)?|F|b)?|\s*Gigabit)(?![A-Za-z0-9])"
        r"(?!\s*(?:memory|内存|ram|缓存|cache))", desc, re.I):
        v = int(m.group(1))
        if v in (2, 4, 8, 16, 32):
            return _F(f"{v}GFC", EXPLICIT, m.group(0).strip())
    return None


def _nicfc_connector(desc):
    m = _NICFC_CONNECTOR.search(desc)
    if not m:
        return None
    raw = m.group(1).upper().replace("RJ45", "RJ-45")
    if raw in ("1000BASE-T", "BASE-T"):
        raw = "RJ-45"
    return _F(raw, EXPLICIT, m.group(0))


def extract_nic_fc(desc, pn="", brand_raw=""):
    specs = {}
    b = _nicfc_card_brand(brand_raw, desc)
    if b:
        specs["brand"] = b
    # FC 与以太信号冲突 → 不给 card_type(classify 返 None 转人工，绝不强判)
    if _NICFC_FC_RX.search(desc) and _NICFC_STRONG_ETH.search(desc):
        p = _nicfc_ports(desc)
        if p:
            specs["port_count"] = p
        return specs
    is_fc = bool(_NICFC_FC_RX.search(desc))
    p = _nicfc_ports(desc)
    if p:
        specs["port_count"] = p
    if is_fc:
        spd = _fc_speed(desc)
        if spd:
            specs["speed"] = spd
        specs["card_type"] = _F("Fibre Channel HBA", DERIVED, "FC")
    else:
        spd = _eth_speed(desc)
        if spd:
            specs["speed"] = spd
        c = _nicfc_connector(desc)
        if c:
            specs["connector"] = c
        specs["card_type"] = _F("NIC", DERIVED, "Ethernet")
    return specs


def render_nic_fc(specs):
    """{品牌} {端口数} {速率} {接口} {卡名} —— 只渲染已抽字段；无任何可辨识规格→None。"""
    if not _val(specs, "card_type"):
        return None
    # 需至少一个真实规格(端口/速率/接口)，否则只剩裸 "NIC"/"FC HBA" 无意义 → 转人工
    if not (_val(specs, "port_count") or _val(specs, "speed") or _val(specs, "connector")):
        return None
    seg = [_val(specs, k) for k in ("brand", "port_count", "speed", "connector", "card_type")
           if _val(specs, k)]
    return " ".join(seg)


def classify_nic_fc_l2(specs):
    """FC→0405，以太→0403；无类型识别字段(冲突/未判)→None(转人工，绝不强分)。"""
    ct = _val(specs, "card_type")
    if ct == "Fibre Channel HBA":
        return "0405"
    if ct == "NIC":
        return "0403"
    return None


def validate_nic_fc(l2, specs, desc):
    errs = []
    ct, spd = _val(specs, "card_type"), _val(specs, "speed")
    if l2 == "0405" and ct != "Fibre Channel HBA":
        errs.append("分类 光纤卡FC 但类型非 Fibre Channel HBA")
    if l2 == "0403" and ct != "NIC":
        errs.append("分类 网卡NIC 但类型非 NIC")
    if ct == "Fibre Channel HBA" and spd and spd.endswith("GbE"):
        errs.append("FC 卡速率不应为以太 GbE")
    if ct == "NIC" and spd and spd.endswith("GFC"):
        errs.append("NIC 速率不应为 FC GFC")
    if _NICFC_SWITCH_RX.search(desc):
        errs.append("描述疑似交换机/交换模块/收发器，非适配卡")
    # FIX#2/#3：光模块(SFP收发器)与存储阵列控制器/IO 模块非适配卡 → 转人工
    if _NICFC_OPTIC_RX.search(desc):
        errs.append("描述疑似光模块/收发器(SFP)，非适配卡")
    if _NICFC_CTRL_RX.search(desc):
        errs.append("描述疑似存储阵列控制器/IO模块，非主机 HBA")
    return errs


# 原样回传：待验代码独立复跑可执行，8/8 黄金用例全过，真实桶 31/31 AUTO_OK=100% 与声称一致。
# 四原则（先识别类型再抽字段 / 只渲染结构化字段 / 无证据不猜 / 分类由字段决定）均满足；
# renderer 不输出任何无证据字段、无单位漂移面（本类无 Gb/s|inch|GHz|W）、缺 card_type 即 None 转人工不硬分。
# 发现的均为不影响当前 31 行与黄金用例的潜在边缘风险（见 issues），不阻断 approve，故不改动代码。

# ── FIELD_SCHEMA 条目（加进 standardize.py 顶部的 FIELD_SCHEMA 字典）──
#     "OTHER_CARD": ["brand", "model", "card_type"],

# ─────────────────────────── 其他适配卡 OTHER_CARD (0499) ───────────────────────────
_CARD_TYPE_RULES = [
    (re.compile(r"\bSAS\b.{0,6}(?:expander|扩展器)|\bexpander\b|扩展器", re.I), "SAS Expander", EXPLICIT),
    (re.compile(r"\bDPU\b|data processing unit|sm[\s-]?nic|smartnic", re.I), "DPU", EXPLICIT),
    (re.compile(r"\bFPGA\b", re.I), "FPGA Card", EXPLICIT),
    (re.compile(r"\bTPM\b|trusted platform", re.I), "TPM Module", EXPLICIT),
    (re.compile(r"\bM\.2\b.{0,12}(?:boot|启动)|boot\s*(?:optimized|card|device)|\bBOSS\b", re.I),
     "M.2 Boot Card", EXPLICIT),
    (re.compile(r"memory\s*riser|内存\s*riser|内存扩展板", re.I), "Memory Riser", EXPLICIT),
    (re.compile(r"mezzanine|扣卡|夹层卡", re.I), "Mezzanine Card", EXPLICIT),
    (re.compile(r"\briser\b|riser\s*(?:card|board|cage|卡|板)|riser卡", re.I), "Riser Card", EXPLICIT),
]
_GENERIC_CARD = re.compile(r"扩展卡|扩展板卡|扩展板|pci(?:e|[\s-]express)?\s*(?:扩展|board|卡)|"
                           r"expansion\s*(?:card|board)|adapter\s*card|adapter\s*board|"
                           r"\briser\b|\bmezzanine\b", re.I)

_MODEL_RULES = [
    re.compile(r"\b(IT\d{2}[A-Z]{3,4}(?:-[A-Z0-9]+)?)\b"),
    re.compile(r"\b(BC\d{2}[A-Z]{2,4})\b"),
    re.compile(r"\b(MZ\d{3}(?:-\d\*\d{1,3}[A-Z]{0,3})?)\b"),
    re.compile(r"\b(03[0-9A-Z]{6})\b"),
    re.compile(r"\b(0[A-Z0-9]{5})\b(?=.*\b(?:riser|card|dell)\b)", re.I),
    re.compile(r"\b(\d{6}-\d{3})\b"),
    re.compile(r"\b(\d{3}-\d{3}-\d{3}[A-Z]?-\d{2})\b"),
]


def _card_type(desc):
    for rx, ctype, src in _CARD_TYPE_RULES:
        m = rx.search(desc)
        if m:
            return _F(ctype, src, m.group(0).strip())
    m = _GENERIC_CARD.search(desc)
    if m:
        return _F("Expansion Card", EXPLICIT, m.group(0).strip())
    return None


def _card_model(desc):
    for rx in _MODEL_RULES:
        m = rx.search(desc)
        if m:
            return _F(m.group(1).upper(), EXPLICIT, m.group(0).strip())
    return None


def extract_other_card(desc, brand_raw=""):
    specs = {}
    bn = T.recognize_brand(brand_raw) or T.recognize_brand(desc)
    if bn:
        specs["brand"] = _F(bn, EXPLICIT, bn)
    mdl = _card_model(desc)
    if mdl:
        specs["model"] = mdl
    ct = _card_type(desc)
    if ct:
        specs["card_type"] = ct
    return specs


def render_other_card(specs):
    if not _val(specs, "card_type"):
        return None
    seg = []
    for k in ("brand", "model", "card_type"):
        if _val(specs, k):
            seg.append(_val(specs, k))
    return " ".join(seg)


def classify_other_card_l2(specs):
    return "0499" if _val(specs, "card_type") else None


def validate_other_card(l2, specs, desc):
    errs = []
    if not _val(specs, "card_type"):
        errs.append("其他适配卡功能类型缺失，无法渲染")
    return errs

# ── 编排接入（standardize() 末尾、其余类目委托模板之前，仿 GPU 分支加）──
#     if l2_code == "0499":
#         specs = extract_other_card(desc, brand)
#         out["object_type"] = "OTHER_CARD"
#         out["structured_specs"] = {k: specs[k] for k in FIELD_SCHEMA["OTHER_CARD"] if k in specs}
#         out["canonical_description"] = render_other_card(specs)
#         l2 = classify_other_card_l2(specs)
#         out["category_l2"] = T.CATEGORY_NAMES.get(l2) if l2 else None
#         out["_l2_code"] = l2
#         out["validation_errors"] = validate_other_card(l2, specs, desc)
#         out["review_status"] = (AUTO_OK if (out["canonical_description"]
#                                 and not out["validation_errors"] and l2) else REVIEW)
#         return out


# ─────────────────────────── 电源 PSU ───────────────────────────
# 真实数据：功率(450W/1200W…)是唯一普遍锚点；AC/DC、热插拔(Hot-Plug/Swap/HS)、
# 80Plus 效率(Platinum/Titanium…)零散出现；型号几乎缺失——"For S7006E"/"VNX DAE3U"
# 是机型兼容串而非 PSU 自身型号，不得当 model 渲染。无功率证据 → 转人工。
# FIELD_SCHEMA 追加："PSU": ["wattage", "input_type", "efficiency", "redundancy"]

_PSU_WATT = re.compile(r"(?<![\d.])(\d{2,4})\s*W\b", re.I)
# 80Plus 评级必须有显式『80 Plus / 80Plus / 80+ Plus』锚点。
# 裸等级词(Gold/Platinum/Silver/Bronze/Titanium)在本域是磁盘产品线(Seagate Gold/WD Gold)、
# 机箱材质(Titanium chassis)、整机系列名(Platinum Server)的高频词，不能反推 80Plus——
# 否则违反原则③『无可靠证据不得猜』。故去掉裸等级兜底，只认带锚点的评级。
_PSU_80PLUS = re.compile(r"80\s*\+?\s*PLUS\s+(Titanium|Platinum|Gold|Silver|Bronze)\b", re.I)


def _psu_input(desc):
    if re.search(r"\bAC\b|交流", desc, re.I):
        return _F("AC", EXPLICIT, "AC")
    if re.search(r"\bDC\b|直流|-48\s*V", desc, re.I):
        return _F("DC", EXPLICIT, "DC")
    return None                            # 缺输入类型 → 不猜（230V/115V 不强推 AC）


def _psu_redundancy(desc):
    # Hot-Sawp 是真实数据里 Hot-Swap 的拼写错误，一并兜住
    if re.search(r"hot[\s-]?(?:plug|swap|sawp)|热插拔|\bHS\b|redundant|冗余", desc, re.I):
        return _F("Hot-Plug", EXPLICIT, "Hot-Plug")
    return None


def _psu_efficiency(desc):
    # 只认显式 80Plus 评级("80 Plus X"/"80Plus X"/"80+ Plus X")。
    # 注意 "89% Efficiency" 是原始百分比、不是 80Plus 评级；裸等级词(Gold/Platinum…)
    # 是产品线/材质/整机系列噪声，均绝不当评级渲染。
    m = _PSU_80PLUS.search(desc)
    if m:
        return _F(f"80Plus {m.group(1).title()}", EXPLICIT, m.group(0))
    return None


def extract_psu(desc, pn="", brand_raw=""):
    specs = {}
    m = _PSU_WATT.search(desc)
    if m and 50 <= int(m.group(1)) <= 3600:
        specs["wattage"] = _F(f"{int(m.group(1))}W", EXPLICIT, m.group(0).strip())
    inp = _psu_input(desc)
    if inp:
        specs["input_type"] = inp
    eff = _psu_efficiency(desc)
    if eff:
        specs["efficiency"] = eff
    red = _psu_redundancy(desc)
    if red:
        specs["redundancy"] = red
    return specs


def render_psu(specs):
    """{功率} {输入} {效率} {冗余} Power Supply —— 功率是唯一关键字段，缺则转人工。"""
    if not _val(specs, "wattage"):
        return None
    seg = [_val(specs, k) for k in ("wattage", "input_type", "efficiency", "redundancy")
           if _val(specs, k)]
    seg.append("Power Supply")
    return " ".join(seg)


def classify_psu_l2(specs):
    """电源无二级子码（taxonomy 仅 06）：有功率证据 → 06，否则 None（转人工）。"""
    return "06" if _val(specs, "wattage") else None


def validate_psu(l2, specs, desc):
    errs = []
    inp = _val(specs, "input_type")
    if inp == "AC" and re.search(r"直流|\bDC\b", desc, re.I) and not re.search(r"AC[\s/-]*DC", desc, re.I):
        errs.append("输入类型 AC 与描述中的 DC 冲突")
    if inp == "DC" and re.search(r"交流", desc):
        errs.append("输入类型 DC 与描述中的交流冲突")
    return errs


# ─────────────────────────── 电池/超级电容 (07) ───────────────────────────
# FIELD_SCHEMA 条目（加入 standardize.py 顶部的 FIELD_SCHEMA dict）：
#     "BATTERY": ["battery_type", "voltage"],
# 真实数据观察：本桶 ~90% 是 OEM 备件名沙拉(品牌+机型/控制器型号+"Battery/BBU/电池")，
# 几乎没有可标准化的结构化规格。唯一稳定的可抽字段是「类型」(BBU/Supercap/CMOS/FBWC/
# CacheVault/NVRAM/Controller)与偶现的电压(V)。型号身份散落在机型串里、不可靠 → 不抽不渲。
# 故策略：只渲染「{电压} {类型词}」这种有确证的最小骨架；类型识别不出来 → 转人工。
# 决不把机型串(DS8870/P410/EVA4000…)塞进 canonical(那是自由拼凑,违背原则二)。
# taxonomy 07 为单级叶子码(无 L2 子码)，故 classify 只在识别出类型时归 "07"，否则 None。
_BAT_CACHEVAULT = re.compile(r"cachevault|cache\s*vault", re.I)
_BAT_SUPERCAP = re.compile(r"super\s*cap(?:acitor)?|supercap|超级电容", re.I)
# 「掉电保护模块」直指超级电容单元本身(华为/H3C 命名)，是强正信号，压过卡本体抑制。
_BAT_FLUSHCAP = re.compile(r"掉电保护(?:模块)?", re.I)
_BAT_FBWC = re.compile(r"\bfbwc\b|flash\s*backed", re.I)
_BAT_BBWC = re.compile(r"\bbbwc\b", re.I)
_BAT_CMOS = re.compile(r"\bcmos\b|\brtc\b|纽扣|coin|lithium\s*coin", re.I)
_BAT_NVRAM = re.compile(r"\bnvram\b", re.I)
_BAT_SMARTSTORAGE = re.compile(r"smart\s*storage\s*battery", re.I)
_BAT_BBU = re.compile(r"\bbbu\b|battery\s*backup", re.I)
# RAID 缓存电池：必须出现“电池/battery”作为被治理项(不是只“支持电容”的卡)。
_BAT_RAIDCACHE = re.compile(
    r"(?:raid|阵列).*?(?:cache\s*battery|缓存电池|卡电池|电池)|缓存电池|cache\s*battery", re.I)
_BAT_CONTROLLER = re.compile(r"controller\s*battery|控制器电池|节点电池", re.I)
_BAT_GENERIC = re.compile(r"\bbattery\b|电池", re.I)
# 否定语境：明写「不含/without/no … battery/超级电容」→ 是控制器本体而非电池本身。
_BAT_NEG_SUPERCAP = re.compile(r"不含\s*超级电容|不带\s*超级电容|without\s*super\s*cap|no\s*super\s*cap", re.I)
_BAT_NEG_BATTERY = re.compile(r"不含\s*电池|不带\s*电池|without\s*battery|no\s*battery|w/?o\s*battery", re.I)
# 卡/模块本体特征：是阵列卡/功能模块/制成板本身(只是“支持”电容)，而非可单独治理的电池/电容备件。
_BAT_CARD_BODY = re.compile(
    r"制成板|功能模块|raid\s*卡|raid\s*card|raid\s*module|raid\s*controller|"
    r"array\s*controller|阵列卡|smart\s*array\s*p\d|megaraid|board\s*id", re.I)
# 卡本体里“支持/含”电容才出现 supercap 字样 → 不能当作电容备件。
_BAT_SUPPORT = re.compile(r"支持\s*超级电容|含\s*超级电容|with\s*super\s*cap", re.I)
# 电压：3.6V / 4.8V / 13.5V / 3.7V（排除 12G/6G 链路、96W/1000W 功率、容量 GB）
_BAT_VOLT = re.compile(r"(?<![A-Za-z0-9.])(\d{1,2}(?:\.\d)?)\s*V\b(?!\w)", re.I)


def _battery_type(desc):
    """识别电池/电容类型。返回 (canonical 类型词字段, 子类语义键) 或 (None, None)。
    先处理否定/卡本体语境(防把“不含电池的RAID卡”渲成电池)。"""
    neg_cap = bool(_BAT_NEG_SUPERCAP.search(desc))
    neg_bat = bool(_BAT_NEG_BATTERY.search(desc))
    card_body = bool(_BAT_CARD_BODY.search(desc))
    support_only = bool(_BAT_SUPPORT.search(desc))     # 卡“支持/含”电容措辞
    # 「掉电保护模块」直指电容单元 → 强正信号，无视 card_body/support_only。
    if _BAT_FLUSHCAP.search(desc) and not neg_cap:
        return _F("Supercapacitor", EXPLICIT, "掉电保护"), "supercap"
    # 强类型词(CacheVault/Supercap/FBWC/BBWC/CMOS/NVRAM/BBU/Smart Storage)即便在卡串里也是确证的
    # 该备件类型；只有“支持/含…电容”这种被动描述 + 卡本体 → 是卡而非电容，转人工。
    if _BAT_CACHEVAULT.search(desc) and not neg_cap and not (card_body and support_only):
        return _F("CacheVault Supercapacitor", EXPLICIT, "CacheVault"), "supercap"
    if _BAT_SUPERCAP.search(desc) and not neg_cap and not (card_body and support_only):
        return _F("Supercapacitor", EXPLICIT, "Supercap"), "supercap"
    if _BAT_FBWC.search(desc):
        return _F("FBWC Module", EXPLICIT, "FBWC"), "fbwc"
    if _BAT_BBWC.search(desc):
        return _F("BBWC Battery", EXPLICIT, "BBWC"), "bbwc"
    if _BAT_CMOS.search(desc):
        return _F("CMOS Battery", EXPLICIT, "CMOS"), "cmos"
    if _BAT_NVRAM.search(desc):
        return _F("NVRAM Battery", EXPLICIT, "NVRAM"), "nvram"
    if _BAT_SMARTSTORAGE.search(desc):
        return _F("Smart Storage Battery", EXPLICIT, "Smart Storage Battery"), "bbu"
    if _BAT_BBU.search(desc) and not neg_bat:
        return _F("BBU", EXPLICIT, "BBU"), "bbu"
    # RAID 缓存电池：要“电池/battery”作宾语(非否定)；若是卡本体只“支持电容”无电池项 → 转人工。
    if _BAT_RAIDCACHE.search(desc) and not neg_bat and not (card_body and support_only):
        return _F("RAID Cache Battery", EXPLICIT, "RAID Cache Battery"), "raid_cache"
    if _BAT_CONTROLLER.search(desc) and not neg_bat:
        return _F("Controller Battery", EXPLICIT, "Controller Battery"), "controller"
    # 泛 battery/电池：否定语境(without/不含 battery) → 是控制器本体，转人工；其余命名即算。
    if _BAT_GENERIC.search(desc) and not neg_bat:
        return _F("Battery", EXPLICIT, "Battery"), "battery"
    return None, None


def _battery_voltage(desc):
    for m in _BAT_VOLT.finditer(desc):
        v = float(m.group(1))
        if 1 <= v <= 60:                       # 备电电压区间；排除 96W/1000W/12G 之类
            n = m.group(1).rstrip("0").rstrip(".") if "." in m.group(1) else m.group(1)
            return _F(f"{n}V", EXPLICIT, m.group(0))
    return None


def extract_battery(desc, brand_raw=""):
    """只抽确证字段：battery_type(类型词) + voltage(电压)。型号/机型不可靠 → 不抽。"""
    specs = {}
    typ, sub = _battery_type(desc)
    if typ:
        specs["battery_type"] = typ
        specs["_sub"] = sub
    volt = _battery_voltage(desc)
    if volt:
        specs["voltage"] = volt
    return specs


def render_battery(specs):
    """{电压} {类型词} —— 只渲染已抽到的确证字段；缺类型 → None(转人工)。
    不把机型/控制器串塞进来(那是自由拼凑)。电容/FBWC 类不渲染电压(以法拉标且语义不同)。"""
    typ = _val(specs, "battery_type")
    if not typ:
        return None                            # 无可辨识类型 → 转人工
    seg = []
    if _val(specs, "voltage") and specs.get("_sub") not in ("supercap", "fbwc"):
        seg.append(_val(specs, "voltage"))
    seg.append(typ)
    return " ".join(seg)


def classify_battery_l2(specs):
    """taxonomy 07 为单级叶子码(无 L2 子码)：识别出类型即归 '07'；否则 None 转人工。"""
    return "07" if _val(specs, "battery_type") else None


def validate_battery(l2, specs, desc):
    errs = []
    sub = specs.get("_sub")
    # 电容与 BBU 同时出现 → 类型歧义，人工确认
    if sub == "supercap" and re.search(r"\bbbu\b|battery\s*backup", desc, re.I):
        errs.append("同时出现超级电容与 BBU，类型歧义需人工确认")
    # 识别为通用 battery 却带强 RAID 卡身份特征 → 疑似把卡本体当电池
    if sub == "battery" and re.search(r"\b(megaraid|smart\s*array\s*p\d)\b", desc, re.I):
        errs.append("疑似 RAID 卡本体而非独立电池备件")
    return errs

# 编排接入(加进 standardize() 的分支链，l1_code == "07" 时)：
#     if l1_code == "07":
#         return _run(out, "BATTERY", extract_battery(desc, brand),
#                     render_battery, classify_battery_l2, validate_battery, desc)
# 注：_run 按 FIELD_SCHEMA["BATTERY"] 过滤，会自动剔除内部键 _sub（不在 schema 中）。


# ─────────────────────────── FIELD_SCHEMA 补丁（集成必需）───────────────────────────
# 待验代码遗漏了 FIELD_SCHEMA["COOLING"] 条目。standardize._run 用
#   allowed = FIELD_SCHEMA.get(obj_type, [])
#   out["structured_specs"] = {k: specs[k] for k in allowed if k in specs}
# 过滤落库字段；若无 COOLING 条目则 allowed=[] → structured_specs 被静默清空
# （canonical 仍正确，因 render 收的是未过滤 specs，但证据字段在持久化层丢失）。
# 故必须在 standardize.py 的 FIELD_SCHEMA 字典中加入下面这一行（form_factor 占位，
# 当前不抽，留待证据更全时启用，与作者备注意图一致）：
#
#     "COOLING": ["brand", "cooling_type", "hot_plug", "form_factor"],
#
# 并在 standardize() 编排里补 l1_code == "08" 分支：
#     if l1_code == "08":
#         return _run(out, "COOLING", extract_cooling(desc, brand),
#                     render_cooling, classify_cooling_l2, validate_cooling, desc)


# ─────────────────────────── 风扇/散热 Cooling（四函数：原样，已验证正确）───────────────────────────
_COOL_TYPES = [
    (re.compile(r"air\s*baffle|导风罩|风道", re.I), "Air Baffle"),
    (re.compile(r"liquid\s*cool|water\s*cool|冷板|液冷|水冷", re.I), "Liquid Cooling"),
    (re.compile(r"heat[\s-]*sink|散热器|散热片", re.I), "Heatsink"),
    (re.compile(r"blower", re.I), "Blower"),
    (re.compile(r"fan\s*back\s*plane|风扇背板", re.I), "Fan Backplane"),
    (re.compile(r"fan\s*(?:board|frame)|风扇框", re.I), "Fan Board"),
    (re.compile(r"fan\s*tray", re.I), "Fan Tray"),
    (re.compile(r"fan\s*cage", re.I), "Fan Cage"),
    (re.compile(r"fan\s*(?:module|assembly|assm|assy)|风扇模块|风机盒|风扇盒", re.I), "Fan Module"),
    # 裸风扇：容忍 Fan2/Fans/紧贴 CJK 的 FAN；左侧 (?<![a-z]) 防误命中 infant/surface 等英文词
    (re.compile(r"(?<![a-z])fans?\b|fan\d|风扇|风机", re.I), "Fan"),
]
_COOL_WHITELIST = ("Fan", "Blower", "Heatsink", "Fan Module", "Fan Tray", "Fan Cage",
                   "Fan Board", "Fan Backplane", "Air Baffle", "Liquid Cooling")
_HOTPLUG = re.compile(r"hot[\s-]*plug|hot[\s-]*swap|热插拔", re.I)


def _cooling_type(desc):
    for rx, t in _COOL_TYPES:
        m = rx.search(desc)
        if m:
            return _F(t, EXPLICIT, m.group(0).strip())
    return None                            # 无形态词 → 不猜


def extract_cooling(desc, brand_raw=""):
    specs = {}
    bnorm, _bzh = T.resolve_brand(brand_raw, desc)
    # 只接受品牌字典认出的规范英文品牌；resolve_brand 兜底回填的原始中文不进结构字段
    if bnorm and (T.recognize_brand(brand_raw) or T.recognize_brand(desc)):
        specs["brand"] = _F(bnorm, EXPLICIT, bnorm)
    ct = _cooling_type(desc)
    if ct:
        specs["cooling_type"] = ct
    m = _HOTPLUG.search(desc)
    if m:
        specs["hot_plug"] = _F("Hot-Plug", EXPLICIT, m.group(0).strip())
    return specs


def render_cooling(specs):
    """{品牌} {Hot-Plug} {类型} —— 仅渲染证据化字段；机型兼容串绝不自由拼接。缺类型 → None。"""
    ct = _val(specs, "cooling_type")
    if not ct:
        return None                        # 无可辨识身份 → 转人工
    seg = []
    if _val(specs, "brand"):
        seg.append(_val(specs, "brand"))
    if _val(specs, "hot_plug"):
        seg.append(_val(specs, "hot_plug"))
    seg.append(ct)
    return " ".join(seg)


def classify_cooling_l2(specs):
    """taxonomy 08 为扁平大类（无 08xx 子码）：识别到 cooling_type 即归 08，否则 None 转人工。"""
    return "08" if _val(specs, "cooling_type") else None


def validate_cooling(l2, specs, desc):
    errs = []
    ct = _val(specs, "cooling_type")
    if ct and ct not in _COOL_WHITELIST:
        errs.append(f"未知散热类型 {ct}")
    if _val(specs, "hot_plug") and not _HOTPLUG.search(desc):
        errs.append("hot_plug 无原文证据")
    return errs


# ─────────────────────────── 光模块 Optics（taxonomy 0901，无子码） ───────────────────────────
# 真实数据：本桶噪声重——大量网卡/适配器/接口板/铜缆混入（含 SFP/QSFP 字样但实体不是光模块）。
# 只在出现"光模块/transceiver/optical/光接口/PMD距离码/波长"等收发器信号、且无"card/adapter/
# 网卡/接口板/铜缆/端口数"等异类信号时才当光模块；否则不在此渲染。绝不补未写的端口数/尺寸/速率。
# 速率单位统一 NNG（10G/25G/40G/100G，仅取已知整数光口速率，FC 线率 4.25/8.5/14.025 不四舍五入猜）；
# 波长 NNNNnm；距离 km/m 原值；接口 LC/MPO；光纤 SMF/MMF。
# FIELD_SCHEMA 追加：
#     "OPTICS": ["brand", "speed", "form_factor", "pmd", "wavelength",
#                "distance", "connector", "fiber"],

# 形态封装：按"具体度"取最具体的一个（QSFP28 > QSFP+ > QSFP；SFP28 > SFP+ > SFP）。
# 描述里常先出现裸 SFP（如 "XG-SFP-LR"）后出现 SFP+，必须挑最具体的而非首现的。
_FORM_VARIANTS = [
    ("QSFP-DD", re.compile(r"QSFP-?DD", re.I)), ("QSFP56", re.compile(r"QSFP56", re.I)),
    ("QSFP28", re.compile(r"QSFP28", re.I)), ("QSFP+", re.compile(r"QSFP\+", re.I)),
    ("QSFP", re.compile(r"\bQSFP\b", re.I)), ("SFP-DD", re.compile(r"SFP-?DD", re.I)),
    ("SFP56", re.compile(r"SFP56", re.I)), ("SFP28", re.compile(r"SFP28", re.I)),
    ("SFP+", re.compile(r"SFP\+", re.I)), ("SFP", re.compile(r"SFP", re.I)),
    ("OSFP", re.compile(r"\bOSFP\b", re.I)), ("XFP", re.compile(r"\bXFP\b", re.I)),
    ("CFP", re.compile(r"\bCFP\d?\b", re.I)), ("GBIC", re.compile(r"\bGBIC\b", re.I)),
]
_FORM_RANK = {"QSFP-DD": 9, "QSFP56": 8, "QSFP28": 8, "QSFP+": 7, "QSFP": 6, "OSFP": 6,
              "SFP-DD": 5, "SFP56": 4, "SFP28": 4, "SFP+": 3, "SFP": 2, "XFP": 5,
              "CFP": 5, "GBIC": 2}
# PMD/类型距离码（替代波长的标准码）：优先 NNGBASE-XXX 里的 XXX，其次裸标准码
_OPTIC_PMD = re.compile(
    r"\b(?:100G|40G|25G|10G)?BASE-?(SR4|LR4|ER4|FR4|DR4|SR|LR|ER|ZR|CR4|CR|USR|iSR4|LW|SW)\b", re.I)
# 裸标准码：SR 后排除 -IOV（SR-IOV 是网卡虚拟化特性，非光 PMD）
_OPTIC_PMD_BARE = re.compile(r"\b(SR4|LR4|ER4|FR4|DR4|PSM4|CWDM4|SR(?!-?IOV)|LR|ER|ZR|USR|iSR4|LW|SW)\b", re.I)
_WAVELEN = re.compile(r"(\d{3,4})\s*-?\s*nm\b", re.I)                      # 1310nm / 1310-nm
_DIST = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*-?\s*(KM|M)\b", re.I)  # 10km / 10-km
_CONNECTOR = re.compile(r"\b(LC|MPO|MTP|SC)\b")
_FIBER = re.compile(r"\b(SMF|MMF|MMO|MM|SM)\b|单模|多模", re.I)
# 速率：显式 NN G[b][/s] / NNGbE / NNGBASE / NNGFC；取首个 ∈ 已知光口速率集
_OPTIC_SPEED = re.compile(
    r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*(?:Gb?(?:/?s|ps|E|FC)?|GBASE|GBe|GBE|G)\b", re.I)
_OPTIC_KNOWN_SPEEDS = {1, 2, 4, 8, 10, 16, 25, 32, 40, 50, 100, 200, 400}
# 收发器信号（出现其一才像光模块）。base-XX 不要求前导词边界（"40GBase-SR4" 里 G 紧贴 Base）；
# 裸 PMD 距离码(SR4/LR4/USR/…)也算信号——_NOT_OPTIC 已先滤掉网卡/铜缆。裸 SR 排除 SR-IOV。
_OPTIC_SIGNAL = re.compile(
    r"transceiver|光模块|光纤模块|optical\s*(?:module|transceiver|optics)|\boptics\b|光接口|"
    r"base-?(?:sr|lr|er|zr|fr|dr)|\b(?:SR4|LR4|ER4|FR4|DR4|SR(?!-?IOV)|LR|ZR|USR|iSR4)\b|"
    r"多模光|单模光|\d+\s*-?\s*nm\b", re.I)
# 异类信号（出现即不当光模块——网卡/适配器/接口板/铜缆/RJ45 网口实体/带端口数的整卡/SR-IOV 网卡特性）
_NOT_OPTIC = re.compile(
    r"\b(card|adapter|nic|网卡|适配器|接口板|主板|board|HBA|PCI-?E?\s*card)\b|"
    r"\bRJ-?45\b|\bDAC\b|\bCU\b|铜缆|copper|\bAOC\b|\bSR-?IOV\b|有源|无源|\d+\s*-?\s*port|端口", re.I)


def _is_optics(desc):
    """收发器信号存在 且 无异类(网卡/铜缆/接口板)信号 → 当光模块；否则交回上层(转人工)。"""
    return bool(_OPTIC_SIGNAL.search(desc)) and not _NOT_OPTIC.search(desc)


def extract_optics(desc, pn="", brand_raw=""):
    specs = {}
    if not _is_optics(desc):           # 网卡/铜缆/接口板等(_NOT_OPTIC)混入 → 不当光模块，转人工
        return specs
    # 品牌：仅在能识别为规范品牌时入 specs（避免 "其他"/原始中文串污染结构字段）；
    # 顶层 standardize() 另会用 resolve_brand 单独存 brand_norm/brand_zh。
    bn = T.recognize_brand(brand_raw) or T.recognize_brand(desc)
    if bn:
        specs["brand"] = _F(bn, EXPLICIT if T.recognize_brand(brand_raw) else DICT, bn)
    for m in _OPTIC_SPEED.finditer(desc):              # 速率：取首个落在已知光口速率集的整数
        v = float(m.group(1))
        if v == int(v) and int(v) in _OPTIC_KNOWN_SPEEDS:
            specs["speed"] = _F(f"{int(v)}G", EXPLICIT, m.group(0).strip())
            break
    best = None                                        # 形态：挑最具体的（SFP28>SFP+>SFP）
    for name, rx in _FORM_VARIANTS:
        mm = rx.search(desc)
        if mm and (best is None or _FORM_RANK[name] > _FORM_RANK[best[0]]):
            best = (name, mm.group(0))
    if best:
        specs["form_factor"] = _F(best[0], EXPLICIT, best[1])
    m = _OPTIC_PMD.search(desc) or _OPTIC_PMD_BARE.search(desc)
    if m:
        specs["pmd"] = _F(m.group(m.lastindex).upper().replace("ISR4", "iSR4"), EXPLICIT, m.group(0))
    m = _WAVELEN.search(desc)
    if m:
        specs["wavelength"] = _F(f"{m.group(1)}nm", EXPLICIT, m.group(0))
    m = _DIST.search(desc)
    if m:
        unit = m.group(2).lower()
        n = m.group(1).rstrip("0").rstrip(".") if "." in m.group(1) else m.group(1)
        specs["distance"] = _F(f"{n}{unit}", EXPLICIT, m.group(0))
    m = _CONNECTOR.search(desc)
    if m:
        specs["connector"] = _F(m.group(1).upper().replace("MTP", "MPO"), EXPLICIT, m.group(0))
    m = _FIBER.search(desc)                            # 光纤模式：单模/SM/SMF→SMF；多模/MM/MMO/MMF→MMF
    if m:
        v = (m.group(1) or "").upper()
        specs["fiber"] = _F("SMF" if v in ("SMF", "SM") or "单模" in m.group(0) else "MMF",
                            EXPLICIT, m.group(0))
    return specs


def render_optics(specs):
    """{速率} {形态} {PMD} {波长} {距离} {接口} {光纤} Optical Transceiver —— 只渲染已抽字段。
    关键字段：形态 + (速率或 PMD) 之一存在才渲染，否则 None 转人工（不猜形态/不补端口）。"""
    form = _val(specs, "form_factor")
    if not (form and (_val(specs, "speed") or _val(specs, "pmd"))):
        return None
    seg = []
    if _val(specs, "speed"):
        seg.append(_val(specs, "speed"))
    seg.append(form)
    for k in ("pmd", "wavelength", "distance", "connector", "fiber"):
        if _val(specs, k):
            seg.append(_val(specs, k))
    seg.append("Optical Transceiver")
    return " ".join(seg)


def classify_optics_l2(specs):
    """0901 无子码：形态 + (速率或 PMD) 齐 → 0901；缺识别字段 → None（转人工，绝不强分）。"""
    if _val(specs, "form_factor") and (_val(specs, "speed") or _val(specs, "pmd")):
        return "0901"
    return None


def validate_optics(l2, specs, desc):
    errs = []
    pmd, fiber = _val(specs, "pmd"), _val(specs, "fiber")
    if pmd and fiber:                                  # SR/SR4/USR=多模；LR/LR4/ER/ZR/FR/DR=单模
        p = pmd.upper()
        if p in ("SR", "SR4", "USR", "ISR4", "SW") and fiber == "SMF":
            errs.append(f"{pmd} 为多模(MMF) 但抽到 SMF，疑似冲突")
        if p in ("LR", "LR4", "ER", "ER4", "ZR", "FR4", "DR4", "LW") and fiber == "MMF":
            errs.append(f"{pmd} 为单模(SMF) 但抽到 MMF，疑似冲突")
    if re.search(r"\bDAC\b|\bAOC\b|铜缆|copper", desc, re.I):
        errs.append("出现 DAC/AOC/铜缆，疑似线缆非光模块")
    return errs


# ── 需在 standardize.py 的 FIELD_SCHEMA 中补入以下条目（题目要求的『四函数+FIELD_SCHEMA』缺失项）──
# 否则经 _run(out, "CABLE", ...) 接线时 allowed=[] 会把 structured_specs 全部剔空。
# 同时 standardize() 需新增分支：
#   if l2_code == "0902":
#       return _run(out, "CABLE", extract_cable(desc, brand), render_cable, classify_cable_l2, validate_cable, desc)
#
# FIELD_SCHEMA["CABLE"] = ["interface_speed", "connector", "length", "cable_type", "media_type"]
#
# 以下四函数主体逻辑经对抗验证正确（8/8 黄金用例真过；75 行真实桶 AUTO_OK=18/75=24.0% 与声称一致；
# 无伪造字段、无单位漂移、无缺证据硬分类、长度取值域与单位门稳健），原样保留：

# ─────────────────────────── 线缆 Cable ───────────────────────────
_C_LEN = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)\s*(?:m\b|米|meter|-?meter)", re.I)
_C_LEN_CM = re.compile(r"(?<![A-Za-z0-9.])(\d{2,4})\s*(?:mm|cm)\b", re.I)
_C_SFF = re.compile(r"\bSFF-?(\d{4})\b", re.I)
_C_MINISAS_HD = re.compile(r"mini[\s-]?sas\s*hd|minisas\s*hd", re.I)
_C_MINISAS = re.compile(r"mini[\s-]?sas\b", re.I)
_C_SLIMSAS = re.compile(r"slim[\s-]?sas", re.I)
_C_OPTIC_CONN = re.compile(r"(QSFP-DD|QSFP28|QSFP\+|QSFP|SFP28|SFP\+|LC[-/]LC|LC[-/]SC)", re.I)
_C_SPEED = re.compile(r"(\d{2,3})\s*Gb?(?:ps|/?\s*s)?\b", re.I)   # 25/40/56/100G 网络高速线速率
_SFF_TYPE = {"8643": "Mini-SAS HD", "8644": "Mini-SAS HD", "8087": "Mini-SAS", "8088": "Mini-SAS"}
_NON_CABLE = re.compile(
    r"网卡|转接卡|轉接卡|擴充卡|扩展卡|riser|multifunction\s+card|\badapter\b|"
    r"console server|raid card|raid 卡|host bus adapter|\bnic\b|ethernet (?:server )?adapter", re.I)
# Supermicro AOC- 是产品前缀(Add-On Card)，非 Active Optical Cable
# 注：本正则偏宽（'AOC '+任意词亦命中），属偏保守误差——只会让真 AOC 转人工，绝不会把卡误渲成线缆。
_AOC_PRODUCT = re.compile(r"\bAOC[-\s]?(?:[A-Z0-9]{2,})", re.I)


def _cable_type(desc):
    """识别线缆类型 → 标准类型词。无法识别明确类型 → None（不把任意 cable 当线缆硬渲）。"""
    low = desc.lower()
    if re.search(r"power cord|电源线|power cable|c1[34]\s*-\s*c1[34]|pdu to", low):
        return _F("Power Cord", EXPLICIT, "Power Cord")
    if _NON_CABLE.search(desc) and not _C_SFF.search(desc):
        return None
    if re.search(r"active optical cable|有源光缆", low) or (
            re.search(r"\baoc\b", low) and not _AOC_PRODUCT.search(desc)):
        return _F("AOC", EXPLICIT, "AOC")
    if re.search(r"direct attach copper|passive copper|twinax|无源铜缆|\bdac\b", low):
        return _F("DAC", EXPLICIT, "DAC")
    if _C_SLIMSAS.search(desc):
        return _F("SlimSAS", EXPLICIT, "SlimSAS")
    if _C_MINISAS_HD.search(desc):
        return _F("Mini-SAS HD", EXPLICIT, "Mini-SAS HD")
    if _C_MINISAS.search(desc):
        return _F("Mini-SAS", EXPLICIT, "Mini-SAS")
    sff = [m.group(1) for m in _C_SFF.finditer(desc)]
    hd = next((s for s in sff if _SFF_TYPE.get(s) == "Mini-SAS HD"), None)
    plain = next((s for s in sff if _SFF_TYPE.get(s) == "Mini-SAS"), None)
    pick = hd or plain
    if pick:
        return _F(_SFF_TYPE[pick], DERIVED, f"SFF-{pick}→{_SFF_TYPE[pick]}")
    if re.search(r"光纤跳线|fibre cable|fiber cable|fibre channel cable|optical cable|光纤线缆|光纤", low) \
            and _C_OPTIC_CONN.search(desc):
        return _F("Fiber Jumper", EXPLICIT, "Fiber Jumper")
    return None


def _cable_connector(desc):
    """连接器：SFF-#### 成对优先(保留 'A to B' 结构)；否则光纤/铜缆插头。无 → None。"""
    sff = []
    for m in _C_SFF.finditer(desc):
        s = f"SFF-{m.group(1)}"
        if s not in sff:
            sff.append(s)
    if sff:
        joined = " to ".join(sff[:2]) if len(sff) >= 2 else sff[0]
        return _F(joined, EXPLICIT, joined)
    m = _C_OPTIC_CONN.search(desc)
    if m:
        return _F(m.group(1).upper().replace("/", "-"), EXPLICIT, m.group(0))
    return None


def _cable_length(desc):
    """长度 → Nm。米优先；否则 mm/cm 确定性折算（630mm→0.63m）。"""
    m = _C_LEN.search(desc)
    if m:
        v = float(m.group(1))
        if 0 < v <= 100:
            n = m.group(1).rstrip("0").rstrip(".") if "." in m.group(1) else m.group(1)
            return _F(f"{n}m", EXPLICIT, m.group(0))
    m = _C_LEN_CM.search(desc)
    if m:
        raw = m.group(0)
        val_mm = int(m.group(1)) * (10 if raw.lower().rstrip().endswith("cm") else 1)
        if 50 <= val_mm <= 100000:
            return _F(f"{val_mm / 1000:g}m", DERIVED, raw)
    return None


def _cable_speed(desc):
    """高速线速率 → NGb/s，仅 25/40/56/100/200/400（DAC/AOC 网络速率；SAS 链路速率不抽这里）。"""
    for m in _C_SPEED.finditer(desc):
        v = int(m.group(1))
        if v in (25, 40, 56, 100, 200, 400):
            return _F(f"{v}Gb/s", EXPLICIT, m.group(0))
    return None


def extract_cable(desc, brand_raw=""):
    specs = {"media_type": _F("Cable", EXPLICIT, "Cable")}
    ct = _cable_type(desc)
    if ct:
        specs["cable_type"] = ct
    cn = _cable_connector(desc)
    if cn:
        specs["connector"] = cn
    ln = _cable_length(desc)
    if ln:
        specs["length"] = ln
    if ct and ct["value"] in ("DAC", "AOC", "Fiber Jumper"):
        sp = _cable_speed(desc)
        if sp:
            specs["interface_speed"] = sp
    return specs


def render_cable(specs):
    """{速率} {连接器} {长度} {类型} Cable —— 缺类型(无法辨识)→None，只渲已抽字段。"""
    ct = _val(specs, "cable_type")
    if not ct:
        return None
    seg = []
    for k in ("interface_speed", "connector", "length"):
        if _val(specs, k):
            seg.append(_val(specs, k))
    if ct == "Power Cord":
        seg.append("Power Cord")
    elif ct == "Fiber Jumper":
        seg.append("Fiber Jumper Cable")
    else:
        seg += [ct, "Cable"]
    return " ".join(seg)


def classify_cable_l2(specs):
    """线缆无子码：识别出线缆类型即归 0902；否则 None（转人工，绝不强分）。"""
    if _val(specs, "cable_type"):
        return "0902"
    return None


def validate_cable(l2, specs, desc):
    errs = []
    ct = _val(specs, "cable_type")
    if ct == "DAC" and re.search(r"active optical|\baoc\b", desc, re.I) and not _AOC_PRODUCT.search(desc):
        errs.append("类型判为 DAC(无源铜) 但描述含 AOC/有源光，疑似冲突")
    if ct == "AOC" and re.search(r"passive copper|无源铜|\bdac\b", desc, re.I):
        errs.append("类型判为 AOC(有源光) 但描述含 DAC/无源铜，疑似冲突")
    if ct in ("Mini-SAS", "Mini-SAS HD", "SlimSAS") and _val(specs, "interface_speed"):
        errs.append("SAS 内部线不应有网络高速速率")
    return errs


# ─────────────────────────── 其他备件/耗材 MISC（taxonomy 10）───────────────────────────
# 本类无子码、几乎无可渲染结构：真实行只能稳定抽到「品牌 + 配件类型词」两项。
# 型号串/料号/自由文字一律不渲染（无证据不猜）。识别不出确定类型词 → item_type=None → 转人工。
#
# 修正点（对抗验证结论）：
#  1) FIELD_SCHEMA 新增 'MISC': ['brand', 'item_type']（原代码缺失，导致 _run 会把字段剔空）。
#  2) extract_misc 增加品牌中文名回退：resolve_brand 识别出英文规范名(bn)但系统无中文映射(bz=None)
#     时（EMC/IBM/AMD 等），从氚云品牌字段「中文（English）」的中文前缀回填 bz，避免把已识别的真品牌
#     当成占位词丢弃；仍对「其他」占位词及纯英文/未知品牌安全丢弃。
#  3) 编排接线：standardize() 需新增 cat-10 分发分支（见文件 standardize() 内，落地说明见 notes）。


_MISC_TYPES = [  # (canonical 类型词, 证据正则) —— 按特异性排序，首命中即取
    ("Rail Kit", r"rail\s*kit|滑\s*轨|导\s*轨"),
    ("Air Baffle", r"air\s*baffle|风\s*?道|导\s*风\s*罩"),
    ("Drive Caddy", r"\bcaddy\b|硬盘\s*托架|盘\s*托"),
    ("Drive Blank", r"\bblank\b|假\s*(?:盘|条)|盲\s*板"),
    ("Bezel", r"\bbezel\b|前\s*面\s*板|面\s*板"),
    ("Bracket", r"\bbracket\b|支\s*架|挂\s*架"),
    ("Optical Drive", r"\bdvd[\s-]?(?:rom|rw)?\b|\bcd[\s-]?rom\b|光\s*驱"),
    ("Tray", r"\btray\b|托\s*架"),
    ("Cover", r"\bcover\b|盖\s*板|顶\s*盖"),
    ("Chassis", r"\bchassis\b|机\s*箱"),
    ("Label", r"\blabel\b|标\s*签"),
]
_MISC_TYPE_RX = [(name, re.compile(rx, re.I)) for name, rx in _MISC_TYPES]

# 氚云品牌字段「中文（English）」→ 取中文前缀（识别出英文规范名但缺 BRAND_ZH 时的回退）
_BRAND_CJK_PREFIX = re.compile(r"\s*([^（(]+)[（(]")


# 附件语境：类型词紧跟在 with/w//含/带/配/附 之后 → 是主对象的"附带件"提及，不是本体
# （实测：'IBM ... Tape Drive with Caddy for TS4500' 曾被压成 'Drive Caddy' 且自动通过）。
# 只看类型词**前方**语境，'Bracket with Latch'/'Bezel with Lock'（附件在后）不受影响。
_ACCESSORY_CTX = re.compile(r"(?:\bwith|\bw/|[含带配附])\s*$", re.I)


def _misc_item_type(desc):
    for name, rx in _MISC_TYPE_RX:
        m = rx.search(desc)
        if m:
            if _ACCESSORY_CTX.search(desc[max(0, m.start() - 8):m.start()]):
                continue                   # 附件提及（with Caddy/带托架）→ 不当本体，试下一类型
            return _F(name, EXPLICIT, m.group(0).strip())
    return None                            # 认不出确定类型 → 不猜，转人工


def _misc_brand_field(brand, desc):
    """渲染用品牌「中文（英文）」；无法可靠得到中文显示名 → None（不渲染伪品牌）。"""
    bn, bz = T.resolve_brand(brand, desc)   # (英文规范名, 中文名)
    if not bn:
        return None
    # 修正 2：识别出英文规范名但系统无中文映射(如 EMC/IBM/AMD) → 回填氚云字段里的中文前缀，
    # 避免把已识别的真品牌当占位词丢弃。
    if not bz:
        m = _BRAND_CJK_PREFIX.match(brand or "")
        bz = m.group(1).strip() if m else None
    # resolve_brand 回退到原样字段时英=中（占位「其他」/未识别）→ bn==bz → 丢弃伪品牌
    if bn and bz and bn != bz:
        return _F(f"{bz}（{bn}）", EXPLICIT, brand or bn)
    return None


def extract_misc(desc, brand=""):
    specs = {}
    bf = _misc_brand_field(brand, desc)
    if bf:
        specs["brand"] = bf
    it = _misc_item_type(desc)
    if it:
        specs["item_type"] = it
    return specs


def render_misc(specs):
    """{品牌} {配件类型} —— 仅渲染已抽到的品牌与确定类型词；缺类型词返回 None。"""
    it = _val(specs, "item_type")
    if not it:                             # 无确定可渲染结构 → 转人工
        return None
    seg = []
    if _val(specs, "brand"):
        seg.append(_val(specs, "brand"))
    seg.append(it)
    return " ".join(seg)


def classify_misc_l2(specs):
    """其他备件/耗材无子码：识别出确定类型词即归 10，否则 None（转人工）。"""
    return "10" if _val(specs, "item_type") else None


def validate_misc(l2, specs, desc, brand=""):
    errs = []
    # 该桶常混入真正属于其它类目的行（SSD/内存/CPU 等）——标出疑似归类错误，转人工复核
    if re.search(r"\bnvme\b|\bssd\b|\bm\.2\b|\bmlc\b|\btlc\b|固态", desc, re.I):
        errs.append("其他备件桶出现固态盘特征词，疑似归类错误")
    if re.search(r"\bddr[2-5]\b|\brdimm\b|\budimm\b", desc, re.I):
        errs.append("其他备件桶出现内存特征词，疑似归类错误")
    # 信息守恒（对齐 GPU v1.2.5 哲学）：结构件不该带"主设备"特征——出现即转人工，不静默丢信息。
    # 实测案例：IBM 3592-55F TS1155 磁带机被压成 'Drive Caddy' 自动通过。
    if re.search(r"tape\s*(?:drive|library)|磁带|\blto-?\d*\b|\bts\d{3,4}\b", desc, re.I):
        errs.append("出现磁带机/磁带库特征词，疑似主设备而非结构件")
    if re.search(r"\b\d+(?:\.\d+)?\s*TB\b", desc, re.I):
        errs.append("出现 TB 级容量特征词，疑似盘/驱动器等主设备")
    # 品牌冲突：字段与描述各自认出的品牌不一致（如字段=联想、描述=IBM）→ 不猜谁对，转人工。
    # 仅 MISC 桶启用——主板类的 联想（IBM）→Lenovo 是沿革内故意行为，不受影响。
    fb, db = T.recognize_brand(brand), T.recognize_brand(desc)
    if fb and db and fb != db:
        errs.append(f"品牌字段({fb})与描述品牌({db})冲突")
    return errs

# 编排接线（修正 3，需加入 standardize()，在「其余类目模板兜底」之前）：
#   if l2_code == "10" or l1_code == "10":
#       return _run(out, "MISC", extract_misc(desc, brand),
#                   render_misc, classify_misc_l2, validate_misc, desc)


# ─────────────────────────── 编排 ───────────────────────────
def standardize(pn: str, description: str | None, brand: str = "") -> dict:
    """全管线：识别类型 → 抽字段 → 分类 → 渲染 → 校验 → review_status。"""
    desc = description or ""
    cls = classify.classify_part(desc, pn, brand) or {}
    l1_code, l2_code = cls.get("l1_code"), cls.get("l2_code")
    brand_norm, brand_zh = T.resolve_brand(brand, desc)
    out = {
        "pn": pn, "object_type": None, "category_l1": cls.get("category_l1"),
        "category_l2": cls.get("category_l2"), "whole_system": bool(cls.get("whole_system")),
        "structured_specs": {}, "canonical_description": None,
        "brand_raw": brand or None, "brand_norm": brand_norm, "brand_zh": brand_zh,
        "validation_errors": [], "review_status": REVIEW,
    }
    if cls.get("whole_system"):
        out["object_type"] = "WHOLE_SYSTEM"
        return out

    # HDD/SSD 由介质词区分（同 02 大类）
    if l1_code == "02" and _is_ssd(desc):
        return _run(out, "DRIVE_SSD", extract_ssd(desc), render_ssd, classify_ssd_l2, validate_ssd, desc)
    if l1_code == "02" and _is_hdd(desc):
        return _run(out, "DRIVE_HDD", extract_hdd(desc), render_hdd, classify_hdd_l2, validate_hdd, desc)
    if l1_code == "01":
        return _run(out, "MEMORY", extract_memory(desc), render_memory, classify_mem_l2, validate_memory, desc)
    if l1_code == "05":
        return _run(out, "CPU", extract_cpu(desc, pn, brand), render_cpu, classify_cpu_l2, validate_cpu, desc)
    if l2_code == "0404":
        specs = extract_gpu(desc, brand, pn)
        out["object_type"] = "GPU"
        out["structured_specs"] = {k: specs[k] for k in FIELD_SCHEMA["GPU"] if k in specs}
        out["canonical_description"] = _with_brand(render_gpu(specs), brand, desc)
        out["validation_errors"] = validate_gpu(desc, specs)
        out["review_status"] = AUTO_OK if (out["canonical_description"] and not out["validation_errors"]) else REVIEW
        return out

    if l2_code == "0301":
        return _run(out, "MAINBOARD", extract_mainboard(desc, pn, brand), render_mainboard, classify_mainboard_l2, validate_mainboard, desc)
    if l2_code == "0302":
        return _run(out, "BACKPLANE", extract_backplane(desc), render_backplane, classify_backplane_l2, validate_backplane, desc)
    if l2_code in ("0401", "0402"):
        return _run(out, "RAID_HBA", extract_raid_hba(desc, pn, brand), render_raid_hba, classify_raid_hba_l2, validate_raid_hba, desc)
    if l2_code in ("0403", "0405"):
        return _run(out, "NIC_FC", extract_nic_fc(desc, pn, brand), render_nic_fc, classify_nic_fc_l2, validate_nic_fc, desc)
    if l2_code == "0499":
        return _run(out, "OTHER_CARD", extract_other_card(desc, brand), render_other_card, classify_other_card_l2, validate_other_card, desc)
    if l2_code == "0901":
        return _run(out, "OPTICS", extract_optics(desc, pn, brand), render_optics, classify_optics_l2, validate_optics, desc)
    if l2_code == "0902":
        return _run(out, "CABLE", extract_cable(desc, brand), render_cable, classify_cable_l2, validate_cable, desc)
    if l1_code == "06":
        return _run(out, "PSU", extract_psu(desc, pn, brand), render_psu, classify_psu_l2, validate_psu, desc)
    if l1_code == "07":
        return _run(out, "BATTERY", extract_battery(desc, brand), render_battery, classify_battery_l2, validate_battery, desc)
    if l1_code == "08":
        return _run(out, "COOLING", extract_cooling(desc, brand), render_cooling, classify_cooling_l2, validate_cooling, desc)
    if l1_code == "10":
        return _run(out, "MISC", extract_misc(desc, brand), render_misc, classify_misc_l2,
                    lambda l2, specs, d: validate_misc(l2, specs, d, brand), desc)

    # 其余类目：暂委托现有模板（卡/电源/光模块/线缆/主板/背板/风扇/电池）
    out["object_type"] = "OTHER"
    fn = normalize_templates.RENDERERS.get(l2_code) or normalize_templates.RENDERERS_L1.get(l1_code)
    if fn:
        try:
            out["canonical_description"] = _with_brand(fn(desc), brand, desc)
        except Exception:  # noqa: BLE001
            out["canonical_description"] = None
    out["review_status"] = AUTO_OK if out["canonical_description"] else REVIEW
    return out


def _with_brand(canon, brand_raw, desc):
    """品牌进标准描述（甲方 2026-07-03：用户在描述里看不到 Seagate 等品牌）。

    已识别的规范品牌且描述文本尚未含该品牌词 → 前缀（'Seagate 8TB ...'）；
    识别不了（占位「其他」/未知写法）不猜、不前缀；模板本身已含品牌（主板/杂项等）不重复。
    """
    if not canon:
        return canon
    bn = T.recognize_brand(brand_raw) or T.recognize_brand(desc)
    if bn and bn.lower() not in canon.lower():
        return f"{bn} {canon}"
    return canon


def _run(out, obj_type, specs, render, classify_l2, validate, desc):
    out["object_type"] = obj_type
    # 只保留 schema 声明的字段（按声明顺序）——结构强制"字段不乱"，剔除内部/越界字段
    allowed = FIELD_SCHEMA.get(obj_type, [])
    out["structured_specs"] = {k: specs[k] for k in allowed if k in specs}
    canon = _with_brand(render(specs), out.get("brand_raw"), desc)
    out["canonical_description"] = canon
    # 分类由抽到的字段决定（缺关键字段 → None，转人工）
    l2 = classify_l2(specs)
    out["category_l2"] = T.CATEGORY_NAMES.get(l2) if l2 else None
    out["_l2_code"] = l2
    out["validation_errors"] = validate(l2, specs, desc)
    no_l2 = l2 is None
    out["review_status"] = (AUTO_OK if (canon and not out["validation_errors"] and not no_l2)
                            else REVIEW)
    return out


def _is_ssd(desc):
    low = desc.lower()
    return bool(re.search(r"\bssd\b|固态|nvme|solid state", low))


def _is_hdd(desc):
    low = desc.lower()
    return bool(re.search(r"\bhdd\b|硬盘|机械盘|机械硬盘", low)) and not _is_ssd(desc)
