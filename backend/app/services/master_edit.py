"""备件主数据自治（WP1）：采购可新建 / 编辑型号。

设计要点（对应甲方 2026-06-30 "和氚云无 API、把服务器 PN 做完善"）：
- 人工维护过的字段写进 dim_part.locked_fields → loader 重导一律不覆盖（防覆盖在 ETL 层兜底）。
- 手录 pn_std 按所填为准（大写+去空白），**不跑标准化策略B去 V 码**——人工值即权威。
- 新建/编辑都落 sys_audit_log；operated_by=真实用户名（不是角色，避开 S-2 记成 'admin' 的坑）。
- 新建前查近似重复（compact 精确撞 + resolver 近似召回），默认拦截、force 可强建。
- 改后搜索即时生效：search_doc 是 STORED 生成列，库自动重算，无需手动重建索引。
"""
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.system import SysAuditLog
from app.services import part_resolver

# 采购可人工维护的字段（编辑这些 → 写进 locked_fields，重导不覆盖）
EDITABLE_FIELDS = ("description", "brand", "category_major", "category_minor",
                   "machine_or_part", "unit")


class MasterEditError(Exception):
    """主数据编辑非法（重名 / 对墓碑行操作 / 空 PN 等）。"""


def _compact(s: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def create_part(db: Session, *, pn_std: str, operated_by: str | None = None,
                force: bool = False, **fields) -> dict:
    """采购手工新建型号。fields ⊆ EDITABLE_FIELDS（machine_or_part 缺省 '备件'）。

    硬重名（同 pn_std 已存在，含墓碑）→ 报错引导改用编辑；近似重复 → 默认返回候选不建，
    force=True 跳过近似仅挡硬重名。所填字段进 locked_fields，master_source=manual，
    reviewed_at=now（人工建档不进"待复核"）。
    """
    pn = (pn_std or "").strip().upper()
    if not pn:
        raise MasterEditError("PN 不能为空")
    if db.scalar(select(DimPart.id).where(DimPart.pn_std == pn)) is not None:
        raise MasterEditError(f"型号已存在: {pn}（请改用编辑）")

    clean = {k: (v.strip() if isinstance(v, str) else v)
             for k, v in fields.items() if k in EDITABLE_FIELDS}
    clean = {k: (v or None) for k, v in clean.items()}
    clean.setdefault("machine_or_part", "备件")

    near = find_near_duplicates(db, pn)
    if near and not force:
        return {"created": False, "near_duplicates": near,
                "message": "存在近似型号，确认无重复后可强制新建(force=true)"}

    locked = sorted(k for k, v in clean.items() if v is not None)
    part = DimPart(pn_std=pn, master_source="manual", reviewed_at=_now(),
                   locked_fields=locked,
                   category_source=("MANUAL" if clean.get("category_major") else None),
                   **clean)
    db.add(part)
    try:
        db.flush()   # 兜并发：pn_std 唯一约束把 check-then-insert 竞态收敛成友好 400
    except IntegrityError as exc:
        db.rollback()
        raise MasterEditError(f"型号已存在: {pn}（请改用编辑）") from exc
    db.add(SysAuditLog(entity_type="part", entity_id=part.id, action="create",
                       before_json=None,
                       after_json={"pn_std": pn, "master_source": "manual", **clean},
                       reason="采购手工新建", operated_by=operated_by))
    db.commit()
    return {"created": True, "id": part.id, "pn_std": pn, "near_duplicates": near}


def edit_part(db: Session, *, pn_std: str, updates: dict,
              operated_by: str | None = None) -> dict | None:
    """编辑任意型号的人工字段。被改字段加入 locked_fields（重导不覆盖）。"""
    part = db.scalar(select(DimPart).where(DimPart.pn_std == pn_std))
    if part is None:
        return None
    if part.status == "merged":
        raise MasterEditError(f"型号 {pn_std} 已合并入他档，请对目标型号操作")
    clean = {k: v for k, v in updates.items() if k in EDITABLE_FIELDS}
    if not clean:
        raise MasterEditError("没有可编辑的字段")

    before = {k: getattr(part, k) for k in clean}
    locked = set(part.locked_fields or [])
    for k, v in clean.items():
        val = v.strip() if isinstance(v, str) else v
        setattr(part, k, val or None if isinstance(val, str) else val)
        locked.add(k)
        if k == "category_major":
            part.category_source = "MANUAL"
    part.locked_fields = sorted(locked)
    part.reviewed_at = _now()
    db.flush()
    db.add(SysAuditLog(entity_type="part", entity_id=part.id, action="edit",
                       before_json=before, after_json=clean,
                       reason="采购编辑主数据", operated_by=operated_by))
    db.commit()
    return {"id": part.id, "pn_std": part.pn_std, "updated": list(clean.keys()),
            "locked_fields": part.locked_fields}
