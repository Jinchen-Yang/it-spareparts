"""系统业务设置：采购、销售与项目维保的统一双税展示口径。"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth import current_identity, require_admin
from app.db import get_db
from app.models.system import SysBusinessSetting
from app.services import system_settings

router = APIRouter(prefix="/system-settings", tags=["system-settings"])

def _read_gate(
    ident: dict = Depends(current_identity),
) -> dict:
    """任一真实登录身份可读取统一口径；非管理员的审计身份由响应层隐藏。"""

    return ident


class SystemSettingsView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    maintenance_display_basis: Literal["inc", "ex", "both"]
    purchase_display_basis: Literal["inc", "ex", "both"]
    sales_display_basis: Literal["inc", "ex", "both"]
    version: int
    updated_by: str | None = None
    updated_at: datetime | None = None


class SystemSettingsUpdate(BaseModel):
    maintenance_display_basis: Literal["inc", "ex", "both"]
    purchase_display_basis: Literal["inc", "ex", "both"]
    sales_display_basis: Literal["inc", "ex", "both"]
    expected_version: int = Field(ge=1)


def _view(
    setting: SysBusinessSetting,
    *,
    include_audit_identity: bool = True,
) -> SystemSettingsView:
    view = SystemSettingsView(
        maintenance_display_basis=(
            setting.maintenance_project_profit_default_basis
        ),
        purchase_display_basis=setting.purchase_display_basis,
        sales_display_basis=setting.sales_display_basis,
        version=setting.version,
        updated_by=setting.updated_by,
        updated_at=setting.updated_at,
    )
    if not include_audit_identity:
        view.updated_by = None
        view.updated_at = None
    return view


@router.get(
    "",
    response_model=SystemSettingsView,
    response_model_exclude_none=True,
)
def get_system_settings(
    response: Response,
    db: Session = Depends(get_db),
    ident: dict = Depends(_read_gate),
) -> SystemSettingsView:
    response.headers["Cache-Control"] = "no-store"
    return _view(
        system_settings.get_business_setting(db),
        include_audit_identity=ident.get("role") == "admin",
    )


@router.put("", response_model=SystemSettingsView)
def update_system_settings(
    body: SystemSettingsUpdate,
    db: Session = Depends(get_db),
    ident: dict = Depends(current_identity),
    _: str = Depends(require_admin),
) -> SystemSettingsView:
    try:
        setting = system_settings.update_business_settings(
            db,
            maintenance_basis=body.maintenance_display_basis,
            purchase_basis=body.purchase_display_basis,
            sales_basis=body.sales_display_basis,
            expected_version=body.expected_version,
            operated_by=ident["sub"],
        )
        db.commit()
        db.refresh(setting)
        return _view(setting)
    except system_settings.BusinessSettingVersionConflict as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"设置已被其他管理员改到 v{exc.current}（你基于 v{exc.expected} 编辑），"
            "请刷新后重试",
        ) from exc
    except Exception:
        db.rollback()
        raise
