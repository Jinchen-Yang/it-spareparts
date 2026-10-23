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
    MaintenanceDemandDeleteEvent,
    MaintenanceDemandDeleteIntent,
    MaintenanceDemandDeleteIntentItem,
    MaintenanceDemandTombstone,
    MaintenanceContractWorkbookState,
    MaintenanceManualCostOverride,
    MaintenanceRoundtripOperation,
)
from app.models.maintenance_project import (
    MaintenanceProject,
    MaintenanceProjectAlias,
    MaintenanceProjectAuditLog,
    MaintenanceProjectContract,
    MaintenanceProjectUserAssignment,
    MaintenanceProjectXsdd,
)
from app.models.maintenance_contract_remediation import (
    MaintenanceContractAmountRemediationEntry,
    MaintenanceContractAmountRemediationRun,
)
from app.models.maintenance_bad_return import (
    MaintenanceBadReturn,
    MaintenanceBadReturnCommand,
    MaintenanceBadReturnLine,
    MaintenanceReturnObligation,
)
from app.models.maintenance_source_assignment import MaintenanceSourceOrderAssignment
from app.models.maintenance_project_operations import (
    MaintenanceCollectionReceipt,
    MaintenanceCollectionSnapshot,
    MaintenanceProjectExpenseAttribution,
    MaintenanceProjectOperationAudit,
    MaintenanceProjectWorkbookOperation,
    MaintenanceProjectWorkbookState,
    MaintenanceProjectWorkbookValidation,
    MaintenanceSiteIssue,
    MaintenanceSiteIssueCommand,
    MaintenanceSiteIssueDeliverySource,
    MaintenanceSiteIssueLine,
    MaintenanceSiteIssueReturnEvent,
)
from app.models.maintenance_manager import (
    BusinessFile,
    BusinessFileDownloadAudit,
    BusinessFileLink,
    MaintenanceAcceptanceOperation,
    MaintenanceAcceptanceDeliverable,
    MaintenanceCollectionMilestone,
    MaintenanceCollectionMilestoneOperation,
    MaintenanceCollectionPlanImportBatch,
    MaintenanceCollectionPlanSourceBinding,
    MaintenanceManagerUploadBatch,
    MaintenanceManagerUploadBatchProject,
    MaintenanceServicePeriod,
)
from app.models.maintenance_warehouse import (
    MaintenanceWarehouseAmbiguity,
    MaintenanceWarehouseAuditEvent,
    MaintenanceWarehouseDocument,
    MaintenanceWarehouseDocumentLine,
    MaintenanceWarehouseDocumentLink,
    MaintenanceWarehouseImportBatch,
)
from app.models.maintenance_migration import (
    MaintenanceHistoricalCostBaseline,
    MaintenanceInventoryOpeningBalance,
    MaintenanceMigrationDiscrepancy,
    MaintenanceMigrationEvent,
    MaintenanceMigrationRun,
    MaintenanceProjectCutoverPlan,
)
from app.models.maintenance_ledger import (
    MaintenanceLedgerContractRow,
    MaintenanceLedgerExpenseRow,
    MaintenanceLedgerImportBatch,
    MaintenanceLedgerPlanRow,
)
from app.models.maintenance_front_stock import (
    MaintenanceFrontStock,
    MaintenanceFrontStockLedger,
)
from app.models.maintenance_ckd_import import (
    MaintenanceCkdHeadRow,
    MaintenanceCkdImportBatch,
    MaintenanceCkdLineRow,
)
from app.models.maintenance_doc_import import (
    MaintenanceDocHeadRow,
    MaintenanceDocImportBatch,
    MaintenanceDocLineRow,
    MaintenanceRkdReturnLine,
)
from app.models.maintenance_wbdd_import import MaintenanceWbddImportReceipt
from app.models.maintenance_acceptance_checklist import (
    MaintenanceAcceptanceChecklistBatch,
    MaintenanceAcceptanceChecklistItem,
)
from app.models.maintenance_bad_salvage import MaintenanceBadSalvage
from app.models.maintenance_collection_evidence import (
    MaintenanceCollectionEvidence,
)
from app.models.maintenance_ai_fallback import MaintenanceAiMappingProposal
from app.models.master_data import (
    Brand,
    ProductCategory,
    ProductDataQualityIssue,
    ProductMatchCandidate,
    ProductMergeLog,
    ProductSpec,
)
from app.models.purchase import FPurchaseLine, FPurchaseOrder
from app.models.sales import FSalesLine, FSalesOrder, FSalesReconciliationSnapshot
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
from app.models.replenishment import (
    ReplenishmentApplication,
    ReplenishmentApplicationLine,
    ReplenishmentApplicationVersion,
    ReplenishmentAuditEvent,
    ReplenishmentReview,
    ReplenishmentReviewLine,
    ReplenishmentCartDraft,
    ReplenishmentCartDraftLine,
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
    "MaintenanceDemandDeleteIntent",
    "MaintenanceDemandDeleteIntentItem",
    "MaintenanceDemandTombstone",
    "MaintenanceDemandDeleteEvent",
    "FProjectExpense",
    "MaintenanceContractWorkbookState",
    "MaintenanceManualCostOverride",
    "MaintenanceRoundtripOperation",
    "MaintenanceProject",
    "MaintenanceProjectAuditLog",
    "MaintenanceProjectContract",
    "MaintenanceProjectUserAssignment",
    "MaintenanceContractAmountRemediationRun",
    "MaintenanceContractAmountRemediationEntry",
    "MaintenanceReturnObligation",
    "MaintenanceBadReturn",
    "MaintenanceBadReturnLine",
    "MaintenanceBadReturnCommand",
    "MaintenanceSourceOrderAssignment",
    "MaintenanceCollectionReceipt",
    "MaintenanceCollectionSnapshot",
    "MaintenanceProjectExpenseAttribution",
    "MaintenanceProjectOperationAudit",
    "MaintenanceProjectWorkbookOperation",
    "MaintenanceProjectWorkbookState",
    "MaintenanceProjectWorkbookValidation",
    "MaintenanceSiteIssue",
    "MaintenanceSiteIssueCommand",
    "MaintenanceSiteIssueDeliverySource",
    "MaintenanceSiteIssueLine",
    "MaintenanceSiteIssueReturnEvent",
    "MaintenanceManagerUploadBatch",
    "MaintenanceManagerUploadBatchProject",
    "MaintenanceServicePeriod",
    "MaintenanceCollectionMilestone",
    "MaintenanceCollectionMilestoneOperation",
    "MaintenanceCollectionPlanImportBatch",
    "MaintenanceCollectionPlanSourceBinding",
    "MaintenanceAcceptanceDeliverable",
    "BusinessFile",
    "BusinessFileDownloadAudit",
    "BusinessFileLink",
    "MaintenanceAcceptanceOperation",
    "MaintenanceWarehouseImportBatch",
    "MaintenanceWarehouseDocument",
    "MaintenanceWarehouseDocumentLine",
    "MaintenanceWarehouseDocumentLink",
    "MaintenanceWarehouseAmbiguity",
    "MaintenanceWarehouseAuditEvent",
    "MaintenanceMigrationRun",
    "MaintenanceProjectCutoverPlan",
    "MaintenanceHistoricalCostBaseline",
    "MaintenanceInventoryOpeningBalance",
    "MaintenanceMigrationDiscrepancy",
    "MaintenanceMigrationEvent",
    "MaintenanceLedgerImportBatch",
    "MaintenanceLedgerContractRow",
    "MaintenanceLedgerPlanRow",
    "MaintenanceLedgerExpenseRow",
    "MaintenanceFrontStock",
    "MaintenanceFrontStockLedger",
    "MaintenanceCkdImportBatch",
    "MaintenanceCkdHeadRow",
    "MaintenanceCkdLineRow",
    "MaintenanceDocImportBatch",
    "MaintenanceWbddImportReceipt",
    "MaintenanceDocHeadRow",
    "MaintenanceDocLineRow",
    "MaintenanceAiMappingProposal",
    "FSalesOrder",
    "FSalesLine",
    "FSalesReconciliationSnapshot",
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
    "ReplenishmentApplication",
    "ReplenishmentApplicationVersion",
    "ReplenishmentApplicationLine",
    "ReplenishmentReview",
    "ReplenishmentReviewLine",
    "ReplenishmentAuditEvent",
    "ReplenishmentCartDraft",
    "ReplenishmentCartDraftLine",
]

# Shared PN module owns its persistence; import only for metadata registration.
from app.pn_catalog.models import AuditEvent as PnCatalogAuditEvent, CommandReceipt as PnCatalogReceipt, PartState as PnCatalogState  # noqa: E501,F401
from app.pn_catalog.content_models import ConditionContent, ContentCandidate  # noqa: F401
from app.pn_catalog.media_models import (  # noqa: F401
    Asset, GalleryGroup, GalleryMember, GalleryEntry, GallerySuppression, Derivative,
    UploadTicket, MultipartUpload, InboxGroup, InboxItem,
)
from app.pn_catalog.job_models import CatalogJob, CatalogJobEvent  # noqa: F401
from app.pn_catalog.migration_models import LegacySourceRecord, MigrationBatch  # noqa: F401
