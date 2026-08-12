/**
 * 维保业务文案统一出口。
 *
 * 所有维保页面的按钮、标签、提示、状态、标题必须从这里取文案，
 * 不直接在组件内硬编码中文。新增维保页面也必须走本模块。
 *
 * 规则：
 *   - 新增文案 → 先在本文件添加，再在组件中使用
 *   - 废弃文案 → 删除并搜索引用确保无残留
 *   - 术语映射 → 在本文件集中管理，不散落在各组件中
 */

// ── 全局术语替换 ──────────────────────────────────────────

/** 数据库字段/技术术语 → 业务展示文案 */
export const TERM = {
  /** 项目经理/项目负责人 → 维保负责人 */
  projectManager: "维保负责人",
  /** PN (Part Number) → 备件型号 */
  pn: "备件型号",
  /** 不含税 → 未税 */
  exTax: "未税",
  /** 含税 → 含税 */
  incTax: "含税",
  /** 单位成本 → 单价 */
  unitCost: "单价",
  /** 明细 → 记录 */
  detail: "记录",
  /** 回填 → 补录 */
  backfill: "补录",
  /** 取价 → 匹配价格 */
  priceMatch: "匹配价格",
  /** 映射 → 确认 */
  mapping: "确认",
  /** 不可见 → 无查看权限 */
  hidden: "无权限查看",
  /** dry-run → 预检不保存 */
  dryRun: "预检，不保存",
  /** manifest → 技术依据 */
  manifest: "技术依据",
  /** 协议版本 → 版本 */
  protocolVersion: "版本",
  /** 原子应用 → 确认导入 */
  atomicApply: "确认导入",
  /** 稳定 ID → 数据标识 */
  stableId: "数据标识",
} as const;

// ── 生命周期状态 ──────────────────────────────────────────

export const LIFECYCLE_LABELS: Record<string, { label: string; color?: string }> = {
  ongoing: { label: "服务中", color: "blue" },
  ended: { label: "已结项" },
  missing: { label: "期限待确认", color: "orange" },
};

// ── 成本水位线 ────────────────────────────────────────────

export const COST_STATUS_LABELS: Record<string, string> = {
  normal: "成本正常",
  yellow: "成本偏高",
  red: "已超合同额",
  unknown: "部分成本待补充",
  restricted: "暂无成本数据",
};

// ── 成本状态标签 ──────────────────────────────────────────

export const COST_LINE_STATUS_LABELS: Record<string, { label: string; color: string }> = {
  missing: { label: "缺成本", color: "orange" },
  restricted: { label: "无权限", color: "default" },
  not_counted: { label: "未纳入核算", color: "default" },
  available: { label: "已核算", color: "green" },
};

// ── 合同标签 ──────────────────────────────────────────────

export const CONTRACT_LABELS = {
  includedEffective: "有效合同",
  excluded: "历史合同（已失效）",
  notCurrent: "已过期",
  statusUnmapped: "合同状态待确认",
  amountMissing: "合同金额未填写",
  amountRestricted: "金额无权限查看",
  basisUnknown: "合同额税类型未确认",
  basisIncTax: "合同额（含税）",
} as const;

// ── 工作簿四表名称 ────────────────────────────────────────

export const WORKBOOK_SHEET_NAMES: Record<string, string> = {
  overview: "项目总览（合同与回款）",
  site_requisitions: "备件领用明细",
  approved_expenses: "费用报销明细",
  manager_tracking: "项目待办与追踪",
};

// ── 工作流五阶段 ──────────────────────────────────────────

export const WORKFLOW_PHASES = [
  { key: "contracts", label: "合同与回款", order: 1 },
  { key: "procurement", label: "采购与备件", order: 2 },
  { key: "requisitions", label: "领用与返还", order: 3 },
  { key: "costs", label: "成本与费用", order: 4 },
  { key: "acceptance", label: "验收与结项", order: 5 },
] as const;

// ── 业务操作按钮 ──────────────────────────────────────────

export const ACTIONS = {
  openProject: "进入项目",
  updateMonth: "更新本月进展",
  registerRequisition: "登记现场领用",
  followReturn: "跟进坏件返还",
  fillMissingCost: "补充缺失成本",
  submitAcceptance: "提交验收资料",
  assignManager: "指派负责人",
  importTritium: "同步项目资料",
  matchPrices: "自动匹配价格",
} as const;

// ── 页面标题 ──────────────────────────────────────────────

export const PAGE_TITLES = {
  workDashboard: "我的待办",
  projectsOverview: "项目总览",
  projectDetail: "项目详情",
  monthlyUpdate: "月度项目更新",
  acceptance: "验收与结项",
  adminProjectMaster: "项目资料同步",
  adminDemands: "异常维保单处理",
  adminWarehouse: "仓库单据核对",
  adminSourceOrders: "历史单据归属",
  adminMigration: "历史数据迁移核对",
  adminCostRefill: "领用缺价补录",
} as const;

// ── 状态提示 ──────────────────────────────────────────────

export const HINTS = {
  noProjectAccess: "暂无项目数据",
  tritiumPending: "项目资料来源于氚云，如需新建项目请联系管理员",
  warehousePending: "仓库数据尚未接入，相关功能暂不可用",
  noProcurementLink: "尚未找到关联采购订单",
  costIncomplete: (n: number) => `有 ${n} 条领用暂缺成本`,
  dataAsOf: (date: string) => `数据更新至 ${date}`,
  viewTechnicalDetails: "查看数据依据",
} as const;
