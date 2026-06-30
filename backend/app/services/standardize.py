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
    "GPU": ["brand", "model", "memory_capacity", "memory_type", "form_factor"],
    "CPU": ["brand", "family", "model", "cores", "base_freq", "l3_cache", "tdp"],
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
_GPU_MODEL = re.compile(
    r"(GeForce\s+RTX\s*\d{3,4}\w*|RTX\s*\d{3,4}\w*|Tesla\s+\w+|Quadro\s+\w+|"
    r"\bA100\b|\bA800\b|\bA30\b|\bA40\b|\bH100\b|\bH200\b|\bH800\b|\bV100\b|\bT4\b|\bL4\b|\bL40S?\b|"
    r"\bMI\d{2,3}\w*|Radeon\s+\w+|Instinct\s+\w+)", re.I)
_GPU_BRAND_BY_MODEL = [
    (re.compile(r"rtx|gtx|geforce|tesla|quadro|^a\d{2,3}|h[12]00|h800|v100|t4|l4|l40", re.I), "NVIDIA"),
    (re.compile(r"\bmi\d|radeon|instinct|firepro", re.I), "AMD"),
]
_GPU_FORM = re.compile(r"\b(SXM5|SXM4|SXM2|SXM|OAM|PCIe|Mezzanine)\b", re.I)
_GPU_VRAM_TYPE = re.compile(r"(HBM3e|HBM3|HBM2e|HBM2|GDDR6X|GDDR6|GDDR5X|GDDR5)\b", re.I)


def extract_gpu(desc, brand_raw=""):
    specs = {}
    m = _GPU_MODEL.search(desc)
    if m:
        model = re.sub(r"\s+", " ", m.group(1).strip())
        model = re.sub(r"RTX\s*(\d)", r"RTX \1", model, flags=re.I)   # RTX4090 → RTX 4090
        specs["model"] = _F(model, EXPLICIT, m.group(0))
    # 品牌：描述/字段显式 → EXPLICIT；否则型号字典推导（RTX→NVIDIA）
    bn = T.recognize_brand(brand_raw) or T.recognize_brand(desc)
    if bn:
        specs["brand"] = _F(bn, EXPLICIT, bn)
    elif specs.get("model"):
        for rx, b in _GPU_BRAND_BY_MODEL:
            if rx.search(specs["model"]["value"]):
                specs["brand"] = _F(b, DICT, f"{specs['model']['value']}→{b}")
                break
    m = re.search(r"(\d{2,3})\s*GB\b", desc, re.I)
    if m:
        specs["memory_capacity"] = _F(f"{m.group(1)}GB", EXPLICIT, m.group(0))
    m = _GPU_VRAM_TYPE.search(desc)
    if m:
        specs["memory_type"] = _F(m.group(1), EXPLICIT, m.group(0))   # 原大小写：HBM3e
    m = _GPU_FORM.search(desc)
    if m:
        specs["form_factor"] = _F(m.group(1).upper().replace("PCIE", "PCIe"), EXPLICIT, m.group(0))
    return specs


def render_gpu(specs):
    """{品牌} {型号} {显存容量} {显存类型} {形态} GPU —— 形态只从证据来，不默认 PCIe。"""
    if not _val(specs, "model"):
        return None
    seg = []
    for k in ("brand", "model", "memory_capacity", "memory_type", "form_factor"):
        if _val(specs, k):
            seg.append(_val(specs, k))
    seg.append("GPU")
    return " ".join(seg)


def validate_gpu(desc, specs):
    errs = []
    low = desc.lower()
    form = _val(specs, "form_factor")
    if re.search(r"\bsxm", low) and form and form.startswith("PCIe"):
        errs.append("原描述 SXM 不得变成 PCIe")
    if "pcie" in low and form and form.startswith("SXM"):
        errs.append("原描述 PCIe 不得变成 SXM")
    if not _val(specs, "model"):
        errs.append("GPU 型号丢失")
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
        specs = extract_gpu(desc, brand)
        out["object_type"] = "GPU"
        out["structured_specs"] = {k: specs[k] for k in FIELD_SCHEMA["GPU"] if k in specs}
        out["canonical_description"] = render_gpu(specs)
        out["validation_errors"] = validate_gpu(desc, specs)
        out["review_status"] = AUTO_OK if (out["canonical_description"] and not out["validation_errors"]) else REVIEW
        return out

    # 其余类目：暂委托现有模板（卡/电源/光模块/线缆/主板/背板/风扇/电池）
    out["object_type"] = "OTHER"
    fn = normalize_templates.RENDERERS.get(l2_code) or normalize_templates.RENDERERS_L1.get(l1_code)
    if fn:
        try:
            out["canonical_description"] = fn(desc)
        except Exception:  # noqa: BLE001
            out["canonical_description"] = None
    out["review_status"] = AUTO_OK if out["canonical_description"] else REVIEW
    return out


def _run(out, obj_type, specs, render, classify_l2, validate, desc):
    out["object_type"] = obj_type
    # 只保留 schema 声明的字段（按声明顺序）——结构强制"字段不乱"，剔除内部/越界字段
    allowed = FIELD_SCHEMA.get(obj_type, [])
    out["structured_specs"] = {k: specs[k] for k in allowed if k in specs}
    canon = render(specs)
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
