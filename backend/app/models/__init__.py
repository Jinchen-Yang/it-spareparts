"""导入所有模型，确保 Base.metadata 完整（供 Alembic autogenerate）。"""
from app.models.chat import ChatMessage, ChatSession
from app.models.dimensions import DimCustomer, DimPart, DimSupplier, PartAlias
from app.models.inquiry import FPartInquiry
from app.models.inventory import Inventory, PartSubstitute
from app.models.maintenance import FMaintenanceLine, FMaintenanceOrder
from app.models.master_data import (
    Brand,
    ProductCategory,
    ProductDataQualityIssue,
    ProductMatchCandidate,
    ProductMergeLog,
    ProductSpec,
)
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder
from app.models.system import (
    SysAuditLog,
    SysImportBatch,
    SysImportError,
    SysImportJob,
    SysRawFile,
    SysRoleTemplate,
    SysUser,
)

__all__ = [
    "ChatSession",
    "ChatMessage",
    "DimPart",
    "PartAlias",
    "DimSupplier",
    "DimCustomer",
    "Brand",
    "ProductCategory",
    "ProductSpec",
    "ProductMatchCandidate",
    "ProductMergeLog",
    "ProductDataQualityIssue",
    "FPurchaseOrder",
    "FPurchaseLine",
    "FMaintenanceOrder",
    "FMaintenanceLine",
    "FSalesOrder",
    "FSalesLine",
    "Inventory",
    "PartSubstitute",
    "FPartInquiry",
    "SysImportBatch",
    "SysImportError",
    "SysImportJob",
    "SysRawFile",
    "SysAuditLog",
    "SysUser",
    "SysRoleTemplate",
]
