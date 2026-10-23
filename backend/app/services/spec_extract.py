"""规格结构化抽取（整改 P2，审核说明 §4.3/原则3）。

只做高频且语义规整的三类：机械盘 / SSD / 内存（实测约 6900+1500 个型号可抽）。
GPU/电源/网卡等没有"容量/接口"统一语义，不强行抽取。
规则与键定义必须同步演进，故 SPEC_KEYS 作为常量放本模块，不建字典表。

抽取结果 source='auto'，只补缺不覆盖（人工维护的 source='manual' 行优先）。
"""
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.master_data import ProductSpec
from app.pn_catalog import api as catalog
from app.services.pn_catalog_legacy_guard import require_authority_command

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
# PCx 带宽码(MB/s) → 速率(MT/s)：PC3-8500=DDR3-1066、PC4-21300=DDR4-2666 …
_PC_BW = {5300: 667, 6400: 800, 8500: 1066, 10600: 1333, 12800: 1600, 14900: 1866,
          17000: 2133, 19200: 2400, 21300: 2666, 23400: 2933, 25600: 3200, 28800: 3600,
          38400: 4800, 44800: 5600, 51200: 6400}
_MEM_FORM = re.compile(r"(LRDIMM|RDIMM|UDIMM|SODIMM|CM-DIMM|DIMM)", re.I)
# 2Rx4 / 2R×4 / 2R*4（分隔符可为 x/×/*）
_RANK = re.compile(r"(\d)\s*R\s*[x×*]\s*(\d)", re.I)
# 英文写法：Single/Dual/Quad/Octal Rank … x4 → 1R/2R/4R/8R + 位宽
_RANK_WORDS = re.compile(r"(Single|Dual|Quad|Octal)[\s-]*Rank[\s,]*[x×*]\s*(\d{1,2})", re.I)
_RANK_N = {"single": "1", "dual": "2", "quad": "4", "octal": "8"}
# Registered（REG/RECC/Registered）= RDIMM
_REG = re.compile(r"\b(REG|RECC|REGISTERED)\b", re.I)
# 频率：4800频率 / 2666 MT/s
_FREQ_CN = re.compile(r"(\d{3,4})\s*(?:频率|MT/?s)", re.I)
# JEDEC 速度等级后缀：2666V / 2933Y / 3200AA → 频率（数字部分）
_DDR_GRADE = re.compile(r"\b([2-9]\d{3})(?:V|Y|W|U|T|P|R|AA|N|K)\b")


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
        m = _MHZ.search(desc) or _FREQ_CN.search(desc)   # 2666MHz / 4800频率 / 2666 MT/s
        if m:
            freq = int(m.group(1))
    if freq is None:
        m = _PC_CODE.search(desc)
        if m:
            code = int(m.group(1))
            # 已知带宽码查表；否则 ≥10000 视作带宽÷8，<10000 视作直接频率（PC4-2666V）
            freq = _PC_BW.get(code) or (code // 8 if code >= 10000 else code)
    if freq is None:
        m = _DDR_GRADE.search(desc)          # 2666V / 3200AA → 2666 / 3200
        if m:
            freq = int(m.group(1))
    if freq and 200 <= freq <= 9000:
        out.append(_spec("frequency", str(freq), freq))

    m = _MEM_FORM.search(desc)
    form = m.group(1).upper() if m else None
    if form in (None, "DIMM") and _REG.search(desc):   # Registered DIMM / REG / RECC → RDIMM
        form = "RDIMM"
    if form:
        out.append(_spec("form_factor", form))

    m = _RANK.search(desc)
    if m:
        out.append(_spec("rank", f"{m.group(1)}Rx{m.group(2)}"))   # 2R×4/2R*4 → 2Rx4
    else:
        rw = _RANK_WORDS.search(desc)        # Dual Rank x4 → 2Rx4
        if rw:
            out.append(_spec("rank", f"{_RANK_N[rw.group(1).lower()]}Rx{rw.group(2)}"))
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


def touch_parts(db: Session, part_ids) -> None:
    """只写 product_specs 的路径须同步推进 dim_part.updated_at（specs 是 /list 输出字段，
    D-36 增量游标靠 updated_at 发现变化）。Core update 显式赋值，不依赖 ORM onupdate。"""
    ids = sorted({int(pid) for pid in part_ids})
    for i in range(0, len(ids), 1000):
        db.execute(update(DimPart).where(DimPart.id.in_(ids[i:i + 1000]))
                   .values(updated_at=func.now())
                   .execution_options(synchronize_session=False))


def _spec_rows(db: Session, part_id: int) -> set[tuple]:
    return set(db.execute(
        select(ProductSpec.spec_key, ProductSpec.spec_value, ProductSpec.spec_unit,
               ProductSpec.numeric_value, ProductSpec.source)
        .where(ProductSpec.part_id == part_id)).all())


def refresh_part_specs(db: Session, part_id: int, description: str | None,
                       *, catalog_record: bool = True) -> None:
    """description 变更后同步该型号的派生规格：删 auto 行 + 按新描述重抽。

    specs 是 description 的纯派生缓存——描述变了缓存必须跟着变，包括新描述抽不出
    规格时清空旧行（否则静默陈旧，生产 6797 条已中招）。source='manual' 行不动，
    重插撞键时 on_conflict_do_nothing 让 manual 优先。不 commit，随调用方事务。
    """
    if catalog_record:
        require_authority_command(db)
    catalog_before = catalog.capture_state(db, part_id) if catalog_record else None
    blocked = catalog.blocked_spec_keys(db, [part_id]).get(part_id, set())
    before = _spec_rows(db, part_id)
    db.execute(delete(ProductSpec).where(ProductSpec.part_id == part_id,
                                         ProductSpec.source == "auto"))
    rows = [{**s, "part_id": part_id, "source": "auto"}
            for s in extract(description) if s["spec_key"] not in blocked]
    if rows:
        db.execute(pg_insert(ProductSpec).values(rows).on_conflict_do_nothing(
            constraint="uq_spec_part_key"))
    if _spec_rows(db, part_id) != before:
        touch_parts(db, [part_id])
    if catalog_record:
        catalog.record_legacy_change(
            db, part_id, catalog_before,
            context=catalog.legacy_context(source="upstream", entrypoint="maintenance_job"),
            action="specs.refresh")


def validate_manual_specs(updates: dict) -> dict:
    """逐键 set/remove；数值使用规范单位，拒绝未知键或有损 Numeric 舍入。"""
    if not isinstance(updates, dict):
        raise ValueError("规格必须按键提交")
    clean = {}
    for key, spec in updates.items():
        if key not in SPEC_KEYS:
            raise ValueError(f"未知规格键: {key}")
        if spec is None:
            clean[key] = None
            continue
        if (not isinstance(spec, dict) or set(spec) != {"value", "unit", "numeric_value"}
                or not isinstance(spec["value"], str) or not spec["value"].strip()
                or len(spec["value"].strip()) > 128):
            raise ValueError(f"规格值非法: {key}")
        if spec["unit"] != SPEC_KEYS[key][1]:
            raise ValueError(f"规格单位非法: {key}")
        number = spec["numeric_value"]
        if number is not None:
            try:
                if not isinstance(number, str):
                    raise ValueError()
                number = Decimal(number)
                if (not number.is_finite() or number < 0 or number >= Decimal("1e14")
                        or number != number.quantize(Decimal("0.0001"))):
                    raise ValueError()
            except (InvalidOperation, ValueError):
                raise ValueError(f"规格数值非法: {key}") from None
            # 人工输入不受自动抽取的盘容量下限等召回规则限制；用 Decimal 校验单位换算。
            matched = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([A-Za-z/]*)", spec["value"].strip())
            factors = {
                "capacity": {"GB": "1", "TB": "1000", "MB": "0.001"},
                "rpm": {"K": "1000", "RPM": "1", "": "1"},
                "speed": {"GBPS": "1", "GB/S": "1"},
                "cache": {"MB": "1"}, "frequency": {"MHZ": "1", "": "1"},
            }.get(key, {})
            factor = factors.get(matched[2].upper()) if matched else None
            if factor is None or Decimal(matched[1]) * Decimal(factor) != number:
                raise ValueError(f"规格展示值与数值不一致: {key}")
        clean[key] = {"spec_value": spec["value"].strip(), "spec_unit": spec["unit"],
                      "numeric_value": number, "source": "manual"}
    return clean


def apply_manual_specs(db: Session, part_id: int, updates: dict,
                       *, catalog_record: bool = True) -> None:
    """父 PN 已由调用方锁定；描述 auto 刷新之后应用本次 manual，不提交。"""
    if not updates:
        return
    if catalog_record:
        require_authority_command(db)
    catalog_before = catalog.capture_state(db, part_id) if catalog_record else None
    touch_parts(db, [part_id])
    for key in sorted(updates):
        spec = updates[key]
        if spec is None:
            db.execute(delete(ProductSpec).where(ProductSpec.part_id == part_id,
                                                  ProductSpec.spec_key == key))
        else:
            db.execute(pg_insert(ProductSpec).values(part_id=part_id, spec_key=key, **spec)
                       .on_conflict_do_update(constraint="uq_spec_part_key", set_=spec))

    if catalog_record:
        catalog.record_legacy_change(
            db, part_id, catalog_before,
            context=catalog.legacy_context(entrypoint="maintenance_job"), action="specs.edit",
            deleted_spec_keys=[key for key, spec in updates.items() if spec is None],
            restored_spec_keys=[key for key, spec in updates.items() if spec is not None])


def backfill_specs(db: Session, rebuild: bool = False, *, catalog_record: bool = True) -> dict:
    """全量幂等回填：对每个 active 型号抽取规格，只补缺不覆盖。

    rebuild=True：先删全部 source='auto' 行再重插（=全量重建派生缓存），
    治历史陈旧；manual 行保留且撞键优先。
    """
    # Lock parents before child deletes/inserts; manual edits use the same order.
    # Include inactive parents when rebuilding, because old auto rows may remain there.
    require_authority_command(db)
    part_ids = list(db.scalars(select(DimPart.id).order_by(DimPart.id)))
    catalog_before = catalog.capture_states(db, part_ids) if catalog_record else {}
    blocked = catalog.blocked_spec_keys(db, part_ids)
    touched: set[int] = set()
    if rebuild:
        touched.update(db.execute(delete(ProductSpec).where(ProductSpec.source == "auto")
                                  .returning(ProductSpec.part_id)).scalars())
    parts = db.execute(
        select(DimPart.id, DimPart.description).where(
            DimPart.status == "active", DimPart.description.is_not(None))
    ).all()
    rows = []
    parts_hit = 0
    for pid, desc in parts:
        specs = [s for s in extract(desc) if s["spec_key"] not in blocked.get(pid, set())]
        if specs:
            parts_hit += 1
            rows.extend({**s, "part_id": pid, "source": "auto"} for s in specs)
    inserted = 0
    for i in range(0, len(rows), 1000):
        stmt = pg_insert(ProductSpec).values(rows[i:i + 1000]).on_conflict_do_nothing(
            constraint="uq_spec_part_key")
        hit = db.execute(stmt.returning(ProductSpec.part_id)).scalars().all()
        inserted += len(hit)
        touched.update(hit)
    # rebuild 删后重插内容相同也推进：只多一次增量重读，商城幂等跳过。
    touch_parts(db, touched)
    if catalog_record:
        catalog.record_legacy_changes(
            db, catalog_before, part_ids=touched,
            context=catalog.legacy_context(source="upstream", entrypoint="maintenance_job"),
            action="specs.backfill")
    return {"parts_scanned": len(parts), "parts_with_specs": parts_hit,
            "spec_rows": len(rows), "spec_inserted": inserted}
