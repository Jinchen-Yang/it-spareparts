"""系统业务设置：维保合同级毛利默认展示口径。"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth import current_identity, require_admin
from app.db import get_db
from app.models.system import SysBusinessSetting
from app.security import require_page
from app.services import system_settings

router = APIRouter(prefix="/system-settings", tags=["system-settings"])

_maintenance_page_gate = require_page("page_maintenance")


def _read_gate(
    ident: dict = Depends(current_identity),
    _: None = Depends(_maintenance_page_gate),
) -> dict:
    """严格登录校验 + 维保页面权限，两层都通过才可读取默认值。"""

    return ident


class SystemSettingsView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    maintenance_project_profit_default_basis: Literal["inc", "ex", "both"]
    version: int
    updated_by: str | None = None
    updated_at: datetime | None = None


class SystemSettingsUpdate(BaseModel):
    maintenance_project_profit_default_basis: Literal["inc", "ex", "both"]
    expected_version: int = Field(ge=1)


def _view(
    setting: SysBusinessSetting,
    *,
    include_audit_identity: bool = True,
) -> SystemSettingsView:
    view = SystemSettingsView.model_validate(setting)
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
        setting = system_settings.update_maintenance_project_profit_default_basis(
            db,
            basis=body.maintenance_project_profit_default_basis,
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
