"""系统业务设置：采购、销售与项目维保的统一双税展示口径。"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth import current_identity, require_admin
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


# ── DSH 企业定制：LLM 配置下发（P6）────────────────────────────────────────
# dsh-itdata-config 插件启动/轮询本端点，把企业唯一模型写入 DSH 的
# llm-pi-ai + agent-default-model settings（取消用户自定义模型）。
# 仅 admin 可读；apiKey 由 DSH 侧按 apiKeyEnv 引用，不落 DSH settings 明文。


class DshLlmConfig(BaseModel):
    enabled: bool
    provider_id: str
    display_name: str
    api: str = "openai-completions"
    base_url: str
    api_key: str = ""
    api_key_env: str
    models: list[dict]
    default_model: str
    default_context_window: int | None = None
    default_max_tokens: int | None = None


def _require_dsh_config_access(request: Request) -> None:
    """机器对机密钥（X-DSH-Config-Token）或 admin Bearer 二选一。

    不用 require_admin/current_identity 依赖：它们在匿名请求上先抛 401，
    会挡掉纯机器密钥通道。这里手动验签 + 角色判断。
    """
    from app import auth as _auth
    token = get_settings().dsh_config_token.strip()
    if token:
        if request.headers.get("x-dsh-config-token") == token:
            return
    authz = request.headers.get("authorization", "")
    if authz.startswith("Bearer "):
        try:
            data = _auth.verify_token(authz[7:])
            if data.get("role") == "admin":
                return
        except Exception:
            pass
    if token:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无效的配置密钥")
    raise HTTPException(status.HTTP_403_FORBIDDEN, "仅管理员可读取 LLM 配置")


@router.get("/dsh-llm-config", response_model=DshLlmConfig,
            dependencies=[Depends(_require_dsh_config_access)])
def get_dsh_llm_config() -> DshLlmConfig:
    s = get_settings()
    if not s.llm_api_key:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED,
                            "企业 LLM 未配置（后端 .env 的 LLM_API_KEY 为空）")
    provider_id = "enterprise-llm"
    api_key_env = "DSH_ENTERPRISE_LLM_KEY"
    models = [{
        "id": s.llm_model,
        "name": s.llm_model,
        # None 不能下发：pi-ai 的 settings 校验会拒绝 null maxTokens/contextWindow
        **({"contextWindow": getattr(s, "llm_context_window", None)} if getattr(s, "llm_context_window", None) else {}),
        **({"maxTokens": s.llm_max_tokens} if s.llm_max_tokens else {}),
    }]
    return DshLlmConfig(
        enabled=True,
        provider_id=provider_id,
        display_name="企业统一模型",
        api="openai-completions",
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        api_key_env=api_key_env,
        models=models,
        default_model=s.llm_model,
        default_context_window=getattr(s, "llm_context_window", None),
        default_max_tokens=s.llm_max_tokens,
    )
