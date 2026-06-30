"""各类备件的标准描述模板渲染（甲方 2026-06-30 各类目模板）。

每个 render_* 从原始描述抽字段、按固定字段顺序拼标准描述；缺关键项返回 None（交人工）。
硬盘/内存在 normalize.py 已实现；本模块覆盖 CPU / 卡 / 光模块 / 线缆 / 电源 / 电池 / 主板背板风扇。
单位统一：速率 12Gb/s、网卡 10GbE、总线 PCIe 3.0 / OCP 3.0、端口 2-Port。
"""
import re

# ── 通用字段抽取 ──
_PORTS = re.compile(r"(\d+)\s*[-]?\s*(?:port|端口|口)\b", re.I)
_PCIE = re.compile(r"pcie\s*([0-9](?:\.[0-9])?)?", re.I)   # 版本可选：PCIe 3.0 / 裸 PCIe
_OCP = re.compile(r"ocp\s*([0-9](?:\.[0-9])?)", re.I)
_NIC_BRANDS = {"intel", "mellanox", "broadcom", "nvidia", "marvell", "qlogic", "cisco",
               "solarflare", "silicom", "emulex"}
_FC_BRANDS = {"qlogic", "emulex", "broadcom", "marvell", "brocade", "atto"}
_GBPS = re.compile(r"(\d+)\s*gb(?:ps|/s)?\b", re.I)         # 12Gb/s（SAS/卡）
_GBE = re.compile(r"(\d+)\s*gbe\b", re.I)                    # 10GbE（网卡）
_CARD_CACHE = re.compile(r"(\d+)\s*gb?\s*cache", re.I)       # 2GB Cache
_CONNECTOR = re.compile(r"(SFP28|SFP\+|QSFP28|QSFP\+|QSFP-DD|QSFP|RJ-?45|Base-?T)", re.I)
_CORES = re.compile(r"(\d+)\s*(?:c\b|core|cores|核)", re.I)
_GHZ = re.compile(r"([0-9.]+)\s*ghz", re.I)
_WATT = re.compile(r"(\d+)\s*w\b", re.I)
_MB_CACHE = re.compile(r"([0-9.]+)\s*mb?\s*(?:l[23])?\s*(?:cache|缓存)?", re.I)


def _bus(desc):
    m = _PCIE.search(desc)
    if m:
        return f"PCIe {m.group(1)}" if m.group(1) else "PCIe"
    m = _OCP.search(desc)
    if m:
        return f"OCP {m.group(1)}"
    return None


def _brand_prefix(desc, start, brands):
    """模型前紧邻的品牌词（X710-DA2 → Intel X710-DA2），保留原写法不归一。"""
    pre = desc[:start].strip().split()
    return pre[-1] if pre and pre[-1].lower() in brands else None


def _ports(desc):
    m = _PORTS.search(desc)
    return f"{m.group(1)}-Port" if m else None


def _search(rx, desc, fmt):
    m = rx.search(desc)
    return fmt(m) if m else None


def _join(parts):
    return " ".join(p for p in parts if p) or None


# ── CPU ──
_INTEL_SERIES = re.compile(r"\b(Bronze|Silver|Gold|Platinum)\b", re.I)
_INTEL_MODEL = re.compile(r"\b(\d{4}[A-Z]*)\b")
_INTEL_LEGACY = re.compile(r"\b(E[357]-\d{4}[A-Z]?\d?|W-\d{4}[A-Z]?|D-\d{3,4}[A-Z]*)\b", re.I)
_EPYC = re.compile(r"EPYC\s*-?\s*(\d{3,4}[A-Z]*)", re.I)


def render_cpu(desc):
    low = desc.lower()
    cores = _search(_CORES, desc, lambda m: f"{m.group(1)}C")
    freq = _search(_GHZ, desc, lambda m: f"{m.group(1)}GHz")
    tdp = _search(_WATT, desc, lambda m: f"{m.group(1)}W")
    # 缓存：取带 MB 的（避开把功率/频率当缓存）；要求附近有 cache/缓存或 MB 显式
    cm = re.search(r"([0-9.]+)\s*mb\b", desc, re.I)
    cache = f"{cm.group(1)}MB" if cm else None
    if "epyc" in low:
        m = _EPYC.search(desc)
        model = m.group(1) if m else None
        return _join(["AMD EPYC", model, cores, freq, cache, tdp]) if (model and cores) else None
    if "xeon" in low or "intel" in low:
        s = _INTEL_SERIES.search(desc)
        leg = _INTEL_LEGACY.search(desc)
        if s:
            mm = _INTEL_MODEL.search(desc[s.end():])
            model = f"{s.group(1).title()} {mm.group(1)}" if mm else s.group(1).title()
        elif leg:
            model = leg.group(1).upper()
        else:
            return None
        return _join(["Intel Xeon", model, cores, freq, cache, tdp]) if cores else None
    return None


# ── 卡：RAID / HBA / NIC / FC / GPU ──
_RAID_MODEL = re.compile(
    r"(Smart Array\s+\S+|MegaRAID\s+\S+|PERC\s+\S+|ServeRAID\s+\S+|P\d{3}\w*-?\w*|9\d{3}-\d+i|H\d{3}\w*)", re.I)
_HBA_MODEL = re.compile(r"(9\d{3}-\d+[ie]|LSI\s+\S+|SAS\d{4}\w*|H\d{3,4}\w*)", re.I)
_NIC_MODEL = re.compile(
    r"(X\d{3}-\w+|ConnectX-\d+\w*|E810[\w-]*|82599[\w-]*|I350[\w-]*|X5\d{2}[\w-]*|BCM\d+\w*|FastLinQ\s+\S+)", re.I)
_FC_MODEL = re.compile(r"(QLE\d+|LPe\d+|QME\d+|LPm\d+)", re.I)
_GPU_MODEL = re.compile(
    r"(Tesla\s+\w+|A100|H100|H200|V100|T4|L4|L40S?|RTX\s*\w+|Quadro\s+\w+|MI\d+\w*|A\d{2,3}|A800)", re.I)
_VRAM = re.compile(r"(\d+)\s*gb\b", re.I)
_VRAM_TYPE = re.compile(r"(GDDR\d\w*|HBM\d\w*|DDR\d)", re.I)


def render_raid(desc):
    m = _RAID_MODEL.search(desc)
    speed = _search(_GBPS, desc, lambda x: f"{x.group(1)}Gb/s")
    itf = "SAS" if re.search(r"\bSAS\b", desc, re.I) else ("SATA" if re.search(r"\bSATA\b", desc, re.I) else None)
    cache = _search(_CARD_CACHE, desc, lambda x: f"{x.group(1)}GB Cache")
    if not m:
        return None
    return _join([m.group(1).strip(), speed, itf, _ports(desc), cache, _bus(desc), "RAID Controller"])


def render_hba(desc):
    m = _HBA_MODEL.search(desc)
    speed = _search(_GBPS, desc, lambda x: f"{x.group(1)}Gb/s")
    itf = "SAS" if re.search(r"\bSAS\b", desc, re.I) else None
    if not m:
        return None
    return _join([m.group(1).strip(), speed, itf, _ports(desc), _bus(desc), "HBA"])


def render_nic(desc):
    m = _NIC_MODEL.search(desc)
    if not m:
        return None
    model = m.group(1).strip()
    b = _brand_prefix(desc, m.start(), _NIC_BRANDS)
    if b:
        model = f"{b} {model}"
    speed = _search(_GBE, desc, lambda x: f"{x.group(1)}GbE")
    conn = _search(_CONNECTOR, desc, lambda x: x.group(1).upper().replace("RJ45", "RJ-45"))
    return _join([model, speed, _ports(desc), conn, _bus(desc), "NIC"])


def render_fc(desc):
    m = _FC_MODEL.search(desc)
    if not m:
        return None
    model = m.group(1).strip()
    b = _brand_prefix(desc, m.start(), _FC_BRANDS)
    if b:
        model = f"{b} {model}"
    sp = re.search(r"(\d+)\s*gb?\b", desc, re.I)
    speed = f"{sp.group(1)}Gb" if sp else None
    return _join([model, speed, _ports(desc), _bus(desc), "Fibre Channel HBA"])


def render_gpu(desc):
    m = _GPU_MODEL.search(desc)
    if not m:
        return None
    model = re.sub(r"\s+", " ", m.group(1).strip())
    vram = _search(_VRAM, desc, lambda x: f"{x.group(1)}GB")
    vtype = _search(_VRAM_TYPE, desc, lambda x: x.group(1))   # 保留原大小写（GDDR6 / HBM2e）
    return _join([model, vram, vtype, _bus(desc) or "PCIe", "GPU"])


# ── 光模块 ──
_OPTIC_FORM = re.compile(r"(QSFP28|QSFP-DD|QSFP\+|QSFP|SFP28|SFP56|SFP\+|OSFP|XFP|CFP\d?|GBIC)", re.I)
_WAVELEN = re.compile(r"(\d{3,4})\s*nm", re.I)
_PMD = re.compile(r"\b(SR4|LR4|ER4|FR4|DR4|PSM4|CWDM4|SR|LR|ER|ZR)\b")   # 标准码替代波长
_DIST = re.compile(r"(\d+(?:\.\d+)?)\s*(k?m)\b", re.I)
_FIBER = re.compile(r"\b(SMF|MMF|单模|多模)\b", re.I)
_OPTIC_SPEED = re.compile(r"(\d+)\s*gb?\b", re.I)                          # 100Gb / 10Gb


def render_optic(desc):
    form = _search(_OPTIC_FORM, desc, lambda m: m.group(1).upper())
    if not form:
        return None
    speed = _search(_OPTIC_SPEED, desc, lambda m: f"{m.group(1)}Gb")
    wl = _search(_WAVELEN, desc, lambda m: f"{m.group(1)}nm") or _search(_PMD, desc, lambda m: m.group(1))
    dist = _search(_DIST, desc, lambda m: f"{m.group(1)}{m.group(2).lower()}")
    fiber = _search(_FIBER, desc, lambda m: {"单模": "SMF", "多模": "MMF"}.get(m.group(1), m.group(1).upper()))
    return _join([speed, form, wl, dist, fiber, "Optical Transceiver"])


# ── 线缆 ──
_CABLE_CONN = re.compile(r"(SFF-\d{4}|Mini-SAS HD|Mini-SAS|SlimSAS|OCuLink|QSFP28|QSFP\+|SFP28|SFP\+)", re.I)
_LEN = re.compile(r"(\d+(?:\.\d+)?)\s*m\b", re.I)
_CABLE_TYPE = re.compile(r"(Passive DAC|Active Optical Cable|Active Optical|AOC|DAC|Twinax)", re.I)


def render_cable(desc):
    length = _search(_LEN, desc, lambda m: f"{m.group(1)}m")
    tm = _CABLE_TYPE.search(desc)
    ctype = {"dac": "Passive DAC", "aoc": "Active Optical"}.get(
        (tm.group(1).lower() if tm else ""), (tm.group(1) if tm else None)) if tm else None
    # 连接器短语 = 去掉 长度/类型/Cable 后的剩余（保留 'A to B' 结构）
    s = re.sub(r"\d+(?:\.\d+)?\s*m\b", "", desc, flags=re.I)
    s = _CABLE_TYPE.sub("", s)
    s = re.sub(r"\bcable\b|线缆|跳线", "", s, flags=re.I)
    prefix = re.sub(r"\s+", " ", s).strip(" ,-")
    if " to " not in prefix.lower() and not _CABLE_CONN.search(prefix):
        return None
    return _join([prefix, length, f"{ctype} Cable" if ctype else "Cable"])


# ── 电源 ──
def render_psu(desc):
    w = re.search(r"(\d+)\s*w\b", desc, re.I)
    if not w:
        return None
    inp = "AC" if re.search(r"\bAC\b|交流", desc, re.I) else ("DC" if re.search(r"\bDC\b|直流", desc, re.I) else None)
    eff = re.search(r"80\s*PLUS\s*(Titanium|Platinum|Gold|Silver|Bronze)", desc, re.I)
    eff = f"80 PLUS {eff.group(1).title()}" if eff else None
    hot = "Hot-Plug" if re.search(r"hot[\s-]?(plug|swap)|热插拔", desc, re.I) else None
    return _join([f"{w.group(1)}W", inp, eff, hot, "Power Supply"])


# ── 电池/超级电容 ──
def render_battery(desc):
    low = desc.lower()
    v = re.search(r"([0-9.]+)\s*v\b", desc, re.I)
    if "supercap" in low or "super cap" in low or "超级电容" in low or "cachevault" in low:
        return _join(["Supercapacitor", v and f"{v.group(1)}V"])
    if "cmos" in low or "纽扣" in low:
        return _join([v and f"{v.group(1)}V", "CMOS Battery"])
    if "raid" in low and ("battery" in low or "电池" in low):
        return "RAID Cache Battery"
    if "battery" in low or "电池" in low or "bbu" in low:
        return _join([v and f"{v.group(1)}V", "Battery"])
    return None


# ── 主板 / 背板 / 风扇（品牌+机型型：保留机型 + 统一类型后缀）──
def _machine_prefix(desc, drop_rx):
    """去掉类型后缀词，留下品牌+机型前缀（如 Dell PowerEdge R740）。"""
    s = re.sub(drop_rx, "", desc, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" ,-")


def render_board(desc):
    pre = _machine_prefix(desc, r"(system\s*board|systemboard|motherboard|mainboard|planar|主板|主逻辑板|server board)")
    return f"{pre} System Board" if pre else None


def render_backplane(desc):
    # 品牌+机型型：机型前缀（含盘位/接口）原样保留 + 统一后缀，避免重复拼接
    pre = _machine_prefix(desc, r"(drive\s*backplane|backplane|背板)")
    return f"{pre} Drive Backplane" if pre else None


def render_fan(desc):
    pre = _machine_prefix(desc, r"(fan\s*module|fan\s*assembly|cooling\s*fan|fan|风扇|散热)")
    return f"{pre} Fan Module" if pre else None


# l2_code / l1_code → renderer
RENDERERS = {
    "0401": render_raid, "0402": render_hba, "0403": render_nic, "0404": render_gpu, "0405": render_fc,
    "0901": render_optic, "0902": render_cable, "0301": render_board, "0302": render_backplane,
}
RENDERERS_L1 = {"05": render_cpu, "06": render_psu, "07": render_battery, "08": render_fan}
