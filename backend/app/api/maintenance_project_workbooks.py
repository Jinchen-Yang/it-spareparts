"""Production HTTP loop for stable-project four-sheet workbooks."""

from __future__ import annotations

from pathlib import Path as FilePath

import anyio
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.maintenance import (
    _content_disposition,
    _parse_and_save_roundtrip_upload,
    _remove_roundtrip_temp,
    _require_workbook_export_permissions,
)
from app.api.maintenance_project_operations import _real_operator
from app.auth import current_identity, current_role
from app.business_time import business_today
from app.config import get_settings
from app.db import get_db
from app.security import (
    UserContext,
    get_current_user_context,
    require_action,
    require_page,
)
from app.services.maintenance_project_workbook_adapter import (
    MaintenanceProjectWorkbookAdapter,
    workbook_preview,
)
from app.services.maintenance_project_workbook_v2 import ProjectWorkbookV2Error


router = APIRouter(prefix="/maintenance", tags=["maintenance"])
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class WorkbookApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    validation_token: str = Field(min_length=1, max_length=64)
    data_version: str = Field(min_length=1, max_length=64)


def _hmac_key() -> bytes:
    key = get_settings().secret_key.encode("utf-8")
    if len(key) < 16:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "服务端工作簿签名密钥配置无效",
        )
    return key


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _http_error(exc: ProjectWorkbookV2Error) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "message": str(exc),
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "sheet": issue.sheet,
                    "row": issue.row,
                    "column": issue.column,
                }
                for issue in exc.issues
            ],
        },
    )


def _safe_export_operator(ident: dict) -> str:
    return str(ident.get("sub") or ident.get("role") or "maintenance-export")[:64]


@router.get("/projects/stable/{project_id}/workbook")
def export_project_workbook(
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    ctx: UserContext = Depends(get_current_user_context),
) -> Response:
    _require_workbook_export_permissions(ctx)
    adapter = MaintenanceProjectWorkbookAdapter(
        db,
        user_ctx=ctx,
        operator=_safe_export_operator(ident),
        as_of=business_today(),
    )
    try:
        artifact = adapter.export(project_id, hmac_key=_hmac_key())
        db.commit()
    except ProjectWorkbookV2Error as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "工作簿导出记录冲突，请重试"
        ) from exc
    except Exception:
        db.rollback()
        raise
    return Response(
        content=artifact.content,
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": _content_disposition(
                artifact.filename,
                ascii_fallback="maintenance_project_workbook.xlsx",
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/projects/stable/{project_id}/workbook/validate")
async def validate_project_workbook_upload(
    request: Request,
    response: Response,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_roundtrip_apply",
            require_data="data_profit",
        )
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _no_store(response)
    _require_workbook_export_permissions(ctx)
    operator = _real_operator(db, ident)
    upload_path: str | None = None
    try:
        upload_path, original_name = await _parse_and_save_roundtrip_upload(request)
        content = await anyio.to_thread.run_sync(FilePath(upload_path).read_bytes)
        adapter = MaintenanceProjectWorkbookAdapter(
            db,
            user_ctx=ctx,
            operator=operator,
            as_of=business_today(),
        )
        validation, issues, validation_id = adapter.validate(
            project_id,
            content,
            hmac_key=_hmac_key(),
        )
        workspace = adapter.load_workspace(project_id)
        exported_at = (
            validation.metadata.get("exported_at") if validation is not None else None
        )
        payload = {
            "validation_token": validation_id,
            "project_id": project_id,
            "data_version": workspace["data_version"],
            "filename": original_name,
            "preview": workbook_preview(
                workspace,
                data_version=str(workspace["data_version"]),
                exported_at=exported_at,
            ),
            "changes": {
                "collection_append": len(validation.creates) if validation else 0
            },
            "warnings": (
                ["未检测到新增回款；确认后将记录本月已更新"]
                if validation is not None and validation.unchanged
                else []
            ),
            "errors": [issue.message for issue in issues],
            "can_apply": validation is not None,
        }
        db.commit()
        return payload
    except HTTPException:
        db.rollback()
        raise
    except ProjectWorkbookV2Error as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "工作簿校验记录冲突，请重新上传",
        ) from exc
    except Exception:
        db.rollback()
        raise
    finally:
        _remove_roundtrip_temp(upload_path)


@router.post("/projects/stable/{project_id}/workbook/apply")
def apply_project_workbook_plan(
    body: WorkbookApplyRequest,
    response: Response,
    project_id: str = Path(..., min_length=1, max_length=36),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_roundtrip_apply",
            require_data="data_profit",
        )
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    _no_store(response)
    _require_workbook_export_permissions(ctx)
    operator = _real_operator(db, ident)
    adapter = MaintenanceProjectWorkbookAdapter(
        db,
        user_ctx=ctx,
        operator=operator,
        as_of=business_today(),
    )
    try:
        result, state = adapter.apply_validation(
            project_id,
            body.validation_token,
            data_version=body.data_version,
        )
        payload = {
            "applied": True,
            "changed_rows": result.created,
            "data_version": state.data_version,
            "warnings": [],
        }
        db.commit()
        return payload
    except ProjectWorkbookV2Error as exc:
        db.rollback()
        raise _http_error(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "工作簿应用与现有数据冲突，未写入任何变更",
        ) from exc
    except Exception:
        db.rollback()
        raise


@router.get("/workbook-validations/{validation_id}/errors.xlsx")
def download_project_workbook_errors(
    validation_id: str = Path(..., min_length=1, max_length=64),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(
            "action_maintenance_roundtrip_apply",
            require_data="data_profit",
        )
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> Response:
    _require_workbook_export_permissions(ctx)
    operator = _real_operator(db, ident)
    adapter = MaintenanceProjectWorkbookAdapter(
        db,
        user_ctx=ctx,
        operator=operator,
        as_of=business_today(),
    )
    content = adapter.load_error_workbook(validation_id)
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "错误工作簿不存在或已过期")
    return Response(
        content=content,
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": _content_disposition(
                f"maintenance_workbook_errors_{validation_id[:12]}.xlsx"
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
