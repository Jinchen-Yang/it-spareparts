"""字段池接口：个人归属与团队权限在此把关，字段语义交给 M7。"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.audience import audience_user, service_error
from app.db import get_db
from app.models.fields_audience import AudienceRule, AudienceRuleMember, FieldDef
from app.security import UserContext, record_access_log
from app.services import audience, field_pool

router = APIRouter(prefix="/fields", tags=["fields"])

# 新业务接入前需显式注册；未知 subject_type 的团队池写入失败关闭。
TEAM_FIELD_PERMISSION = {"demo": "action_account_manage"}


class AudienceRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["public", "private", "include", "exclude"] = "public"
    user_ids: list[str] = Field(default_factory=list)
    group_ids: list[int] = Field(default_factory=list)


class DefineField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: str = Field(min_length=1, max_length=64)
    level: Literal["header", "line"]
    name: str = Field(min_length=1, max_length=32)
    owner_scope: Literal["personal", "team"] = "personal"
    value_type: Literal["text", "number", "date", "bool", "choice"] = "text"
    choices: list[str] | None = None
    default_rule: AudienceRuleInput | None = None
    auto_include: bool = True
    sort: int = 0


class UpdateField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=32)
    value_type: Literal["text", "number", "date", "bool", "choice"] | None = None
    choices: list[str] | None = None
    default_rule: AudienceRuleInput | None = None
    auto_include: bool | None = None
    sort: int | None = None
    archived: bool | None = None


def _require_team_permission(subject_type: str, viewer: UserContext) -> None:
    key = TEAM_FIELD_PERMISSION.get(subject_type)
    if key is None or not (viewer.permissions or {}).get(key):
        raise HTTPException(403, "无权管理该类型的团队字段池")


def _field_views(db: Session, defs: list[FieldDef]) -> list[dict]:
    rule_ids = {row.default_rule_id for row in defs if row.default_rule_id is not None}
    rules: dict[int, dict] = {}
    if rule_ids:
        for rule in db.scalars(select(AudienceRule).where(AudienceRule.id.in_(rule_ids))):
            rules[rule.id] = {"mode": rule.mode, "user_ids": [], "group_ids": []}
        for member in db.scalars(
            select(AudienceRuleMember).where(AudienceRuleMember.rule_id.in_(rule_ids))
        ):
            if member.user_id is not None:
                rules[member.rule_id]["user_ids"].append(member.user_id)
            elif member.group_id is not None:
                rules[member.rule_id]["group_ids"].append(member.group_id)
        for rule in rules.values():
            rule["user_ids"].sort()
            rule["group_ids"].sort()
    return [
        {
            "id": row.id,
            "owner_scope": row.owner_scope,
            "owner_id": row.owner_id,
            "subject_type": row.subject_type,
            "level": row.level,
            "name": row.name,
            "value_type": row.value_type,
            "choices": row.choices,
            "default_rule_id": row.default_rule_id,
            "default_rule": rules.get(row.default_rule_id),
            "auto_include": row.auto_include,
            "sort": row.sort,
            "archived": row.archived,
            "created_at": row.created_at,
        }
        for row in defs
    ]


def _new_rule(db: Session, rule: AudienceRuleInput | None, viewer: UserContext) -> int | None:
    if rule is None:
        return None
    return audience.create_rule(db, rule.mode, rule.user_ids, rule.group_ids, viewer)


@router.get("/defs")
def list_defs(
    subject_type: str = Query(min_length=1, max_length=64),
    level: Literal["header", "line"] = Query(),
    db: Session = Depends(get_db),
    viewer: UserContext = Depends(audience_user),
) -> list[dict]:
    try:
        return _field_views(db, field_pool.list_defs(db, subject_type, level, viewer))
    except (ValueError, PermissionError) as exc:
        service_error(db, exc)


@router.get("/template")
def template_for(
    subject_type: str = Query(min_length=1, max_length=64),
    level: Literal["header", "line"] = Query(),
    anchor: str | None = Query(default=None, min_length=1, max_length=64),
    db: Session = Depends(get_db),
    viewer: UserContext = Depends(audience_user),
) -> list[dict]:
    try:
        return _field_views(db, field_pool.template_for(db, subject_type, level, viewer, anchor=anchor))
    except (ValueError, PermissionError) as exc:
        service_error(db, exc)


@router.get("/name-suggestions")
def name_suggestions(
    subject_type: str = Query(min_length=1, max_length=64),
    q: str = Query(default="", max_length=32),
    db: Session = Depends(get_db),
    viewer: UserContext = Depends(audience_user),
) -> list[dict]:
    try:
        return field_pool.name_suggestions(db, subject_type, q, viewer)
    except (ValueError, PermissionError) as exc:
        service_error(db, exc)


@router.post("/defs", status_code=201)
def define_field(
    body: DefineField,
    db: Session = Depends(get_db),
    viewer: UserContext = Depends(audience_user),
) -> dict:
    if body.owner_scope == "team":
        _require_team_permission(body.subject_type, viewer)
    try:
        values = body.model_dump(exclude={"default_rule"})
        row = field_pool.define_field(db, **values, viewer=viewer)
        if body.default_rule is not None:
            row = field_pool.update_field(
                db, row, default_rule_id=_new_rule(db, body.default_rule, viewer)
            )
        db.commit()
    except (ValueError, PermissionError) as exc:
        service_error(db, exc)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "同名字段已存在") from exc
    record_access_log(viewer, "field_def_create", "field_def", {"id": row.id})
    return _field_views(db, [row])[0]


@router.patch("/defs/{field_id}")
def update_field(
    field_id: int,
    body: UpdateField,
    db: Session = Depends(get_db),
    viewer: UserContext = Depends(audience_user),
) -> dict:
    row = db.get(FieldDef, field_id)
    if row is None:
        raise HTTPException(404, "字段不存在")
    if row.owner_scope == "team":
        _require_team_permission(row.subject_type, viewer)
    elif row.owner_id != viewer.user_id:
        raise HTTPException(403, "只能修改自己的个人字段")
    values = body.model_dump(exclude_unset=True, exclude={"default_rule"})
    if any(value is None for key, value in values.items() if key != "choices"):
        raise HTTPException(422, "字段属性不能为 null")
    try:
        # 先取得字段池的锁，再创建可能锁分组的规则，与 save_values 保持同序。
        row = field_pool.update_field(db, row, **values)
        if "default_rule" in body.model_fields_set:
            row = field_pool.update_field(
                db, row, default_rule_id=_new_rule(db, body.default_rule, viewer)
            )
        db.commit()
    except (ValueError, PermissionError) as exc:
        service_error(db, exc)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "同名字段已存在") from exc
    record_access_log(viewer, "field_def_update", "field_def", {"id": row.id})
    return _field_views(db, [row])[0]
