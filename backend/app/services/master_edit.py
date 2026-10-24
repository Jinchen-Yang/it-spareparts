"""备件主数据自治（WP1）：采购可新建 / 编辑型号。

设计要点（对应甲方 2026-06-30 "和氚云无 API、把服务器 PN 做完善"）：
- 人工维护过的字段写进 dim_part.locked_fields → loader 重导一律不覆盖（防覆盖在 ETL 层兜底）。
- 手录 pn_std 按所填为准（大写+去空白），**不跑标准化策略B去 V 码**——人工值即权威。
- 新建/编辑都落 sys_audit_log；operated_by=真实用户名（不是角色，避开 S-2 记成 'admin' 的坑）。
- 新建前查近似重复（compact 精确撞 + resolver 近似召回），默认拦截、force 可强建。
- 改后搜索即时生效：search_doc 是 STORED 生成列，库自动重算，无需手动重建索引。
"""
import re
import hashlib
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.master_data import ProductSpec
from app.models.system import SysAuditLog
from app.services import master_data, part_resolver, spec_extract

# 采购可人工维护的字段（编辑这些 → 写进 locked_fields，重导不覆盖）
EDITABLE_FIELDS = ("description", "brand", "manufacturer", "summary",
                   "category_major", "category_minor", "machine_or_part", "unit")
CATEGORY_FIELDS = {"category_major", "category_minor"}


class MasterEditError(Exception):
    """主数据编辑非法（重名 / 对墓碑行操作 / 空 PN 等）。"""


class MasterEditConflict(MasterEditError):
    def __init__(self, code: str, message: str, conflicts: list[dict]):
        super().__init__(message)
        self.detail = {"code": code, "message": message, "conflicts": conflicts}


def _edit_state(db: Session, part: DimPart) -> dict:
    specs = db.execute(select(
        ProductSpec.spec_key, ProductSpec.spec_value, ProductSpec.spec_unit,
        ProductSpec.numeric_value, ProductSpec.source
    ).where(ProductSpec.part_id == part.id).order_by(ProductSpec.spec_key)).all()
    return {
        "version_mode": "field_snapshot",  # M2 前的现值基线，不能作为单调版本或发布水位。
        # 对外集成用的不透明乐观并发版本（updated_at ISO）；与 version_mode 无关。
        "version": part.updated_at.isoformat() if part.updated_at else None,
        "part_id": part.id, "pn_std": part.pn_std,
        "identity": {"part_id": part.id, "pn_std": part.pn_std,
                     "status": part.status, "merged_into_id": part.merged_into_id},
        "fields": {k: getattr(part, k) for k in EDITABLE_FIELDS},
        "specs": {key: {"value": value, "unit": unit,
                         "numeric_value": format(number.normalize(), "f") if number is not None else None,
                         "source": source} for key, value, unit, number, source in specs},
    }


def get_part_edit_state(db: Session, part_id: int) -> dict | None:
    # 鉴权可能已开启请求事务；独立短只读快照不改变调用方事务，也不阻挡写者。
    with Session(bind=db.get_bind()) as reader:
        reader.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        reader.execute(text("SET TRANSACTION READ ONLY"))
        part = reader.scalar(select(DimPart).where(DimPart.id == part_id))
        return _edit_state(reader, part) if part is not None else None


def _audit_state(db: Session, part: DimPart) -> dict:
    return {**_edit_state(db, part), "brand_id": part.brand_id,
            "category_id": part.category_id, "category_source": part.category_source,
            "locked_fields": list(part.locked_fields or []), "master_source": part.master_source,
            "reviewed_at": part.reviewed_at.isoformat() if part.reviewed_at else None}


def _check_baseline(current: dict, baseline: dict, updates: dict, spec_updates: dict,
                    manual_specs: dict) -> None:
    if (baseline.get("version_mode") != "field_snapshot"
            or not isinstance(baseline.get("fields"), dict)
            or not isinstance(baseline.get("specs"), dict)):
        raise MasterEditError("缺少完整编辑基线，请重新打开编辑器")
    if baseline.get("part_id") != current["part_id"] or baseline.get("identity") != current["identity"]:
        raise MasterEditConflict("identity_conflict", "型号身份已变化，请重新打开编辑器", [{
            "field": "identity", "baseline": baseline.get("identity"),
            "current": current["identity"], "proposed": None}])
    conflicts = []
    checked_updates = dict(updates)
    if CATEGORY_FIELDS.intersection(updates):
        # 两级品类是一个人工选择；另一列也必须仍等于打开编辑器时的基线。
        for key in CATEGORY_FIELDS:
            if key not in baseline["fields"]:
                raise MasterEditError(f"缺少字段基线: {key}")
            checked_updates.setdefault(key, baseline["fields"][key])
    for key, proposed in checked_updates.items():
        if key not in baseline["fields"]:
            raise MasterEditError(f"缺少字段基线: {key}")
        if (baseline["fields"][key] != current["fields"][key]
                and proposed != current["fields"][key]):
            conflicts.append({"field": key, "baseline": baseline["fields"][key],
                              "current": current["fields"][key], "proposed": proposed})
    for key, proposed in spec_updates.items():
        spec = manual_specs[key]
        effective = None if spec is None else {
            "value": spec["spec_value"], "unit": spec["spec_unit"],
            "numeric_value": (format(spec["numeric_value"].normalize(), "f")
                              if spec["numeric_value"] is not None else None),
        }
        current_spec = current["specs"].get(key)
        # source 是人工确认后的落地属性；同值 auto 也允许被本次确认成 manual。
        current_value = ({k: v for k, v in current_spec.items() if k != "source"}
                         if current_spec is not None else None)
        if (baseline["specs"].get(key) != current["specs"].get(key)
                and effective != current_value):
            conflicts.append({"field": f"specs.{key}", "baseline": baseline["specs"].get(key),
                              "current": current["specs"].get(key), "proposed": proposed})
    if conflicts:
        raise MasterEditConflict("field_conflict", "所选字段已变化，草稿已保留，请重新核对", conflicts)


def list_part_edit_history(db: Session, part_id: int, *, limit: int = 20,
                           before_id: int | None = None) -> dict:
    """仅人工审计记录；旧记录可能稀疏，不声称全路径版本或提供历史恢复。"""
    query = select(SysAuditLog).where(SysAuditLog.entity_type == "part",
                                     SysAuditLog.entity_id == part_id,
                                     SysAuditLog.action.in_(["create", "edit"]))
    if before_id is not None:
        query = query.where(SysAuditLog.id < before_id)
    rows = db.scalars(query.order_by(SysAuditLog.id.desc()).limit(limit + 1)).all()
    return {"history_basis": "manual_audit", "items": [
        {"audit_id": row.id, "action": row.action, "before": row.before_json,
         "after": row.after_json, "operated_by": row.operated_by,
         "operated_at": row.operated_at.isoformat()} for row in rows[:limit]],
        "next_before_id": rows[limit - 1].id if len(rows) > limit else None}


def _compact(s: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_fields(fields: dict) -> dict:
    if set(fields) - set(EDITABLE_FIELDS):
        raise MasterEditError("包含不可编辑字段")
    limits = {"brand": 128, "manufacturer": 128, "category_major": 64,
              "category_minor": 128, "machine_or_part": 16, "unit": 16}
    clean = {}
    for key, value in fields.items():
        if value is not None and not isinstance(value, str):
            raise MasterEditError(f"字段必须是文本: {key}")
        value = value.strip() or None if isinstance(value, str) else value
        if value is not None and key in limits and len(value) > limits[key]:
            raise MasterEditError(f"字段过长: {key}")
        clean[key] = value
    return clean


def find_near_duplicates(db: Session, pn_std: str, exclude_id: int | None = None,
                         limit: int = 5) -> list[dict]:
    """近似重复提示：① compact 归一后完全相同（强信号）② resolver 近似召回。"""
    out: list[dict] = []
    compact = _compact(pn_std)
    if compact:
        rows = db.execute(
            select(DimPart.id, DimPart.pn_std, DimPart.description)
            .where(DimPart.pn_compact == compact, DimPart.status == "active")
        ).all()
        for rid, pn, desc in rows:
            if rid != exclude_id:
                out.append({"pn_std": pn, "description": desc, "reason": "归一化后完全相同"})
    if out:
        return out[:limit]
    try:  # 无精确撞 → resolver 近似（容错连字符 / 中英品牌混写 / 历史别名）
        # log_miss=False：查重不写 search_miss 审计、不在本事务中途 commit
        res = part_resolver.resolve(db, pn_std, limit=limit, log_miss=False)
        for it in res.get("items", []):
            if (it.get("pn_std") and _compact(it["pn_std"]) != compact
                    and (it.get("score") or 0) >= 0.6):
                out.append({"pn_std": it["pn_std"], "description": it.get("description"),
                            "reason": f"近似(匹配度 {round(it['score'], 2)})"})
    except Exception:  # noqa: BLE001  resolver 异常不应挡住新建
        pass
    return out[:limit]


def create_part_in_transaction(db: Session, *, pn_std: str, operated_by: str | None = None,
                               force: bool = False, spec_updates: dict | None = None,
                               master_source: str = "manual", **fields) -> dict:
    """采购手工新建型号。fields ⊆ EDITABLE_FIELDS（machine_or_part 缺省 '备件'）。

    硬重名（同 pn_std 已存在，含墓碑）→ 报错引导改用编辑；近似重复 → 默认返回候选不建，
    force=True 跳过近似仅挡硬重名。所填字段进 locked_fields，master_source 缺省 manual
    （shop 服务建档传 shop），reviewed_at=now（人工建档不进"待复核"）。
    """
    pn = (pn_std or "").strip().upper()
    if not pn:
        raise MasterEditError("PN 不能为空")
    if len(pn) > 128:
        raise MasterEditError("PN 不能超过 128 个字符")
    if master_source not in ("manual", "shop"):
        raise MasterEditError(f"建档来源非法: {master_source}")
    clean = _clean_fields(fields)
    try:
        manual_specs = spec_extract.validate_manual_specs(spec_updates or {})
    except ValueError as exc:
        raise MasterEditError(str(exc)) from exc
    master_data.acquire_master_write_lock(db)
    # 无父 ID 时以规范输入键协调本人工入口；唯一约束仍兜其他入口的竞态。
    lock_key = int.from_bytes(hashlib.sha256(f"master-pn:{pn}".encode()).digest()[:8],
                              "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    if db.scalar(select(DimPart.id).where(DimPart.pn_std == pn)) is not None:
        raise MasterEditError(f"型号已存在: {pn}（请改用编辑）")

    clean.setdefault("machine_or_part", "备件")

    near = find_near_duplicates(db, pn)
    if near and not force:
        return {"created": False, "near_duplicates": near,
                "message": "存在近似型号，确认无重复后可强制新建(force=true)"}

    locked = sorted(k for k, v in clean.items() if v is not None)
    part = DimPart(pn_std=pn, master_source=master_source, reviewed_at=_now(),
                   locked_fields=locked,
                   category_source=("MANUAL" if clean.get("category_major") else None),
                   **clean)
    try:
        with db.begin_nested():
            db.add(part)
            db.flush()
    except IntegrityError as exc:
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) != "dim_part_pn_std_key":
            raise
        raise MasterEditError(f"型号已存在: {pn}（请改用编辑）") from exc
    master_data.bind_part_dictionaries(db, part, clean)
    spec_extract.refresh_part_specs(db, part.id, part.description)
    spec_extract.apply_manual_specs(db, part.id, manual_specs)
    audit = SysAuditLog(entity_type="part", entity_id=part.id, action="create",
                       before_json=None,
                       after_json={"pn_std": pn, "master_source": master_source, **clean,
                                   "_snapshot": _audit_state(db, part)},
                       reason=("商城服务新建" if master_source == "shop"
                               else "采购手工新建"),
                       operated_by=operated_by)
    db.add(audit)
    db.flush()
    return {"created": True, "id": part.id, "pn_std": pn, "near_duplicates": near,
            "audit_id": audit.id, "edit_state": _edit_state(db, part)}


def create_part(db: Session, **kwargs) -> dict:
    """旧人工入口的最终提交点；组合命令请用事务内方法。"""
    try:
        result = create_part_in_transaction(db, **kwargs)
        db.commit()
        return result
    except DBAPIError as exc:
        db.rollback()
        _raise_concurrent_write(exc)
        raise
    except Exception:
        db.rollback()
        raise


def lock_part(db: Session, *, part_id: int | None = None,
              pn_std: str | None = None) -> DimPart | None:
    """先锁父 PN 并重读已驻留 ORM 实体。调用方不得提前修改该实体。"""
    condition = DimPart.id == part_id if part_id is not None else DimPart.pn_std == pn_std
    with db.no_autoflush:
        master_data.acquire_master_write_lock(db)
        return db.scalar(select(DimPart).where(condition).with_for_update()
                         .execution_options(populate_existing=True))


def edit_part_in_transaction(db: Session, *, pn_std: str, updates: dict,
                              part_id: int | None = None,
                              base_snapshot: dict | None = None,
                              spec_updates: dict | None = None,
                              operated_by: str | None = None) -> dict | None:
    """编辑任意型号的人工字段。被改字段加入 locked_fields（重导不覆盖）。"""
    part = lock_part(db, part_id=part_id, pn_std=pn_std)
    if part is None:
        return None
    clean = _clean_fields(updates)
    try:
        manual_specs = spec_extract.validate_manual_specs(spec_updates or {})
    except ValueError as exc:
        raise MasterEditError(str(exc)) from exc
    current = _edit_state(db, part)
    if base_snapshot is not None:
        _check_baseline(current, base_snapshot, clean, spec_updates or {}, manual_specs)
    if part.status == "merged" or part.pn_std != pn_std:
        if base_snapshot is not None:
            raise MasterEditConflict("identity_conflict", "型号身份已变化，请重新打开编辑器", [])
        raise MasterEditError(f"型号 {pn_std} 已合并入他档，请对目标型号操作")
    if not clean and not manual_specs:
        raise MasterEditError("没有可编辑的字段")

    before = {k: getattr(part, k) for k in clean}
    snapshot_before = _audit_state(db, part)
    locked = set(part.locked_fields or [])
    for k, v in clean.items():
        val = v.strip() if isinstance(v, str) else v
        setattr(part, k, val or None if isinstance(val, str) else val)
        locked.add(k)
    if CATEGORY_FIELDS.intersection(clean):
        locked.update(CATEGORY_FIELDS)
        part.category_source = "MANUAL"
    part.locked_fields = sorted(locked)
    part.reviewed_at = _now()
    master_data.bind_part_dictionaries(db, part, clean)
    db.flush()
    # 描述变更 → 同事务重抽派生规格缓存（防 specs 陈旧：删 auto 行重插，manual 优先）
    if "description" in clean and part.description != before["description"]:
        spec_extract.refresh_part_specs(db, part.id, part.description)
    spec_extract.apply_manual_specs(db, part.id, manual_specs)
    audit = SysAuditLog(entity_type="part", entity_id=part.id, action="edit",
                       before_json={**before, "_snapshot": snapshot_before},
                       after_json={**clean, "_snapshot": _audit_state(db, part)},
                       reason="采购编辑主数据", operated_by=operated_by)
    db.add(audit)
    db.flush()
    return {"id": part.id, "pn_std": part.pn_std,
            "updated": [*clean, *(f"specs.{key}" for key in manual_specs)],
            "locked_fields": part.locked_fields, "audit_id": audit.id,
            "edit_state": _edit_state(db, part)}


def edit_part(db: Session, **kwargs) -> dict | None:
    """旧内部调用的最终提交点；浏览器必须经 HTTP 基线校验。"""
    try:
        result = edit_part_in_transaction(db, **kwargs)
        db.commit()
        return result
    except DBAPIError as exc:
        db.rollback()
        _raise_concurrent_write(exc)
        raise
    except Exception:
        db.rollback()
        raise


def _raise_concurrent_write(exc: DBAPIError) -> None:
    if getattr(exc.orig, "sqlstate", None) == "40P01":
        raise MasterEditConflict(
            "concurrent_write", "主数据库正在并发更新，本次修改未生效，请保留草稿后重试", []
        ) from exc
