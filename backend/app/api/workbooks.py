"""通用工作簿接口；业务 schema 和权限由注册表提供。"""
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth import current_identity
from app.db import get_db
from app.security import UserContext
from app.services.workbook import operations
from app.services.workbook.registry import ApplyFailed, get_spec
from app.services.workbook.schema import WorkbookError

router = APIRouter(prefix="/workbooks", tags=["workbooks"])
Db = Annotated[Session, Depends(get_db)]


def workbook_context(ident: dict = Depends(current_identity)) -> UserContext:
    if ident.get("fb") or ident.get("authn") == "shared" or not ident.get("sub"):
        raise HTTPException(403, "工作簿操作需要实名账号")
    return UserContext(user_id=ident["sub"], role=ident.get("role", "guest"),
                       permissions=ident.get("perms"), is_authenticated=True)


Ctx = Annotated[UserContext, Depends(workbook_context)]


@router.get("/{schema_name}/schema")
def workbook_schema(schema_name: str, db: Db, ctx: Ctx):
    try:
        spec = get_spec(schema_name)
        ctx = operations._actor(db, ctx)
        operations._authorize(db, spec, ctx, None, None)
        return spec.schema.to_dict(ctx=ctx)
    except WorkbookError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/{schema_name}/preview")
async def preview_workbook(
    schema_name: str, db: Db, ctx: Ctx, file: UploadFile = File(...),
    subject_type: str | None = Query(default=None, max_length=32),
    subject_id: int | None = Query(default=None, gt=0, le=2**63 - 1),
):
    try:
        if not file.filename or not file.filename.lower().endswith(".xlsx"):
            raise WorkbookError("只支持 .xlsx 文件，请使用系统导出的模板")
        content = await file.read(10 * 1024 * 1024 + 1)
        result = operations.preview(db, schema_name, content, ctx=ctx,
                                    subject_type=subject_type, subject_id=subject_id)
        db.commit()
        return result
    except WorkbookError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    finally:
        await file.close()


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_plan_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirm_deletes: list[str] = Field(default_factory=list)


@router.post("/operations/{operation_id}/apply")
def apply_workbook(operation_id: int, body: ApplyInput, db: Db, ctx: Ctx):
    try:
        result = operations.apply(db, operation_id, ctx, body.expected_plan_digest,
                                  confirm_deletes=body.confirm_deletes)
        db.commit()
        return result
    except ApplyFailed as exc:
        # 业务已回滚到 savepoint；保留 failed 回执，不把业务写回去。
        db.commit()
        detail = {"message": str(exc), **exc.result} if exc.result else str(exc)
        raise HTTPException(exc.status_code, detail) from exc
    except WorkbookError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
