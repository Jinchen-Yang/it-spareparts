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

// ── 回款计划提醒（reminder-only，不产生到账事实）──────────

export const COLLECTION_PAGE = {
  title: "回款提醒",
  subtitle: "按计划月份查看项目回款提醒；标记、改期或重新打开只管理提醒本身，不产生财务事实。",
  /** 固定边界说明：只在详情页头部展示，不得用于指标或按钮。 */
  disclaimer: "已处理仅表示本次提醒已完成，不代表财务确认到账",
  searchLabel: "搜索回款提醒",
  searchPlaceholder: "搜索项目编号、名称或合同编号",
  stateFilterLabel: "提醒状态筛选",
  ownerScopeLabel: "负责人范围",
  ownerScopeAll: "全部项目",
  ownerScopeMe: "我负责的",
  stateAll: "全部",
  importPlan: "导入回款计划",
  viewFullProject: "查看完整项目",
  retry: "重试",
  managerLabel: "维保负责人",
  servicePeriodLabel: "维保期限",
  contractsLabel: "关联合同",
  nextActionLabel: "下一条",
  metricMilestones: "计划节点数",
  metricNeedsReview: "计划有变更",
  metricDueThisMonth: "本月待跟进",
  metricOverdue: "已逾期",
  metricHandled: "已处理",
  colContract: "合同编号",
  colSequence: "期次",
  colPlannedMonth: "计划月份",
  colPlannedAmount: "计划金额",
  colState: "提醒状态",
  colLastOperation: "最近处理记录",
  colActions: "操作",
  actionHandle: "标记已处理",
  actionReschedule: "改期",
  actionReopen: "重新打开",
  incompleteHint: "先补齐计划字段",
  needsReviewHint: "计划有变更，请重新打开后处理",
  emptyDirectory: "当前筛选暂无项目",
  emptyDetail: "选择左侧项目查看回款计划",
  noPlanHint: "当前项目暂无回款计划，请联系管理员导入",
  loadFailed: "回款计划加载失败",
  detailLoadFailed: "详情加载失败，请重试",
  permissionDenied: "当前账号无权查看该项目回款计划",
  versionConflict: "数据已变化，请刷新后重试",
  amountRestricted: "无权限查看",
  /** 目录行没有下一条可跟进节点时的占位文案。 */
  noActionable: "无待办",
  /** 节点期次展示：第 N 期。 */
  sequenceOf: (n: number) => `第 ${n} 期`,
} as const;

export const COLLECTION_STATE_OPTIONS: { label: string; value: string }[] = [
  { label: "全部", value: "all" },
  { label: "计划有变更", value: "needs_review" },
  { label: "信息待补", value: "incomplete" },
  { label: "已逾期", value: "overdue" },
  { label: "本月跟进", value: "due_this_month" },
  { label: "待到期", value: "upcoming" },
  { label: "已处理", value: "handled" },
];

export const COLLECTION_STATE_LABELS: Record<string, { label: string; color?: string }> = {
  needs_review: { label: "计划有变更", color: "purple" },
  handled: { label: "已处理", color: "green" },
  incomplete: { label: "信息待补", color: "orange" },
  overdue: { label: "已逾期", color: "red" },
  due_this_month: { label: "本月跟进", color: "gold" },
  upcoming: { label: "待到期", color: "default" },
};

export const COLLECTION_FOLLOW_UP = {
  handleTitle: "标记已处理",
  rescheduleTitle: "改期",
  reopenTitle: "重新打开",
  noteLabel: "处理说明（可选）",
  notePlaceholder: "填写本次跟进说明（选填）",
  plannedMonthLabel: "新计划月份",
  plannedMonthPlaceholder: "YYYY-MM",
  reasonLabel: "理由",
  reasonRequired: "请填写理由",
  monthRequired: "请选择新计划月份",
  submit: "确认",
  cancel: "取消",
  submitSuccess: "提醒状态已更新",
  submitFailed: "提醒操作失败，请重试",
  versionConflict: "数据已变化，请刷新后重试",
} as const;

/** 节点操作记录的动作标签（last_operation.action → 展示文案）。 */
export const COLLECTION_OPERATION_LABELS: Record<string, string> = {
  handle: "标记已处理",
  reschedule: "改期",
  reopen: "重新打开",
};

export const COLLECTION_IMPORT = {
  title: "导入回款计划",
  stepSelectFile: "选择文件",
  stepPreview: "解析预览",
  stepReviewBindings: "审核绑定",
  stepApply: "确认应用",
  filePickLabel: "选择 .xls 文件",
  fileHint: "上传后先预检、不直接修改计划",
  previewAction: "解析预览",
  repreview: "重新预览",
  nextStep: "下一步",
  prevStep: "上一步",
  complete: "完成",
  filePicked: (name: string, size: number) => `${name}（${size} 字节）`,
  previewZeroWriteHint: "解析预览阶段不写入任何计划数据",
  previewFailed: "解析预览失败，请重试",
  applyFailed: "应用失败，请重试",
  permissionDenied: "无权执行此操作",
  applyResultTitle: "导入结果",
  bindingSearchShortHint: "请输入至少 2 个字符",
  countProjects: "项目数",
  countMilestones: "节点数",
  countBound: "已绑定",
  countPendingBinding: "待绑定",
  countBlockers: "阻断",
  countWarnings: "警告",
  diffCreate: "新增",
  diffUpdate: "更新",
  diffUnchanged: "未变",
  diffSourceMissing: "来源缺失",
  bindingSearchPlaceholder: "搜索项目编号或名称",
  bindingContractPlaceholder: "选择合同",
  bindingProjectLabel: "项目",
  bindingContractLabel: "合同",
  bindingReasonLabel: "改派理由",
  bindingReasonRequired: "请填写改派理由",
  blockerHint: "阻断项未清零前不能应用",
  apply: "确认应用",
  applyDisabledHint: "请先处理阻断项并完成全部绑定",
  countCreated: "新增",
  countUpdated: "更新",
  countUnchanged: "未变",
  countSourceMissing: "来源缺失",
  countNeedsReview: "计划变更待复核",
  expired: "批次已过期，请重新预览",
  versionConflict: "数据已变化，请刷新后重试",
} as const;
