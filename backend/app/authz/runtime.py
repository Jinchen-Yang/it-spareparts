"""Resource activation is persistent; removing configuration suspends access."""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.authz.catalog import CATALOG_VERSION, RESOURCE_DEFINITIONS, SALES_RESOURCE, resource_descriptors, validate_catalog
from app.authz.errors import authz_error
from app.config import get_settings


@dataclass(frozen=True)
class RuntimeState:
    migration_state: str
    runtime_state: str
    configurable: bool


def authorization_mode() -> str:
    return get_settings().authorization_mode


def _configuration() -> tuple[set[str], set[str]]:
    try:
        validate_catalog()
    except (KeyError, TypeError, ValueError):
        raise authz_error(503, "AUTHZ_CATALOG_INVALID", "授权目录与执行定义不一致") from None
    settings = get_settings()
    enabled = settings.authz_enabled_resources
    suspended = settings.authz_suspended_resources
    if (type(enabled) is not list or type(suspended) is not list
            or not all(type(item) is str for item in enabled + suspended)
            or len(enabled) != len(set(enabled)) or len(suspended) != len(set(suspended))):
        raise authz_error(503, "AUTHZ_CONFIGURATION_INVALID", "授权运行配置包含重复或非法资源")
    enabled, suspended = list(enabled), list(suspended)
    if settings.authz_sales_analysis_enabled:
        enabled.append(SALES_RESOURCE)
    if settings.authz_sales_analysis_suspended:
        suspended.append(SALES_RESOURCE)
    enabled, suspended = set(enabled), set(suspended)
    if not enabled | suspended <= RESOURCE_DEFINITIONS.keys():
        raise authz_error(503, "AUTHZ_CONFIGURATION_INVALID", "不能启用尚未实施或未知的授权资源")
    return enabled, suspended


def activate_configured_resources(bind) -> None:
    """Startup-only operation, in its own committed transaction (never a GET)."""
    from app.models.authorization import SysAuthorizationCutover
    if not isinstance(bind, Engine):
        raise ValueError("授权激活必须使用独立引擎连接，不能复用请求事务")
    enabled, suspended = _configuration()
    # A compatibility process is permitted to start beside an activated one,
    # but cannot activate or serve new resources. Its requests see suspension.
    if authorization_mode() != "unified_modules":
        if enabled or suspended:
            raise authz_error(503, "AUTHZ_CONFIGURATION_INVALID", "启用资源需要统一模块授权模式")
        return
    with Session(bind) as db, db.begin():
        existing = set(db.scalars(select(SysAuthorizationCutover.resource)))
        if existing - RESOURCE_DEFINITIONS.keys():
            raise authz_error(503, "AUTHZ_CATALOG_INVALID", "已切换授权资源缺少执行定义")
        if suspended - enabled - existing:
            raise authz_error(503, "AUTHZ_CONFIGURATION_INVALID", "尚未切换的资源不能仅设置暂停")
        for resource in sorted(enabled):
            db.execute(insert(SysAuthorizationCutover).values(
                resource=resource, activated_catalog_version=CATALOG_VERSION,
            ).on_conflict_do_nothing(index_elements=[SysAuthorizationCutover.resource]))


def runtime_states(db: Session) -> dict[str, RuntimeState]:
    from app.models.authorization import SysAuthorizationCutover
    enabled, suspended = _configuration()
    markers = set(db.scalars(select(SysAuthorizationCutover.resource)))
    if markers - RESOURCE_DEFINITIONS.keys():
        raise authz_error(503, "AUTHZ_CATALOG_INVALID", "已切换授权资源缺少执行定义")
    if enabled - markers or suspended - markers:
        raise authz_error(503, "AUTHZ_NOT_READY", "授权资源启动切换尚未完成")
    unified = authorization_mode() == "unified_modules"
    result = {}
    for descriptor in resource_descriptors():
        resource = descriptor["id"]
        migration = descriptor["migration_state"]
        if resource in markers and migration == "implemented":
            state = "enabled" if unified and resource in enabled and resource not in suspended else "suspended"
        else:
            state = "legacy"
        result[resource] = RuntimeState(migration, state, unified and migration == "implemented" and state != "legacy")
    return result


def is_authoritative(db: Session, resource: str) -> bool:
    states = runtime_states(db)
    if resource not in states:
        raise authz_error(403, "AUTHZ_FORBIDDEN", "未知授权资源")
    return states[resource].runtime_state != "legacy"


def require_configurable(db: Session, resource: str, *, states=None) -> None:
    if authorization_mode() != "unified_modules":
        raise authz_error(409, "AUTHZ_MODE_NOT_UNIFIED", "请在统一模块授权模式下管理权限")
    states = runtime_states(db) if states is None else states
    if resource not in states or not states[resource].configurable:
        raise authz_error(422, "AUTHZ_RESOURCE_NOT_CONFIGURABLE", "该资源当前不可配置")


def canonical_management_required(db: Session) -> bool:
    from app.models.authorization import SysAuthorizationCutover, SysAuthorizationMigrationReceipt
    return (authorization_mode() == "unified_modules"
            or db.scalar(select(SysAuthorizationCutover.resource).limit(1)) is not None
            or db.scalar(select(SysAuthorizationMigrationReceipt.entity_id).where(
                SysAuthorizationMigrationReceipt.migration_key.in_(('authz_s2', 'authz_s3')),
                SysAuthorizationMigrationReceipt.entity_kind == 'artifact_protocol').limit(1)) is not None)


def mark_artifact_protocol_used(bind: Engine, *, protocol: str = 'authz_s2') -> None:
    """Commit the format marker before publishing any unified filesystem artifact.

    Independent of a later request rollback: once new-format bytes may exist,
    withdrawing configuration or downgrading must not restore a legacy reader.
    """
    from app.models.authorization import SysAuthorizationMigrationReceipt
    if not isinstance(bind, Engine) or protocol not in ('authz_s2', 'authz_s3'):
        raise ValueError('产物权威标记必须使用独立引擎事务')
    with Session(bind) as db, db.begin():
        db.execute(insert(SysAuthorizationMigrationReceipt).values(
            migration_key=protocol, entity_kind='artifact_protocol', entity_id='global',
            before_state={}, after_state={'schema_version': 2 if protocol == 'authz_s3' else 1,
                                          'authority': 'unified_modules'},
        ).on_conflict_do_nothing(index_elements=['migration_key', 'entity_kind', 'entity_id']))
