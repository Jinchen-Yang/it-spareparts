"""单库、单次提交的机型清单命令。命令回执与归属在同一事务落账。"""

import hashlib
import json
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.model_parts import (
    MachineModel,
    MachineModelCommand,
    MachineModelMember,
    MachineModelName,
    MachineModelPending,
)
from app.schemas.model_parts import Create, Import, Remove, Resolve
from app.services import model_parts_catalog as catalog


def compact(value):
    return "".join(catalog.key(value).split())


def create_model(db: Session, body: Create):
    brand_key = compact(body.brand)
    if brand_key in {"h3c", "华三", "新华三"}:
        brand_key = "h3c"
    names = {}
    for value in [body.name, *body.aliases]:
        names.setdefault(compact(value), value)
    if any(len((brand_key + name).encode()) > 2048 for name in names):
        raise HTTPException(422, "规范化后的品牌或机型名称过长")
    existing = db.scalar(
        select(MachineModelName).where(
            MachineModelName.brand_key == brand_key, MachineModelName.name_key.in_(names)
        )
    )
    if existing:
        raise HTTPException(409, "该品牌下已有同名机型或搜索别名")
    model = MachineModel(brand=body.brand, name=body.name)
    db.add(model)
    db.flush()
    db.add_all(
        MachineModelName(
            model_id=model.id, brand_key=brand_key, name_key=key, name=name, is_primary=name == body.name
        )
        for key, name in names.items()
    )
    return {"modelId": str(model.id)}, True, {}


def members_for(db, model_id):
    rows = list(
        db.scalars(
            select(MachineModelMember)
            .where(MachineModelMember.model_id == model_id)
            .order_by(MachineModelMember.part_id)
        )
    )
    targets = catalog.canonical_ids(db, {r.part_id for r in rows})
    grouped = defaultdict(list)
    for row in rows:
        grouped[targets[row.part_id]].append(row)
    return grouped


def require_model(db, model_id):
    if db.get(MachineModel, int(model_id)) is None:
        raise HTTPException(404, "机型不存在，请刷新目录")


def import_rows(db, body: Import):
    mid = int(body.modelId)
    require_model(db, mid)
    existing = members_for(db, mid)
    confirmed = defaultdict(set)
    for pid, members in existing.items():
        for member in members:
            for raw in member.raw_pns:
                confirmed[catalog.key(raw)].add(pid)
    waiting = set(db.scalars(select(MachineModelPending.raw_key).where(MachineModelPending.model_id == mid)))
    active = [
        row
        for row in body.rows
        if not row.ignore
        and catalog.key(row.rawPn)
        and ("reviewDecision" not in row.model_fields_set or row.reviewDecision == "确认纳入")
    ]
    matches = catalog.lookup(db, [row.rawPn for row in active])
    added = pending = 0
    seen_parts, seen_raw = set(), set()
    for row in active:
        raw = catalog.key(row.rawPn)
        if len(raw.encode()) > 2048:
            raise HTTPException(422, f"第 {row.line} 行规范化后的原号过长")
        options = matches[raw]
        if {int(i) for i in row.candidateIds} != options:
            raise HTTPException(409, f"第 {row.line} 行的档案候选已变更，请重新预览")
        selected = int(row.partId) if row.partId else None
        # 人工核实过的现场原号只在此机型内去重；歧义号仍可显式选择另一有效档案。
        if raw in confirmed and (selected is None or selected in confirmed[raw]):
            continue
        if selected is not None and selected not in options:
            raise HTTPException(422, f"第 {row.line} 行所选 PN 不在经过核对的候选中")
        if raw in waiting:
            continue
        target = selected
        if target is None and len(options) == 1:
            target = next(iter(options))
        if target is not None:
            if target in existing or target in seen_parts:
                continue
            seen_parts.add(target)
        elif raw in seen_raw:
            continue
        seen_raw.add(raw)
        fields = dict(model_id=mid, position=row.position, condition=row.condition, source=row.source)
        if target is None:
            db.add(
                MachineModelPending(
                    **fields,
                    raw_key=raw,
                    raw_pn=row.rawPn,
                    candidate_ids=sorted(options),
                    reason="ambiguous" if options else "unknown",
                )
            )
            pending += 1
        else:
            db.add(MachineModelMember(**fields, part_id=target, raw_pns=[row.rawPn]))
            added += 1
    return (
        {"added": added, "pending": pending, "skipped": len(body.rows) - added - pending},
        bool(added or pending),
        {},
    )


def resolve_pending(db, body: Resolve):
    row = db.get(MachineModelPending, int(body.pendingId))
    if row is None:
        raise HTTPException(409, "待核记录已处理，请刷新")
    part_id = int(body.partId)
    part = db.get(DimPart, part_id)
    if part is None or part.status != "active":
        raise HTTPException(409, "所选备件档案已变更，请重新核对")
    allowed = catalog.lookup(db, [row.raw_pn])[catalog.key(row.raw_pn)]
    if allowed and part_id not in allowed:
        raise HTTPException(409, "请从当前匹配的备件档案中核对")
    before = {"pending": catalog.pending_view(row, row.candidate_ids)}
    # 人工核对明确指向所选规范身份。即使另一条旧归属已合并到它，新增证据
    # 也要挂当前 ID；将来撤销主库合并时，两次独立确认各自保留。
    link = db.get(MachineModelMember, (row.model_id, part_id))
    evidence = catalog.joined(row.source, body.source)
    if link is not None:
        before["member"] = catalog.member_view(link, part_id)
        if catalog.key(row.raw_pn) not in {catalog.key(raw) for raw in link.raw_pns}:
            link.raw_pns = [*link.raw_pns, row.raw_pn]
        link.source = catalog.joined(link.source, evidence)
        link.position = catalog.joined(link.position, row.position)
        link.condition = catalog.joined(link.condition, row.condition)
    else:
        db.add(
            MachineModelMember(
                model_id=row.model_id,
                part_id=part_id,
                position=row.position,
                condition=row.condition,
                source=evidence,
                raw_pns=[row.raw_pn],
            )
        )
    db.delete(row)
    return {}, True, before


def remove_member(db, body: Remove):
    require_model(db, body.modelId)
    target = int(body.partId)
    rows = members_for(db, int(body.modelId)).get(target, ())
    if (
        not rows
        or catalog.membership_version(int(body.modelId), target, (row.part_id for row in rows))
        != body.expectedMembershipVersion
    ):
        raise HTTPException(409, "归属或主库身份已变更，请刷新清单后重新确认")
    before = {"members": [catalog.member_view(row, row.part_id) for row in rows]}
    for row in rows:
        db.delete(row)
    return {}, True, before


def execute(db: Session, actor: str, body) -> dict:
    # 模块使用一份全局清单版本。序列化维护写入，读请求在可重复读快照中不持此锁。
    db.execute(select(func.pg_advisory_xact_lock(0x4D504F4F, 76)))
    # 表中明确提供空的核对结论，与未提供该列的普通 PN 清单具有不同语义。
    payload = body.model_dump(mode="json", exclude_unset=True)
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    receipt = db.get(MachineModelCommand, body.requestId)
    if receipt:
        if receipt.actor != actor or receipt.fingerprint != fingerprint:
            raise HTTPException(409, "该请求编号已用于其他操作，请重新预览")
        return dict(receipt.result)
    old = catalog.revision(db)
    if old != body.expectedRevision:
        raise HTTPException(409, "机型目录已变更，请刷新后重新预览")
    handlers = {
        "create": create_model,
        "import": import_rows,
        "resolve": resolve_pending,
        "remove": remove_member,
    }
    fields, changed, before = handlers[body.action](db, body)
    new = old + int(changed)
    result = {"revision": new, **fields}
    db.add(
        MachineModelCommand(
            request_id=body.requestId,
            actor=actor,
            revision=new,
            fingerprint=fingerprint,
            action=body.action,
            command=payload,
            before=before,
            result=result,
        )
    )
    db.commit()
    return result
