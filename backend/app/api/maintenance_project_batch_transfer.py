"""All-project maintenance import/export gateway.

Public contract (``/maintenance/project-batch-transfer``):

* ``GET /options`` returns permission-filtered import and export registries.
* ``POST /preview`` accepts one or more raw ``.xlsx`` files and freezes a
  server-owned mapping/match plan.
* ``POST /apply`` accepts only the preview token, payload/data CAS hashes and
  selected row keys.  Client-supplied canonical values are never accepted.
* ``POST /download`` exports all projects matching the current board filters,
  constrained to the server field/form whitelist.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import config, permissions
from app.auth import current_identity, current_role
from app.api.maintenance_project_scope import resolve_visible_project_ids
from app.business_time import business_today
from app.db import get_db
from app.maintenance_boss import require_maintenance_boss
from app.security import (
    UserContext,
    get_current_user_context,
    record_access_log,
    require_action,
    require_page,
)
from app.services import import_safety
from app.services import maintenance_boss_board as board
from app.services import maintenance_bulk_import as bulk
from app.services import maintenance_project_assignments
from app.services import maintenance_project_catalog as catalog
from app.services import maintenance_project_export as project_export
from app.services import maintenance_project_operations as operations


router = APIRouter(
    prefix="/maintenance/project-batch-transfer",
    tags=["maintenance"],
    dependencies=[Depends(require_maintenance_boss)],
)

_IMPORT_ACTION = "action_maintenance_ledger_import"

_DOWNLOAD_FORMS = (
    {
        "key": "project_master",
        "label": "项目主档与期限",
        "description": "项目名称、负责人、期限、生命周期等主数据",
        "groups": {"项目基础", "期限"},
        "default_selected": True,
    },
    {
        "key": "contract",
        "label": "合同",
        "description": "销售单号、含税合同额及完整性状态",
        "groups": {"合同与回款"},
        "key_prefixes": ("contract_",),
        "explicit_keys": {"contract_nos"},
        "default_selected": True,
    },
    {
        "key": "collection",
        "label": "累计回款",
        "description": "已确认累计实收及数据状态",
        "groups": {"合同与回款"},
        "key_prefixes": ("collection_",),
        "default_selected": True,
    },
    {
        "key": "maintenance_activity",
        "label": "维保业务统计",
        "description": "单据、数量、预交付与供退货统计",
        "groups": {"业务统计"},
        "default_selected": False,
    },
    {
        "key": "cost",
        "label": "成本与状态",
        "description": "按账号权限提供已知成本、成本率和卡片状态",
        "groups": {"成本"},
        "default_selected": False,
    },
)


class BatchApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_token: str = Field(min_length=25, max_length=256)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_version: str | int
    row_keys: list[str] = Field(min_length=1, max_length=bulk.MAX_PREVIEW_ROWS)


class BatchDownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forms: list[str] = Field(min_length=1, max_length=len(_DOWNLOAD_FORMS))
    fields: list[str] = Field(
        min_length=1,
        max_length=len(project_export.EXPORT_FIELDS),
    )
    q: str | None = Field(default=None, max_length=128)
    lifecycle: str = Field(
        default="all",
        pattern=r"^(ongoing|ended|missing|payment_complete|all)$",
    )
    card_status: str | None = Field(
        default=None,
        pattern=r"^(normal|warning|alert)$",
    )
    sort: str = Field(
        default="name",
        pattern=r"^(attention|orders|name|known_cost|cost_ratio)$",
    )


def _require_board_view(
    ctx: UserContext = Depends(get_current_user_context),
) -> UserContext:
    graph = ctx.permissions or permissions.template_for(ctx.role)
    safe = permissions.runtime_safe(graph)
    if (
        not config.ENABLE_RBAC
        or ctx.role == "admin"
        or safe.get("page_maintenance_boss")
        or safe.get("page_maintenance")
    ):
        return ctx
    raise HTTPException(status.HTTP_403_FORBIDDEN, "无维保项目查看权限")


def _can_import(ctx: UserContext) -> bool:
    if not config.ENABLE_RBAC or ctx.role == "admin":
        return True
    graph = ctx.permissions or permissions.effective(ctx.role, None)
    safe = permissions.runtime_safe(graph)
    return bool(safe.get(_IMPORT_ACTION) and safe.get("data_profit"))


def _operator(ident: dict) -> str:
    return str(ident.get("username") or ident.get("sub") or "unknown")


def _field_form_keys(field: project_export.ExportField) -> list[str]:
    keys: list[str] = []
    for form in _DOWNLOAD_FORMS:
        if field.group not in form.get("groups", set()):
            continue
        prefixes = form.get("key_prefixes")
        explicit = form.get("explicit_keys", set())
        if prefixes or explicit:
            if field.key not in explicit and not any(
                field.key.startswith(prefix) for prefix in prefixes or ()
            ):
                continue
        keys.append(str(form["key"]))
    return keys


def _download_options(ctx: UserContext) -> tuple[list[dict], list[dict], list[str], list[str]]:
    export = project_export.export_options(ctx)
    available_keys = {field["key"] for field in export["fields"]}
    fields_by_key = {field.key: field for field in project_export.EXPORT_FIELDS}
    fields = [
        {
            **field,
            "form_keys": _field_form_keys(fields_by_key[field["key"]]),
        }
        for field in export["fields"]
        if _field_form_keys(fields_by_key[field["key"]])
    ]
    default_fields = [
        key
        for key in export["default_fields"]
        if key in available_keys
        and any(field["key"] == key for field in fields)
    ]
    default_forms = list(
        dict.fromkeys(
            form_key
            for field in fields
            if field["key"] in default_fields
            for form_key in field["form_keys"]
        )
    )
    forms = [
        {
            "key": form["key"],
            "label": form["label"],
            "description": form["description"],
            "default_selected": form["key"] in default_forms,
        }
        for form in _DOWNLOAD_FORMS
        if any(form["key"] in field["form_keys"] for field in fields)
    ]
    return forms, fields, default_forms, default_fields


@router.get("/options")
def transfer_options(
    response: Response,
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(_require_board_view),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    forms, fields, default_forms, default_fields = _download_options(ctx)
    return {
        "can_import": _can_import(ctx),
        "can_download": True,
        "max_files": bulk.MAX_TRANSFER_FILES,
        "accepted_extensions": [".xlsx"],
        "import_kinds": [
            {
                "key": (
                    "sales_contract"
                    if form["form_type"] == "sales_contract_amount"
                    else "receipt"
                ),
                "label": form["label"],
                "description": (
                    "含税额按订单金额/含税标记/税率/税金/未税金额交叉校验"
                    if form["form_type"] == "sales_contract_amount"
                    else "累计值只取实收金额；销售收款额和优惠额仅用于对账"
                ),
                "required_fields": form["required_fields"],
                "accepted_aliases": form["accepted_headers"],
                "metric_basis": (
                    {
                        "contract_amount": "inc_tax",
                        "sales_source_sync": "amount_ex_tax+tax_rate_if_unique",
                    }
                    if form["form_type"] == "sales_contract_amount"
                    else {
                        "collection": "actual_received_inc_tax",
                        "source_field": "收款明细.实收金额",
                    }
                ),
            }
            for form in bulk.registered_forms()
        ],
        "download_forms": forms,
        "download_fields": fields,
        "default_forms": default_forms,
        "default_fields": default_fields,
    }


@router.post("/preview")
async def preview_transfer(
    response: Response,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(_IMPORT_ACTION, require_data="data_profit")
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if not files or len(files) > bulk.MAX_TRANSFER_FILES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "invalid_file_count",
                "message": f"一次必须上传 1–{bulk.MAX_TRANSFER_FILES} 个文件",
            },
        )
    payloads: list[tuple[str, bytes]] = []
    for file in files:
        filename = file.filename or ""
        if not filename.lower().endswith(".xlsx"):
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                {"code": "unsupported_media_type", "message": "只接受 .xlsx 文件"},
            )
        try:
            data = await import_safety.read_limited(file, bulk.MAX_PREVIEW_BYTES)
            import_safety.validate_xlsx_zip(data, max_bytes=bulk.MAX_PREVIEW_BYTES)
        except import_safety.UploadSafetyError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                {"code": "invalid_xlsx", "message": str(exc)},
            ) from exc
        payloads.append((filename, data))
    operator = _operator(ident)
    allowed_project_ids = resolve_visible_project_ids(db, ctx)
    record_access_log(
        ctx,
        "maintenance_project_batch_preview",
        "maintenance",
        {
            "file_count": len(payloads),
            "filenames": [name for name, _ in payloads],
            "scope": "full" if allowed_project_ids is None else "owned",
        },
    )
    try:
        return bulk.preview_transfer(
            db,
            payloads,
            operated_by=operator,
            allowed_project_ids=allowed_project_ids,
        )
    except bulk.BulkImportScopeDenied as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"code": "permission_denied", "message": str(exc)},
        ) from exc
    except bulk.BulkImportConflict as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "preview_conflict", "message": str(exc)},
        ) from exc
    except bulk.BulkImportInvalid as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "invalid_workbook",
                "message": str(exc),
                "issues": exc.issues,
            },
        ) from exc


@router.post("/apply")
def apply_transfer(
    body: BatchApplyRequest,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _auth: str = Depends(current_role),
    _page: None = Depends(require_page("page_maintenance")),
    _action: None = Depends(
        require_action(_IMPORT_ACTION, require_data="data_profit")
    ),
    ctx: UserContext = Depends(get_current_user_context),
) -> dict:
    operator = _operator(ident)
    allow_admin = ctx.role in {"admin", "boss"}
    allowed_project_ids = resolve_visible_project_ids(db, ctx)
    record_access_log(
        ctx,
        "maintenance_project_batch_apply",
        "maintenance",
        {"selected_rows": len(body.row_keys)},
    )
    try:
        return bulk.apply_transfer(
            db,
            preview_token=body.preview_token,
            payload_hash=body.payload_hash,
            data_version=str(body.data_version),
            row_keys=body.row_keys,
            operated_by=operator,
            allow_admin=allow_admin,
            allowed_project_ids=allowed_project_ids,
        )
    except bulk.BulkImportScopeDenied as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {"code": "permission_denied", "message": str(exc)},
        ) from exc
    except bulk.BulkImportInvalid as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "invalid_selection", "message": str(exc), "issues": exc.issues},
        ) from exc
    except (
        bulk.BulkImportConflict,
        catalog.MaintenanceProjectCatalogConflict,
        operations.MaintenanceOperationConflict,
    ) as exc:
        db.rollback()
        try:
            bulk.record_transfer_failure(
                db,
                preview_token=body.preview_token,
                operated_by=operator,
                error_code="apply_conflict",
                message=str(exc),
                allow_admin=allow_admin,
            )
        except Exception:  # noqa: BLE001 - preserve the original controlled error
            db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "apply_conflict", "message": str(exc)},
        ) from exc
    except (
        catalog.MaintenanceProjectCatalogError,
        operations.MaintenanceOperationError,
    ) as exc:
        db.rollback()
        try:
            bulk.record_transfer_failure(
                db,
                preview_token=body.preview_token,
                operated_by=operator,
                error_code="business_rule_violation",
                message=str(exc),
                allow_admin=allow_admin,
            )
        except Exception:  # noqa: BLE001
            db.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "business_rule_violation", "message": str(exc)},
        ) from exc
    except Exception as exc:
        db.rollback()
        try:
            bulk.record_transfer_failure(
                db,
                preview_token=body.preview_token,
                operated_by=operator,
                error_code="apply_failed",
                message=type(exc).__name__,
                allow_admin=allow_admin,
            )
        except Exception:  # noqa: BLE001
            db.rollback()
        raise


@router.post("/download")
def download_transfer(
    body: BatchDownloadRequest,
    db: Session = Depends(get_db),
    _auth: str = Depends(current_role),
    ctx: UserContext = Depends(_require_board_view),
) -> Response:
    forms, fields, _default_forms, _default_fields = _download_options(ctx)
    allowed_forms = {form["key"] for form in forms}
    field_options = {field["key"]: field for field in fields}
    unknown_forms = [key for key in body.forms if key not in allowed_forms]
    unknown_fields = [key for key in body.fields if key not in field_options]
    if unknown_forms or unknown_fields:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "invalid_download_selection",
                "message": "包含服务端权限白名单之外的表单或字段",
                "forms": unknown_forms,
                "fields": unknown_fields,
            },
        )
    selected_forms = set(body.forms)
    incompatible = [
        key
        for key in body.fields
        if not selected_forms.intersection(field_options[key]["form_keys"])
    ]
    if incompatible:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "field_form_mismatch",
                "message": "所选字段不属于已勾选表单",
                "fields": incompatible,
            },
        )
    record_access_log(
        ctx,
        "maintenance_project_batch_download",
        "maintenance",
        {"forms": body.forms, "field_count": len(body.fields)},
    )
    try:
        content, row_count = project_export.build_project_export(
            db,
            user_ctx=ctx,
            field_keys=body.fields,
            q_text=body.q,
            lifecycle=body.lifecycle,
            card_status=body.card_status,
            sort=body.sort,
            allowed_project_ids=(
                maintenance_project_assignments.maintenance_scope_project_ids(
                    db, ctx
                )
            ),
        )
    except project_export.ProjectExportError as exc:
        code = "download_too_large" if isinstance(
            exc, project_export.ProjectExportTooLarge
        ) else "invalid_download_selection"
        http_status = (
            status.HTTP_403_FORBIDDEN
            if isinstance(exc, project_export.ForbiddenProjectExportField)
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(
            http_status,
            {"code": code, "message": str(exc)},
        ) from exc
    except board.BoardSortNotPermitted as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"code": "sort_requires_cost_permission", "message": str(exc)},
        ) from exc
    except board.BoardCostContractNotPermitted as exc:
        # lifecycle=payment_complete 等由合同财务数据推得的筛选需要 data_profit。
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "cost_contract_permission_required",
                "message": "成本及合同财务数据权限",
            },
        ) from exc
    stamp = business_today().strftime("%Y%m%d")
    ascii_name = f"maintenance-projects-{stamp}.xlsx"
    utf8_name = quote(f"维保项目清单-{stamp}.xlsx")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{utf8_name}"
            ),
            "X-Export-Row-Count": str(row_count),
        },
    )
