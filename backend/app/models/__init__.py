"""导入所有模型，确保 Base.metadata 完整（供 Alembic autogenerate）。"""
from app.models.chat import ChatMessage, ChatSession
from app.models.data_quality import FactDataQualityIssue
from app.models.dimensions import DimCustomer, DimPart, DimSupplier, PartAlias
from app.models.inquiry import FPartInquiry
from app.models.inventory import Inventory, PartSubstitute
from app.models.maintenance import (
    FMaintenanceLine,
    FMaintenanceOrder,
    FProjectExpense,
    MaintenanceContractWorkbookState,
    MaintenanceManualCostOverride,
    MaintenanceRoundtripOperation,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAuditLog,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
)
from app.models.maintenance_project_operations import (
    MaintenanceCollectionSnapshot,
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookOperation,
    MaintenanceProjectWorkbookState,
    MaintenanceProjectWorkbookValidation,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueLine,
)
from app.models.maintenance_manager import (
    BusinessFile,
    BusinessFileDownloadAudit,
    BusinessFileLink,
    MaintenanceAcceptanceOperation,
    MaintenanceAcceptanceDeliverable,
    MaintenanceCollectionMilestone,
    MaintenanceManagerUploadBatch,
    MaintenanceManagerUploadBatchProject,
    MaintenanceServicePeriod,
)
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
    SysBusinessSetting,
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
    "FactDataQualityIssue",
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
    "FProjectExpense",
    "MaintenanceContractWorkbookState",
    "MaintenanceManualCostOverride",
    "MaintenanceRoundtripOperation",
    "MaintenanceProject",
    "MaintenanceProjectAuditLog",
    "MaintenanceProjectContract",
    "MaintenanceProjectUserAssignment",
    "MaintenanceCollectionSnapshot",
    "MaintenanceProjectExpenseAttribution",
    "MaintenanceProjectOperationAudit",
    "MaintenanceProjectWorkbookOperation",
    "MaintenanceProjectWorkbookState",
    "MaintenanceProjectWorkbookValidation",
    "MaintenanceSiteIssue",
    "MaintenanceSiteIssueLine",
    "MaintenanceManagerUploadBatch",
    "MaintenanceManagerUploadBatchProject",
    "MaintenanceServicePeriod",
    "MaintenanceCollectionMilestone",
    "MaintenanceAcceptanceDeliverable",
    "BusinessFile",
    "BusinessFileDownloadAudit",
    "BusinessFileLink",
    "MaintenanceAcceptanceOperation",
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
    "SysBusinessSetting",
    "SysUser",
    "SysRoleTemplate",
]
