"""工作簿预览、确认与首次回执；调用方提交事务，业务写入用同一会话。"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import inspect
import json

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.models.workbook import WorkbookOperation
from app.security import UserContext, record_access_log
from app.services.workbook.parse import parse_workbook
from app.services.workbook.plan import Plan, digest_plan, json_value, plan
from app.services.workbook.registry import ApplyFailed, Conflict, Forbidden, NotFound, ValidationConflict, get_spec
from app.services.workbook.schema import WorkbookError, column_allowed
from app.services.workbook.validation import RowError, effective_plan, error_for_row
from app.services.workflow.registry import WorkflowError
from app.services.workflow.work_items import actor_context


def _actor(db, actor):
    try:
        return actor_context(db, actor)[1]
    except WorkflowError as exc:
        raise Forbidden("工作簿操作需要有效的实名账号") from exc


def _authorize(db, spec, ctx, subject_type, subject_id):
    if not spec.can_access(db, ctx, subject_type, subject_id):
        raise Forbidden("无权操作该工作簿")


def _result(operation, *, replayed=False):
    result = {"operation_id": operation.id, "plan_digest": digest_plan(operation.plan),
              "plan": deepcopy(operation.plan), "replayed": replayed,
              "status": operation.status}
    if operation.response is not None:
        result["response"] = deepcopy(operation.response)
    return result


def _check_plan_permissions(spec, data, ctx):
    columns = {sheet.key: {column.key: column for column in sheet.columns}
               for sheet in spec.schema.sheets}
    for row in data["rows"]:
        keys = set(row["values"]) | {change["key"] for change in row["changes"]}
        for key in keys:
            column = columns.get(row["sheet"], {}).get(key)
            if column is None and not key.startswith("+"):
                raise Forbidden("模板字段已变更，无法确认历史字段权限，请重新导出")
            if column is not None and not column_allowed(column, ctx):
                raise Forbidden("字段权限已变更，请重新导出后编辑")


def _validate(db, spec, comparison, ctx):
    if spec.validate_fn is None:
        return []
    savepoint = db.begin_nested()
    event.listen(db, "before_commit", _guard_commit)
    try:
        callback_plan = deepcopy(comparison)
        signature = inspect.signature(spec.validate_fn)
        try:
            signature.bind(db, callback_plan, ctx)
        except TypeError:
            # 仅在调用前按签名选择协议，不能捕获业务 TypeError 后重复执行。
            signature.bind(callback_plan, db)
            errors = spec.validate_fn(callback_plan, db)
        else:
            errors = spec.validate_fn(db, callback_plan, ctx)
        if not isinstance(errors, list) or any(not isinstance(error, RowError) for error in errors):
            raise WorkbookError("校验回调必须返回 RowError 列表")
        return [error.to_dict() for error in errors]
    finally:
        event.remove(db, "before_commit", _guard_commit)
        savepoint.rollback()


def _merge_errors(comparison, errors):
    for error in errors:
        comparison.errors.append(error)
        for row in comparison.rows:
            if row["sheet"] == error["sheet"] and (
                error.get("row_key") == row["key"] or error.get("row_key") is None
                and error.get("row_number") == row.get("row_number")
            ):
                row["errors"].append(error)


def _failed(db, operation, ctx, comparison, errors, *, conflict=False, cause=None):
    _merge_errors(comparison, errors)
    operation.status = "failed"
    operation.plan = comparison.to_dict()
    operation.response = {"error": "应用失败，整份业务更改已回滚",
                          "error_type": type(cause).__name__ if cause else "ValidationError",
                          "row_errors": errors}
    db.flush()
    record_access_log(ctx, "workbook_apply_failed", "workbook_operation",
                      {"operation_id": operation.id, "error_type": operation.response["error_type"]})
    message = "预览后服务器记录已变更，请重新导出后编辑" if conflict else operation.response["error"]
    exc_type = ValidationConflict if conflict else ApplyFailed
    raise exc_type(message, result=_result(operation)) from (
        cause or WorkbookError("；".join(error["message"] for error in errors)))


def preview(db: Session, schema_name: str, file_bytes: bytes, *, ctx: UserContext,
            subject_type: str | None = None, subject_id: int | None = None) -> dict:
    spec = get_spec(schema_name)
    ctx = _actor(db, ctx)
    if (subject_type is None) != (subject_id is None):
        raise WorkbookError("subject_type 和 subject_id 必须同时提供")
    if subject_type is not None and (not subject_type or len(subject_type) > 32
                                     or isinstance(subject_id, bool) or not 0 < subject_id < 2**63):
        raise WorkbookError("业务对象标识无效")
    _authorize(db, spec, ctx, subject_type, subject_id)
    # 精确文件已应用过时先取首次回执，模板升级不应触发重新解析和执行。
    file_sha256 = hashlib.sha256(file_bytes).hexdigest()
    applied = db.scalar(select(WorkbookOperation).where(
        WorkbookOperation.schema_name == schema_name,
        WorkbookOperation.file_sha256 == file_sha256,
        WorkbookOperation.actor == ctx.user_id,
        WorkbookOperation.status == "applied",
    ).with_for_update())
    if applied is not None:
        if (applied.subject_type, applied.subject_id) != (subject_type, subject_id):
            raise Conflict("该文件已关联其他工作簿对象，请重新导出")
        _check_plan_permissions(spec, applied.plan, ctx)
        record_access_log(ctx, "workbook_replay", "workbook_operation", {"operation_id": applied.id})
        return _result(applied, replayed=True)
    parsed = parse_workbook(spec.schema, file_bytes, ctx=ctx)
    if len(parsed.export_id) > 128:
        raise WorkbookError("导出编号过长，请重新导出模板")
    # 同文件并发预览串行化；唯一约束仍是最终数据库防线。
    lock_key = int.from_bytes(hashlib.sha256(
        f"{parsed.export_id}:{parsed.file_sha256}:{ctx.user_id}".encode()).digest()[:8],
        "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
    operation = db.scalar(select(WorkbookOperation).where(
        WorkbookOperation.export_id == parsed.export_id,
        WorkbookOperation.file_sha256 == parsed.file_sha256,
        WorkbookOperation.actor == ctx.user_id,
    ).with_for_update())
    if operation is not None:
        if (operation.schema_name, operation.subject_type, operation.subject_id) != (
            schema_name, subject_type, subject_id,
        ):
            raise Conflict("该文件已关联其他工作簿对象，请重新导出")
        _check_plan_permissions(spec, operation.plan, ctx)
        replayed = operation.status == "applied"
        record_access_log(ctx, "workbook_replay", "workbook_operation",
                          {"operation_id": operation.id, "replayed": replayed})
        return _result(operation, replayed=replayed)
    comparison = plan(parsed, lambda sheet, ids: spec.load_current(db, sheet, ids, ctx))
    comparison.subject_type = subject_type
    comparison.subject_id = subject_id
    valid_rows = [row for row in comparison.rows if row["status"] in ("added", "modified", "deleted")
                  and not row["errors"]]
    _merge_errors(comparison, _validate(db, spec, effective_plan(comparison, valid_rows), ctx))
    # 冲突已经由状态阻止应用；逐行补充错误供预览红框显示，保留原有全局计数契约。
    if spec.validate_fn is not None:
        for row in comparison.rows:
            if row["status"] == "conflict":
                row["errors"].append(error_for_row(row, Conflict("服务器记录已变更或不可访问，请重新导出后编辑")).to_dict())
    operation = WorkbookOperation(
        schema_name=schema_name, subject_type=subject_type, subject_id=subject_id,
        export_id=parsed.export_id, file_sha256=parsed.file_sha256,
        actor=ctx.user_id, plan=comparison.to_dict(), status="previewed",
    )
    db.add(operation)
    db.flush()
    record_access_log(ctx, "workbook_preview", "workbook_operation", {"operation_id": operation.id})
    return _result(operation)


def _guard_commit(_db):
    raise WorkbookError("工作簿业务回调不得自行提交事务")


def apply(db: Session, operation_id: int, actor, expected_plan_digest: str,
          apply_fn=None, *, confirm_deletes=()) -> dict:
    ctx = _actor(db, actor)
    operation = db.scalar(select(WorkbookOperation).where(
        WorkbookOperation.id == operation_id,
    ).with_for_update().execution_options(populate_existing=True))
    if operation is None:
        raise NotFound("工作簿操作不存在")
    if operation.actor != ctx.user_id:
        raise Forbidden("只能确认本人上传的工作簿")
    spec = get_spec(operation.schema_name)
    _authorize(db, spec, ctx, operation.subject_type, operation.subject_id)
    _check_plan_permissions(spec, operation.plan, ctx)
    if not isinstance(expected_plan_digest, str) or not hmac.compare_digest(
        digest_plan(operation.plan), expected_plan_digest,
    ):
        raise Conflict("预览内容已变更，请重新预览后确认")
    if operation.status == "applied":
        record_access_log(ctx, "workbook_replay", "workbook_operation", {"operation_id": operation.id})
        return _result(operation, replayed=True)
    if operation.status == "failed":
        raise Conflict("该次应用已失败，请修正文件后重新上传")
    stored = Plan.from_dict(deepcopy(operation.plan))
    if stored.schema_version != str(spec.schema.version):
        raise Conflict("模板版本已更新，请重新导出后编辑")
    if any(row["status"] == "conflict" for row in stored.rows):
        raise Conflict("工作簿存在版本冲突，请重新导出后编辑")
    if stored.errors or any(row["errors"] for row in stored.rows):
        business_errors = [error for error in stored.errors if error.get("kind") == "business"]
        if business_errors:
            # 失败回执沿用预览中的定位信息，不能绕过已知业务错误强行确认。
            clean = deepcopy(stored)
            clean.errors = [error for error in clean.errors if error.get("kind") != "business"]
            for row in clean.rows:
                row["errors"] = [error for error in row["errors"] if error.get("kind") != "business"]
            _failed(db, operation, ctx, clean, business_errors)
        raise WorkbookError("工作簿存在错误，请修正后重新上传")
    if not isinstance(confirm_deletes, (list, tuple, set)) or any(
        not isinstance(key, str) for key in confirm_deletes
    ):
        raise WorkbookError("删除确认必须逐行提供 delete_key")
    confirmed = set(confirm_deletes)
    candidates = {row["delete_key"] for row in stored.rows if row["status"] == "deleted"}
    if not confirmed <= candidates:
        raise WorkbookError("删除确认包含不属于本次预览的行")
    rows = [row for row in stored.rows if row["status"] in ("added", "modified")
            or row["status"] == "deleted" and row["delete_key"] in confirmed]
    # load_current 的注册契约要求在当前事务中锁定记录，直至外层提交。
    for sheet in sorted({row["sheet"] for row in rows if row["status"] != "added"}):
        existing = [row for row in rows if row["sheet"] == sheet and row["status"] != "added"]
        current = spec.load_current(db, sheet, [row["entity_id"] for row in existing], ctx)
        stale = [row for row in existing if row["entity_id"] not in current
                 or current[row["entity_id"]][0] != row["base_version"]]
        if stale:
            if spec.validate_fn is not None:
                errors = [error_for_row(row, Conflict("预览后服务器记录已变更，请重新导出后编辑")).to_dict()
                          for row in stale]
                _failed(db, operation, ctx, stored, errors, conflict=True)
            raise Conflict("预览后服务器记录已变更，请重新导出后编辑")
    effective = Plan(stored.schema_name, stored.export_id, rows,
                     dict(Counter(row["status"] for row in rows)), [], stored.warnings,
                     stored.schema_version, stored.subject_type, stored.subject_id)
    errors = _validate(db, spec, effective, ctx)
    if errors:
        _failed(db, operation, ctx, stored, errors)
    try:
        with db.begin_nested():
            event.listen(db, "before_commit", _guard_commit)
            try:
                response = (apply_fn or spec.apply_fn)(db, effective, ctx)
            finally:
                event.remove(db, "before_commit", _guard_commit)
            if not isinstance(response, dict):
                raise WorkbookError("工作簿应用回调必须返回回执对象")
            response = json_value(response)
            json.dumps(response, allow_nan=False)
            db.flush()
    except Exception as exc:
        errors = [error_for_row(row, exc).to_dict() for row in effective.rows]
        _failed(db, operation, ctx, stored, errors, cause=exc)
    operation.status = "applied"
    operation.response = response
    operation.applied_at = datetime.now(timezone.utc)
    db.flush()
    record_access_log(ctx, "workbook_apply", "workbook_operation",
                      {"operation_id": operation.id, "confirmed_deletes": sorted(confirmed)})
    return _result(operation)
