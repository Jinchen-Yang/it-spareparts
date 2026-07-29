"""类型化业务设置的读取与并发安全更新。"""

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.system import SysAuditLog, SysBusinessSetting

ProfitDefaultBasis = Literal["inc", "ex", "both"]
DisplayBasis = Literal["inc", "ex", "both"]


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
        "purchase_display_basis": setting.purchase_display_basis,
        "sales_display_basis": setting.sales_display_basis,
        "version": setting.version,
    }


def update_business_settings(
    db: Session,
    *,
    maintenance_basis: ProfitDefaultBasis,
    purchase_basis: DisplayBasis,
    sales_basis: DisplayBasis,
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
    if (
        maintenance_basis
        == setting.maintenance_project_profit_default_basis
        and purchase_basis == setting.purchase_display_basis
        and sales_basis == setting.sales_display_basis
    ):
        return setting

    before = _snapshot(setting)
    setting.maintenance_project_profit_default_basis = maintenance_basis
    setting.purchase_display_basis = purchase_basis
    setting.sales_display_basis = sales_basis
    setting.version += 1
    setting.updated_by = operated_by
    setting.updated_at = datetime.now(timezone.utc)
    db.add(
        SysAuditLog(
            entity_type="sys_business_setting",
            entity_id=setting.id,
            action="business_display_basis_update",
            before_json=before,
            after_json=_snapshot(setting),
            reason="统一更新采购、销售、项目维保展示口径，不改变双口径计算事实",
            operated_by=operated_by,
        ),
    )
    db.flush()
    return setting


def update_maintenance_project_profit_default_basis(
    db: Session,
    *,
    basis: ProfitDefaultBasis,
    expected_version: int,
    operated_by: str,
) -> SysBusinessSetting:
    """旧内部调用兼容；保留采购、销售当前值并走同一原子更新实现。"""
    current = get_business_setting(db)
    return update_business_settings(
        db,
        maintenance_basis=basis,
        purchase_basis=current.purchase_display_basis,
        sales_basis=current.sales_display_basis,
        expected_version=expected_version,
        operated_by=operated_by,
    )
