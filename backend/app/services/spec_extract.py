"""规格结构化抽取（整改 P2，审核说明 §4.3/原则3）。

只做高频且语义规整的三类：机械盘 / SSD / 内存（实测约 6900+1500 个型号可抽）。
GPU/电源/网卡等没有"容量/接口"统一语义，不强行抽取。
规则与键定义必须同步演进，故 SPEC_KEYS 作为常量放本模块，不建字典表。

抽取结果 source='auto'，只补缺不覆盖（人工维护的 source='manual' 行优先）。
"""
import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.master_data import ProductSpec

# spec_key → (中文名, 单位)。numeric_value 口径：capacity=GB、rpm=RPM、frequency=MHz、speed=Gbps、cache=MB
SPEC_KEYS = {
    "part_type": ("部件类型", None),       # HDD / SSD / RAM
    "capacity": ("容量", "GB"),
    "interface": ("接口", None),           # SAS / SATA / NVME / FC / SCSI
    "speed": ("接口速率", "Gbps"),         # 3 / 6 / 12 / 24 Gbps
    "rpm": ("转速", "RPM"),
    "cache": ("缓存", "MB"),               # 128 / 256 MB Cache
    "form_factor": ("尺寸/封装", None),    # 2.5 / 3.5 / RDIMM / LRDIMM ...
    "generation": ("内存代数", None),      # DDR2~DDR5
    "frequency": ("内存频率", "MHz"),
    "rank": ("Rank/位宽", None),           # 2Rx4 / 1Rx8
}

# ---- 信号与守卫 ----
_DISK_SIGNAL = re.compile(r"(SAS|SATA|NVME|SSD|HDD|硬盘|SOLID STATE|FLASH DRIVE)", re.I)
# 控制器/阵列卡/线缆等也含 SAS 字样，不是盘
_DISK_GUARD = re.compile(
    r"(CONTROLLER|控制器|SMART ARRAY|阵列|RAID|HBA|EXPANDER|扩展|背板|BACKPLANE|"
    r"CABLE|线缆|网卡|ADAPTER|风扇|电源|POWER SUPPLY|TRAY|托架|支架|CADDY)", re.I)
_MEM_SIGNAL = re.compile(r"(DDR[2-5]|PC[2-5]L?-\d|RDIMM|LRDIMM|UDIMM|SODIMM|内存|MEMORY)", re.I)

_SSD = re.compile(r"(SSD|SOLID STATE|NVME|固态)", re.I)
# T 容量：负向先行避免匹配料号内部数字（如 02311T）；上界+前导零守卫在 _disk_capacity 里
_CAP_T = re.compile(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*TB?\b", re.I)
# G 容量：≥100 才认（3G/6G/12G 是接口速率、2GB 是缓存）
_CAP_G = re.compile(r"(\d{3,5})\s*GB?\b", re.I)
_MAX_DISK_TB = 64   # 单盘真实容量上界，过滤被误当容量的料号片段
_INTERFACE = ["NVME", "SAS", "SATA", "FC", "SCSI"]
_RPM = re.compile(r"(\d+(?:\.\d+)?)\s*K\b", re.I)
# 整数转速：7200转 / 10000 RPM → 归一成 K（7.2K / 10K）
_RPM_FULL = re.compile(r"(\d{4,5})\s*(?:转|RPM|R/MIN)", re.I)
_FORM_DISK = re.compile(r"(2\.5|3\.5)\s*(寸|英寸|INCH|\")?", re.I)
# 接口速率：12Gbps / 12Gb/s（带 b）；或裸 6G/12G（1~2 位数 + G 且非 GB，盘语境=速率）
_SPEED = re.compile(r"(\d+(?:\.\d+)?)\s*Gb(?:ps|/s)?\b", re.I)
_SPEED_BARE = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})\s*G(?![Bb])\b", re.I)
# 缓存：128MB Cache / 256MB / 256M（盘语境下 M(B) 即缓存）
_CACHE = re.compile(r"(\d+)\s*MB?\b", re.I)
# 尺寸口语：SFF=2.5 / LFF=3.5

_MEM_CAP = re.compile(r"(?<![A-Z0-9-])(\d{1,3})\s*GB?\b", re.I)
_DDR = re.compile(r"DDR([2-5])", re.I)
_PC_GEN = re.compile(r"PC([2-5])L?-", re.I)
_DDR_FREQ = re.compile(r"DDR[2-5]-(\d{3,4})\b", re.I)
_MHZ = re.compile(r"(\d{3,4})\s*MHZ", re.I)
_PC_CODE = re.compile(r"PC[2-5]L?-(\d{4,5})", re.I)
_MEM_FORM = re.compile(r"(LRDIMM|RDIMM|UDIMM|SODIMM|CM-DIMM|DIMM)", re.I)
_RANK = re.compile(r"(\dR\s*x\s*\d)", re.I)   # 2Rx4 / 1Rx8


def _spec(key: str, value: str, numeric=None) -> dict:
    label, unit = SPEC_KEYS[key]
    return {"spec_key": key, "spec_value": value, "spec_unit": unit,
            "numeric_value": Decimal(str(numeric)) if numeric is not None else None}


def _t_capacity(desc: str) -> dict | None:
    """从描述抽 TB 容量；过滤被误当容量的料号片段（前导零多位 / 超上界）。"""
    for m in _CAP_T.finditer(desc):
        raw = m.group(1)
        int_part = raw.split(".")[0]
        if len(int_part) > 1 and int_part.startswith("0"):
            continue                       # 02311T 等料号片段
        val = float(raw)
        if 0 < val <= _MAX_DISK_TB:
            return _spec("capacity", f"{raw}TB", val * 1000)
    return None


def _extract_disk(desc: str) -> list[dict]:
    out = []
    is_ssd = bool(_SSD.search(desc))
    out.append(_spec("part_type", "SSD" if is_ssd else "HDD"))

    cap = _t_capacity(desc)               # T 不合理时不短路 GB
    if cap is None:
        m = _CAP_G.search(desc)
        if m and int(m.group(1)) >= 100:
            cap = _spec("capacity", f"{m.group(1)}GB", int(m.group(1)))
    if cap is not None:
        out.append(cap)

    up = desc.upper()
    for itf in _INTERFACE:
        if itf in up:
            out.append(_spec("interface", itf))
            break

    m = _SPEED.search(desc) or _SPEED_BARE.search(desc)   # 12Gbps/12Gb/s 或裸 6G
    if m and 1 <= float(m.group(1)) <= 100:
        out.append(_spec("speed", f"{m.group(1)}Gbps", float(m.group(1))))

    if not is_ssd:
        m = _RPM.search(desc)
        # 转速合理域 4~16K，排除 "256K cache" 之类
        if m and 4 <= float(m.group(1)) <= 16:
            out.append(_spec("rpm", f"{m.group(1)}K", float(m.group(1)) * 1000))
        else:                                  # 整数转速 7200转/10000RPM → 归一成 K
            mf = _RPM_FULL.search(desc)
            if mf and 4000 <= int(mf.group(1)) <= 16000:
                k = int(mf.group(1)) / 1000
                kv = f"{k:g}K"
                out.append(_spec("rpm", kv, int(mf.group(1))))

    m = _CACHE.search(desc)                # 缓存 MB（128/256…）
    if m and 1 <= int(m.group(1)) <= 4096:
        out.append(_spec("cache", f"{m.group(1)}MB", int(m.group(1))))

    m = _FORM_DISK.search(desc)
    if m:
        out.append(_spec("form_factor", m.group(1)))
    elif re.search(r"\bSFF\b", desc, re.I):       # SFF=2.5寸 / LFF=3.5寸
        out.append(_spec("form_factor", "2.5"))
    elif re.search(r"\bLFF\b", desc, re.I):
        out.append(_spec("form_factor", "3.5"))
    return out


def _extract_memory(desc: str) -> list[dict]:
    out = [_spec("part_type", "RAM")]

    m = _MEM_CAP.search(desc)
    if m and int(m.group(1)) in (1, 2, 4, 8, 16, 32, 64, 96, 128, 192, 256, 512):
        out.append(_spec("capacity", f"{m.group(1)}GB", int(m.group(1))))

    gen = None
    m = _DDR.search(desc)
    if m:
        gen = m.group(1)
    else:
        m = _PC_GEN.search(desc)
        if m:
            gen = m.group(1)
    if gen:
        out.append(_spec("generation", f"DDR{gen}"))

    freq = None
    m = _DDR_FREQ.search(desc)
    if m:
        freq = int(m.group(1))
    if freq is None:
        m = _MHZ.search(desc)
        if m:
            freq = int(m.group(1))
    if freq is None:
        m = _PC_CODE.search(desc)
        if m:
            code = int(m.group(1))
            # PC4-25600 是带宽(MB/s)=频率×8；PC4-2666V 是 MT/s 直接当频率
            freq = code // 8 if code >= 10000 else code
    if freq and 200 <= freq <= 9000:
        out.append(_spec("frequency", str(freq), freq))

    m = _MEM_FORM.search(desc)
    if m:
        out.append(_spec("form_factor", m.group(1).upper()))

    m = _RANK.search(desc)
    if m:
        out.append(_spec("rank", m.group(1).replace(" ", "")))
    return out


def extract(description: str | None) -> list[dict]:
    """从描述抽取结构化规格。无法识别的品类返回 []。"""
    if not description:
        return []
    desc = description.strip()
    if _MEM_SIGNAL.search(desc):
        return _extract_memory(desc)
    if _DISK_SIGNAL.search(desc) and not _DISK_GUARD.search(desc):
        return _extract_disk(desc)
    return []


def backfill_specs(db: Session) -> dict:
    """全量幂等回填：对每个 active 型号抽取规格，只补缺不覆盖。"""
    parts = db.execute(
        select(DimPart.id, DimPart.description).where(
            DimPart.status == "active", DimPart.description.is_not(None))
    ).all()
    rows = []
    parts_hit = 0
    for pid, desc in parts:
        specs = extract(desc)
        if specs:
            parts_hit += 1
            rows.extend({**s, "part_id": pid, "source": "auto"} for s in specs)
    inserted = 0
    for i in range(0, len(rows), 1000):
        stmt = pg_insert(ProductSpec).values(rows[i:i + 1000]).on_conflict_do_nothing(
            constraint="uq_spec_part_key")
        inserted += len(db.execute(stmt.returning(ProductSpec.id)).all())
    return {"parts_scanned": len(parts), "parts_with_specs": parts_hit,
            "spec_rows": len(rows), "spec_inserted": inserted}
