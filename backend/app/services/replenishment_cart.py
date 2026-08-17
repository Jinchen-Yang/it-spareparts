"""Cloud cart persistence and atomic hand-off to replenishment submission."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dimensions import DimPart
from app.models.replenishment import ReplenishmentCartDraft, ReplenishmentCartDraftLine
from app.models.system import SysUser
from app.services import replenishment


def _uid() -> str:
    return str(uuid.uuid4())


def _owner(db: Session, username: str) -> SysUser:
    user = db.scalar(select(SysUser).where(SysUser.username == username, SysUser.is_active.is_(True)))
    if user is None:
        raise replenishment.ReplenishmentError("实名账号不存在或已停用", code="identity_required", status_code=403)
    return user


def _payload(db: Session, draft: ReplenishmentCartDraft | None) -> dict | None:
    if draft is None:
        return None
    rows = db.execute(
        select(ReplenishmentCartDraftLine, DimPart)
        .join(DimPart, DimPart.id == ReplenishmentCartDraftLine.part_id)
        .where(ReplenishmentCartDraftLine.draft_id == draft.draft_id)
        .order_by(ReplenishmentCartDraftLine.line_no)
    ).all()
    return {
        "draft_id": draft.draft_id,
        "project_id": draft.project_id,
        "request_note": draft.request_note,
        "client_request_id": draft.client_request_id,
        "version": draft.version,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
        "lines": [
            {
                "draft_line_id": line.draft_line_id,
                "line_no": line.line_no,
                "part_id": part.id,
                "pn_std": part.pn_std,
                "description": part.description,
                "brand": part.brand,
                "unit": part.unit,
                "quantity": line.quantity,
                "special_note": line.special_note,
            }
            for line, part in rows
        ],
    }


def get_cart_draft(db: Session, *, username: str, project_id: str) -> dict | None:
    user = _owner(db, username)
    draft = db.scalar(select(ReplenishmentCartDraft).where(
        ReplenishmentCartDraft.owner_user_id == user.id,
        ReplenishmentCartDraft.project_id == project_id,
    ))
    return _payload(db, draft)


def replace_cart_draft(
    db: Session,
    *,
    username: str,
    role: str,
    project_id: str,
    expected_version: int | None,
    request_note: str | None,
    lines: list[dict],
) -> dict:
    user = _owner(db, username)
    project = replenishment._authorized_project(db, project_id=project_id, user=user, role=role)
    if not lines or len(lines) > replenishment.MAX_LINES:
        raise replenishment.ReplenishmentError("购物车明细必须为 1-200 条")
    cleaned: list[dict] = []
    part_ids: list[int] = []
    for item in lines:
        part_id = int(item["part_id"])
        quantity = replenishment._integer_quantity(item["quantity"])
        if quantity > 999999:
            raise replenishment.ReplenishmentError("数量必须为 1-999999")
        part_ids.append(part_id)
        cleaned.append({
            "part_id": part_id,
            "quantity": int(quantity),
            "special_note": replenishment._clean_optional(item.get("special_note"), maximum=4000),
        })
    if len(part_ids) != len(set(part_ids)):
        raise replenishment.ReplenishmentError("同一 PN 只能出现一次", code="duplicate_part", status_code=409)
    parts = list(db.scalars(select(DimPart).where(
        DimPart.id.in_(part_ids), DimPart.status == "active", DimPart.is_excluded.is_(False)
    )))
    if len(parts) != len(part_ids):
        raise replenishment.ReplenishmentError("购物车包含不存在、已合并或已排除的 PN", code="part_unavailable", status_code=422)
    draft = db.scalar(select(ReplenishmentCartDraft).where(
        ReplenishmentCartDraft.owner_user_id == user.id,
        ReplenishmentCartDraft.project_id == project.project_id,
    ).with_for_update())
    if draft is None:
        if expected_version not in (None, 0):
            raise replenishment.ReplenishmentError("购物车版本已变化，请重新加载", code="version_conflict", status_code=409)
        draft = ReplenishmentCartDraft(
            draft_id=_uid(), owner_user_id=user.id, project_id=project.project_id,
            client_request_id=_uid(), version=1,
        )
        db.add(draft)
        db.flush()
    elif expected_version is not None and draft.version != expected_version:
        raise replenishment.ReplenishmentError("草稿已在其他页面更新，请重新加载", code="version_conflict", status_code=409)
    elif expected_version is not None:
        draft.version += 1
    draft.request_note = replenishment._clean_optional(request_note, maximum=4000)
    db.query(ReplenishmentCartDraftLine).filter(
        ReplenishmentCartDraftLine.draft_id == draft.draft_id
    ).delete(synchronize_session=False)
    for line_no, item in enumerate(cleaned, 1):
        db.add(ReplenishmentCartDraftLine(
            draft_line_id=_uid(), draft_id=draft.draft_id, line_no=line_no,
            part_id=item["part_id"], quantity=item["quantity"], special_note=item["special_note"],
        ))
    db.flush()
    result = _payload(db, draft) or {}
    db.commit()
    return result


def delete_cart_draft(
    db: Session, *, username: str, project_id: str, expected_version: int | None = None
) -> bool:
    user = _owner(db, username)
    draft = db.scalar(select(ReplenishmentCartDraft).where(
        ReplenishmentCartDraft.owner_user_id == user.id,
        ReplenishmentCartDraft.project_id == project_id,
    ).with_for_update())
    if draft is None:
        return False
    if expected_version is not None and draft.version != expected_version:
        raise replenishment.ReplenishmentError("草稿已在其他页面更新，请重新加载", code="version_conflict", status_code=409)
    db.delete(draft)
    db.commit()
    return True


def submit_cart_draft_atomic(
    db: Session, *, username: str, role: str, project_id: str, expected_version: int
) -> dict:
    user = _owner(db, username)
    draft = db.scalar(select(ReplenishmentCartDraft).where(
        ReplenishmentCartDraft.owner_user_id == user.id,
        ReplenishmentCartDraft.project_id == project_id,
    ).with_for_update())
    if draft is None:
        raise replenishment.ReplenishmentError("购物车不存在", code="cart_not_found", status_code=404)
    if draft.version != expected_version:
        raise replenishment.ReplenishmentError("草稿已在其他页面更新，请重新加载", code="version_conflict", status_code=409)
    lines = list(db.scalars(select(ReplenishmentCartDraftLine).where(
        ReplenishmentCartDraftLine.draft_id == draft.draft_id
    ).order_by(ReplenishmentCartDraftLine.line_no)))
    if not lines:
        raise replenishment.ReplenishmentError("购物车为空，不能提交", code="empty_cart", status_code=422)
    result = replenishment.submit_application_atomic(
        db, username=username, role=role, client_request_id=draft.client_request_id,
        project_id=project_id, request_note=draft.request_note,
        lines=[{"part_id": line.part_id, "quantity": line.quantity, "special_note": line.special_note} for line in lines],
        commit=False,
    )
    db.delete(draft)
    db.commit()
    return result
