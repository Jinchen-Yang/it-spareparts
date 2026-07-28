"""类型化业务设置的读取与并发安全更新。"""

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.system import SysAuditLog, SysBusinessSetting

ProfitDefaultBasis = Literal["inc", "ex", "both"]


class BusinessSettingMissing(RuntimeError):
    """迁移或种值异常：类型化单例行不存在。"""


class BusinessSettingVersionConflict(RuntimeError):
    """管理员基于旧版本保存，拒绝覆盖较新的设置。"""

    def __init__(self, expected: int, current: int):
        super().__init__(f"expected v{expected}, current v{current}")
        self.expected = expected
        self.current = current


def get_business_setting(
    db: Session,
    *,
    for_update: bool = False,
) -> SysBusinessSetting:
    query = select(SysBusinessSetting).where(SysBusinessSetting.id == 1)
    if for_update:
        query = query.with_for_update()
    setting = db.scalar(query)
    if setting is None:
        raise BusinessSettingMissing(
            "sys_business_setting singleton id=1 is missing; run database migrations",
        )
    return setting


def _snapshot(setting: SysBusinessSetting) -> dict:
    return {
        "maintenance_project_profit_default_basis":
            setting.maintenance_project_profit_default_basis,
        "version": setting.version,
    }


def update_maintenance_project_profit_default_basis(
    db: Session,
    *,
    basis: ProfitDefaultBasis,
    expected_version: int,
    operated_by: str,
) -> SysBusinessSetting:
    """锁住单例并更新。

    本函数只 flush、不 commit：业务行与 ``SysAuditLog`` 由 API 调用方在同一个事务
    中提交。相同值是幂等写，不递增 version，也不制造无意义审计。
    """

    setting = get_business_setting(db, for_update=True)
    if expected_version != setting.version:
        raise BusinessSettingVersionConflict(expected_version, setting.version)
    if basis == setting.maintenance_project_profit_default_basis:
        return setting

    before = _snapshot(setting)
    setting.maintenance_project_profit_default_basis = basis
    setting.version += 1
    setting.updated_by = operated_by
    setting.updated_at = datetime.now(timezone.utc)
    db.add(
        SysAuditLog(
            entity_type="sys_business_setting",
            entity_id=setting.id,
            action="maintenance_profit_basis_update",
            before_json=before,
            after_json=_snapshot(setting),
            reason="仅影响维保合同级毛利的默认展示口径，不改变双口径计算事实",
            operated_by=operated_by,
        ),
    )
    db.flush()
    return setting
