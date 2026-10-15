"""人工命令可组合进调用方事务，不会提前提交或回滚外层工作。"""
from sqlalchemy import select

from app.models.dimensions import DimPart
from app.models.master_data import Brand, ProductCategory, ProductSpec
from app.models.system import SysAuditLog
from app.services.master_edit import create_part_in_transaction, edit_part_in_transaction
from app.services import master_edit
import pytest


def test_create_and_edit_share_caller_rollback(db):
    created = create_part_in_transaction(
        db, pn_std="TX-ROLLBACK", description="600GB SAS HDD", brand="RollbackBrand",
        category_major="回滚品类", force=True, operated_by="real-user")
    edited = edit_part_in_transaction(
        db, part_id=created["id"], pn_std="TX-ROLLBACK",
        updates={"description": "1.2TB SAS HDD"}, operated_by="real-user")
    assert edited["id"] == created["id"]
    db.rollback()
    assert db.scalar(select(DimPart.id).where(DimPart.pn_std == "TX-ROLLBACK")) is None
    assert db.scalar(select(Brand.id).where(Brand.normalized_name == "RollbackBrand")) is None
    assert db.scalar(select(ProductCategory.id).where(
        ProductCategory.category_major == "回滚品类")) is None
    assert db.scalar(select(ProductSpec.id)) is None
    assert db.scalar(select(SysAuditLog.id)) is None


def test_rejected_core_command_does_not_rollback_callers_work(db):
    create_part_in_transaction(db, pn_std="TX-KEPT", force=True)
    with pytest.raises(master_edit.MasterEditError):
        create_part_in_transaction(db, pn_std="TX-KEPT", force=True)
    db.commit()
    assert db.scalar(select(DimPart.id).where(DimPart.pn_std == "TX-KEPT")) is not None
