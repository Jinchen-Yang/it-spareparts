"""系统业务设置：采购、销售与项目维保的统一双税展示口径。"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth import current_identity, require_admin, verify_token_db
from app.config import get_settings
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


# ---------- DSH 企业助手：唯一模型配置下发（itdata-config 插件轮询） ----------
_bearer_optional = HTTPBearer(auto_error=False)


def _dsh_config_gate(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
    db: Session = Depends(get_db),
) -> str:
    """机器密钥（x-dsh-config-token == DSH_CONFIG_TOKEN，非空时）或 admin token 二者其一。"""
    settings = get_settings()
    header = request.headers.get("x-dsh-config-token", "")
    if settings.dsh_config_token and header and header == settings.dsh_config_token:
        return "machine"
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少凭证")
    data = verify_token_db(creds.credentials, db)
    if data.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限或机器密钥")
    return "admin"


@router.get("/dsh-llm-config")
def dsh_llm_config(response: Response, _: str = Depends(_dsh_config_gate)) -> dict:
    """企业唯一模型配置（来源：后端 .env 的 LLM_* 配置）。密钥随响应下发给 DSH 宿主写入其 credentials，
    仅机器密钥/管理员可读；DSH 侧不落 settings 明文。"""
    settings = get_settings()
    response.headers["Cache-Control"] = "no-store"
    return {
        "enabled": bool(settings.llm_api_key),
        "provider_id": "enterprise-llm",
        "display_name": settings.dsh_llm_display_name,
        "api": "openai-completions",
        "base_url": settings.llm_base_url,
        "default_model": settings.llm_model,
        "api_key": settings.llm_api_key,
        "api_key_env": "DSH_ENTERPRISE_LLM_KEY",
        "reasoning": "off",
        "models": [{"id": settings.llm_model, "name": settings.llm_model}],
    }
