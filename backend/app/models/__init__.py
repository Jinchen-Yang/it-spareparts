"""导入所有模型，确保 Base.metadata 完整（供 Alembic autogenerate）。"""
from app.models.dimensions import DimCustomer, DimPart, DimSupplier, PartAlias
from app.models.inquiry import FPartInquiry
from app.models.inventory import Inventory, PartSubstitute
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import (
    SysAuditLog,
    SysImportBatch,
    SysImportError,
    SysRawFile,
)

__all__ = [
    "DimPart",
    "PartAlias",
    "DimSupplier",
    "DimCustomer",
    "FPurchaseOrder",
    "FPurchaseLine",
    "FSalesOrder",
    "FSalesLine",
    "Inventory",
    "PartSubstitute",
    "FPartInquiry",
    "SysImportBatch",
    "SysImportError",
    "SysRawFile",
    "SysAuditLog",
]
