"""M7 字段池：只保存通用主体标识，事务由调用方提交。"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.fields_audience import AudienceGroup, AudienceRule, FieldAnchorUsage, FieldDef, FieldValue
from app.security import UserContext
from app.services import audience


class FieldPoolError(ValueError):
    def __init__(self, message: str, *, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Subject:
    subject_type: str
    subject_id: str
    level: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user(viewer: UserContext) -> str:
    if not isinstance(viewer.user_id, str) or not viewer.user_id or len(viewer.user_id) > 64:
        raise PermissionError("字段池需要实名账号")
    return viewer.user_id


def _subject_type_level(subject_type: str, level: str) -> None:
    if not isinstance(subject_type, str) or not subject_type.strip() or len(subject_type) > 64:
        raise ValueError("subject_type 必须为 1–64 字")
    if level not in {"header", "line"}:
        raise ValueError("level 必须为 header 或 line")


def _subject(subject) -> Subject:
    if isinstance(subject, dict):
        if subject.get("subject_id") is None:
            raise ValueError("subject_id 不能为空")
        result = Subject(subject["subject_type"], str(subject["subject_id"]), subject["level"])
    else:
        if subject.subject_id is None:
            raise ValueError("subject_id 不能为空")
        result = Subject(subject.subject_type, str(subject.subject_id), subject.level)
    _subject_type_level(result.subject_type, result.level)
    if not result.subject_id.strip() or len(result.subject_id) > 128:
        raise ValueError("subject_id 必须为 1–128 字")
    return result


def _lock(db: Session, key: str) -> None:
    # 主体尚未有值时也能串行化，防止并发新增突破每层 20 个的上限。
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


def _definition(name, value_type, choices) -> str:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 32:
        raise ValueError("字段名称必须为 1–32 字")
    if value_type not in {"text", "number", "date", "bool", "choice"}:
        raise ValueError("不支持的字段类型")
    if value_type == "choice":
        if (not isinstance(choices, list) or not choices
                or any(not isinstance(v, str) or not v or len(v) > 2000 for v in choices)
                or len(set(choices)) != len(choices)):
            raise ValueError("单选字段必须提供不重复的非空文本选项")
    elif choices is not None:
        raise ValueError("只有单选字段可以设置 choices")
    return name.strip()


def _owned_rule(db: Session, rule_id, viewer: UserContext) -> None:
    if rule_id is not None:
        rule = db.get(AudienceRule, rule_id)
        if rule is None or rule.owner_id != _user(viewer):
            raise ValueError("可见性规则不存在或不属于当前账号")


def define_field(db: Session, *, subject_type: str, level: str, name: str,
                 viewer: UserContext, owner_scope: str = "personal", value_type: str = "text",
                 choices=None, default_rule_id=None, auto_include: bool = True, sort: int = 0) -> FieldDef:
    """团队池权限由调用方校验，模块本身不认识业务权限键。"""
    owner = _user(viewer)
    _subject_type_level(subject_type, level)
    name = _definition(name, value_type, choices)
    if owner_scope not in {"personal", "team"}:
        raise ValueError("owner_scope 必须为 personal 或 team")
    _owned_rule(db, default_rule_id, viewer)
    owner_id = owner if owner_scope == "personal" else None
    _lock(db, f"field-def:{owner_scope}:{owner_id}:{subject_type}:{level}")
    duplicate = db.scalar(select(FieldDef.id).where(
        FieldDef.owner_scope == owner_scope, FieldDef.owner_id == owner_id,
        FieldDef.subject_type == subject_type, FieldDef.level == level,
        FieldDef.name == name, FieldDef.archived.is_(False)))
    if duplicate is not None:
        raise FieldPoolError("字段名称已存在", status_code=409)
    field = FieldDef(owner_scope=owner_scope, owner_id=owner_id, subject_type=subject_type,
                     level=level, name=name, value_type=value_type, choices=choices,
                     default_rule_id=default_rule_id, auto_include=auto_include, sort=sort,
                     archived=False)
    db.add(field)
    db.flush()
    return field


def update_field(db: Session, field_def: FieldDef, **changes) -> FieldDef:
    """复用定义校验；个人归属和团队写权限由接口在调用前检查。"""
    allowed = {"name", "value_type", "choices", "default_rule_id", "auto_include", "sort", "archived"}
    if changes.keys() - allowed:
        raise ValueError("不支持的字段定义属性")
    _lock(db, f"field-def:{field_def.owner_scope}:{field_def.owner_id}:{field_def.subject_type}:{field_def.level}")
    field_def = db.scalar(select(FieldDef).where(FieldDef.id == field_def.id)
                          .with_for_update().execution_options(populate_existing=True))
    name = _definition(changes.get("name", field_def.name),
                       changes.get("value_type", field_def.value_type),
                       changes.get("choices", field_def.choices))
    archived = changes.get("archived", field_def.archived)
    if not archived:
        duplicate = db.scalar(select(FieldDef.id).where(
            FieldDef.id != field_def.id, FieldDef.owner_scope == field_def.owner_scope,
            FieldDef.owner_id == field_def.owner_id, FieldDef.subject_type == field_def.subject_type,
            FieldDef.level == field_def.level, FieldDef.name == name, FieldDef.archived.is_(False)))
        if duplicate is not None:
            raise FieldPoolError("字段名称已存在", status_code=409)
    if any(k in changes and changes[k] != getattr(field_def, k) for k in ("value_type", "choices")):
        if db.scalar(select(FieldValue.id).where(FieldValue.field_def_id == field_def.id).limit(1)):
            raise ValueError("已有历史值的字段不能修改类型或选项")
    for key, value in changes.items():
        setattr(field_def, key, name if key == "name" else value)
    db.flush()
    return field_def


def archive_field(db: Session, field_def: FieldDef, viewer: UserContext | None = None) -> FieldDef:
    if viewer is not None and field_def.owner_scope == "personal" and field_def.owner_id != _user(viewer):
        raise PermissionError("只有本人可以修改个人字段")
    return update_field(db, field_def, archived=True)


def list_defs(db: Session, subject_type: str, level: str, viewer: UserContext) -> list[FieldDef]:
    _subject_type_level(subject_type, level)
    owner = _user(viewer)
    fields = db.scalars(select(FieldDef).where(
        FieldDef.subject_type == subject_type, FieldDef.level == level, FieldDef.archived.is_(False),
        (FieldDef.owner_scope == "team") | ((FieldDef.owner_scope == "personal") & (FieldDef.owner_id == owner))
    ).execution_options(populate_existing=True)).all()
    # 团队定义覆盖同名个人定义，排序不影响覆盖顺序。
    merged = {field.name: field for field in sorted(fields, key=lambda f: f.owner_scope == "team")}
    return sorted(merged.values(), key=lambda f: (f.sort, f.name, f.id))


def template_for(db: Session, subject_type: str, level: str, viewer: UserContext,
                 anchor: str | None = None) -> list[FieldDef]:
    fields = list_defs(db, subject_type, level, viewer)
    remembered = set()
    if anchor is not None:
        _anchor(anchor)
        remembered = set(db.scalars(select(FieldAnchorUsage.field_def_id).where(
            FieldAnchorUsage.anchor == anchor, FieldAnchorUsage.user_id == _user(viewer),
            FieldAnchorUsage.last_used_at >= _now() - timedelta(days=180))).all())
    return [field for field in fields if field.auto_include or field.id in remembered]


def _anchor(anchor: str) -> None:
    if not isinstance(anchor, str) or not anchor.strip() or len(anchor) > 64:
        raise ValueError("anchor 必须为 1–64 字")


def _value(field: FieldDef, value) -> str:
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif isinstance(value, (int, float, Decimal)):
        value = str(value)
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("字段值必须为文本且最多 2000 字")
    if field.value_type == "number":
        try:
            if not value.strip() or not Decimal(value).is_finite():
                raise InvalidOperation
        except InvalidOperation as exc:
            raise ValueError("数字字段格式错误") from exc
    elif field.value_type == "date":
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("日期字段格式必须为 YYYY-MM-DD") from exc
    elif field.value_type == "bool" and value not in {"true", "false"}:
        raise ValueError("是否字段格式必须为 true 或 false")
    elif field.value_type == "choice" and value not in (field.choices or []):
        raise ValueError("字段值不在单选选项中")
    return value


def save_values(db: Session, subject, values: list[dict], viewer: UserContext,
                anchor: str | None = None) -> None:
    """按传入项增改删，未传入的值保留；失败回滚本次操作，调用方仍拥有事务。"""
    subject = _subject(subject)
    owner = _user(viewer)
    if anchor is not None:
        _anchor(anchor)
    _lock(db, f"field-values:{subject.subject_type}:{subject.subject_id}:{subject.level}")
    # 与定义增改保持相同锁序，读取定义到写值期间不允许归档或修改类型。
    for scope, owner_id in (("team", None), ("personal", owner)):
        _lock(db, f"field-def:{scope}:{owner_id}:{subject.subject_type}:{subject.level}")
    with db.begin_nested():
        groups = set()
        for entry in values:
            if isinstance(entry.get("rule"), dict):
                ids = entry["rule"].get("group_ids", [])
                if not isinstance(ids, list) or any(type(gid) is not int or gid <= 0 for gid in ids):
                    raise ValueError("分组标识无效")
                groups.update(ids)
        if groups:
            # 一次按 id 锁全体分组，避免多字段规则分别锁组时交叉等待。
            list(db.scalars(select(AudienceGroup).where(AudienceGroup.id.in_(groups))
                            .order_by(AudienceGroup.id).with_for_update()))
        fields = list_defs(db, subject.subject_type, subject.level, viewer)
        by_id = {field.id: field for field in fields}
        by_name = {field.name: field for field in fields}
        stored = db.execute(select(FieldValue, FieldDef).join(FieldDef).where(
            FieldValue.subject_type == subject.subject_type, FieldValue.subject_id == subject.subject_id,
            FieldDef.level == subject.level).execution_options(populate_existing=True)).all()
        current = {item.field_def_id: item for item, _ in stored}
        clearable = {field.id for _, field in stored if field.owner_scope == "team" or field.owner_id == owner}
        seen = set()
        for entry in values:
            if bool(entry.get("field_def_id")) == bool(entry.get("new_name")):
                raise ValueError("field_def_id 和 new_name 必须二选一")
            if entry.get("new_name"):
                name = _definition(entry["new_name"], "text", None)
                field = by_name.get(name)
                if field is None:
                    field = define_field(db, subject_type=subject.subject_type, level=subject.level,
                                         name=name, viewer=viewer)
                    by_name[name] = field
                    by_id[field.id] = field
            else:
                field = by_id.get(entry["field_def_id"])
                if field is None:
                    field_id = entry["field_def_id"]
                    if field_id in clearable and entry.get("value") in (None, ""):
                        if field_id in seen:
                            raise ValueError("同一字段不能重复提交")
                        seen.add(field_id)
                        db.delete(current.pop(field_id))
                        continue
                    raise ValueError("字段不存在、已归档或不属于当前字段池及层级")
            if field.id in seen:
                raise ValueError("同一字段不能重复提交")
            seen.add(field.id)
            existing = current.get(field.id)
            if entry.get("value") in (None, ""):
                if existing is not None:
                    db.delete(existing)
                    current.pop(field.id)
                continue
            value = _value(field, entry["value"])
            rule_id = existing.rule_id if existing is not None else None
            if "rule" in entry:
                rule = entry["rule"]
                if isinstance(rule, dict):
                    rule_id = audience.create_rule(db, rule.get("mode"), rule.get("user_ids", []),
                                                   rule.get("group_ids", []), viewer)
                elif rule is None or isinstance(rule, int):
                    _owned_rule(db, rule, viewer)
                    rule_id = rule
                else:
                    raise ValueError("可见性规则格式错误")
            if existing is None:
                existing = FieldValue(subject_type=subject.subject_type, subject_id=subject.subject_id,
                                      field_def_id=field.id, value=value, rule_id=rule_id,
                                      updated_by=owner, updated_at=_now())
                db.add(existing)
                current[field.id] = existing
            else:
                existing.value, existing.rule_id = value, rule_id
                existing.updated_by, existing.updated_at = owner, _now()
            # 无 anchor 的使用也计入名称联想，空串保留为内部计数槽，不参与 PN 模板召回。
            stmt = insert(FieldAnchorUsage).values(field_def_id=field.id, anchor=anchor or "",
                user_id=owner, last_used_at=_now(), uses=1)
            db.execute(stmt.on_conflict_do_update(
                index_elements=["field_def_id", "anchor", "user_id"],
                set_={"last_used_at": stmt.excluded.last_used_at, "uses": FieldAnchorUsage.uses + 1}))
        if len(current) > 20:
            raise ValueError("每个主体每层最多 20 个字段")
        db.flush()


def read_values(db: Session, subjects, viewer: UserContext, bypass, *,
                include_rules: bool = False) -> dict[str, list[dict]]:
    subjects = [_subject(subject) for subject in subjects]
    if len({s.subject_type for s in subjects}) > 1:
        raise ValueError("一次读取只能包含一种 subject_type，避免 subject_id 冲突")
    output = {subject.subject_id: [] for subject in subjects}
    if not subjects:
        return output
    levels = {(s.subject_id, s.level) for s in subjects}
    rows = db.execute(select(FieldValue, FieldDef).join(FieldDef).where(
        FieldValue.subject_type == subjects[0].subject_type,
        FieldValue.subject_id.in_(output)).execution_options(populate_existing=True)).all()
    records, rules = [], {}
    for value, field in rows:
        if (value.subject_id, field.level) not in levels:
            continue
        key = str(value.id)
        records.append({key: (value.subject_id, {"field_def_id": field.id, "name": field.name, "value": value.value})})
        rules[key] = (value.rule_id if value.rule_id is not None else field.default_rule_id, value.updated_by)
    rule_views = audience.read_rules(db, rules, viewer, bypass=bypass) if include_rules else {}
    for record in audience.filter_fields(db, records, viewer, field_rules=rules, bypass=bypass):
        for key, (subject_id, payload) in record.items():
            if key in rule_views:
                payload["rule"] = rule_views[key]
            output[subject_id].append(payload)
    ordering = {field.id: (field.sort, field.name, field.id) for _, field in rows}
    for values in output.values():
        values.sort(key=lambda v: ordering[v["field_def_id"]])
    return output


def name_suggestions(db: Session, subject_type: str, q: str, viewer: UserContext) -> list[dict]:
    fields = list_defs(db, subject_type, "header", viewer) + list_defs(db, subject_type, "line", viewer)
    fields = [field for field in fields if q.casefold() in field.name.casefold()]
    if not fields:
        return []
    counts = dict(db.execute(select(FieldAnchorUsage.field_def_id, func.sum(FieldAnchorUsage.uses))
        .where(FieldAnchorUsage.field_def_id.in_([f.id for f in fields]))
        .group_by(FieldAnchorUsage.field_def_id)).all())
    return [{"field_def_id": field.id, "name": field.name, "uses": counts.get(field.id, 0)}
            for field in sorted(fields, key=lambda f: (-counts.get(f.id, 0), f.name, f.id))]
