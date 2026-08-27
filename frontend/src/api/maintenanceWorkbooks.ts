import { api } from "../api";

/**
 * 回填类下载/上传 API（页面重设计 R2；口径 REQUIREMENTS #38/#40）。
 *
 * 三处下载点各配一个上传点，**在哪下载就在哪上传**：
 * ①主页全局备件行级表 ②项目总表六 sheet ③各 tab 单 sheet（同 ② 带 sheets 参数）。
 *
 * 原始单据（需求单/发货/返库/入库/台账）不走这里——那条路仍是 admin
 * 「数据中心 → 数据导入」（#42），本模块只管系统回填后的**改价/报销/回款**回传。
 */
export type RangePreset = "today" | "yesterday" | "this_week" | "this_month" | "custom";

export const RANGE_LABELS: Record<RangePreset, string> = {
  today: "今天",
  yesterday: "昨天",
  this_week: "本周",
  this_month: "本月",
  custom: "自定义",
};

/** 表 6 六 sheet 名（后端 ALL_SHEETS 的镜像，顺序一致）。 */
export const SHEETS = {
  basics: "01_项目基础信息",
  overview: "02_概览数据",
  parts: "03_备件订单",
  expense: "04_报销订单",
  collection: "05_项目经理回款单",
  site: "06_现场领用与返还",
} as const;

export interface WorkbookApplyResult {
  project_id?: string;
  applied_by?: string;
  import_batch_id?: string;
  sheets?: string[];
  cost_refills: number;
  site_return_flags: number;
  expense_updates: number;
  expense_creates?: number;
  expense_voids?: number;
  site_creates?: number;
  site_voids?: number;
  site_updates?: number;
  cost_overrides?: number;
  line_creates?: number;
  line_updates?: number;
  qty_updates?: number;
  line_voids?: number;
  order_reassignments?: number;
  contract_updates?: number;
  plan_creates?: number;
  plan_updates?: number;
  plan_voids?: number;
  collection_creates: number;
  collection_updates?: number;
  collection_voids: number;
}

/** 总表 2.1 回传预检（#265 契约三）：在 apply 结果字段之外带回作废预览清单。 */
export interface WillVoidRow {
  sheet?: string;
  row_token?: string;
  order_no?: string;
  reason?: string;
  [key: string]: unknown; // 宽松解析：后端字段调整不阻塞前端
}

export interface WillReassignOrder {
  source_order_id?: string;
  order_no?: string;
  from_project_id?: string | null;
  from_project_name?: string | null;
  to_project_id?: string;
}

export interface WorkbookValidateResult extends Partial<WorkbookApplyResult> {
  will_void_rows?: WillVoidRow[];
  will_reassign_orders?: WillReassignOrder[];
  [key: string]: unknown;
}

const BASE = "/maintenance";

async function download(path: string, params?: Record<string, unknown>): Promise<Blob> {
  const resp = await api.get<Blob>(path, { params, responseType: "blob" });
  return resp.data;
}

async function upload(path: string, file: File): Promise<WorkbookApplyResult> {
  const body = new FormData();
  body.append("file", file);
  const resp = await api.post<WorkbookApplyResult>(path, body);
  return resp.data;
}

/** ①主页全局：全项目备件行级表（系统自动回填价之后）。 */
export const downloadSparePartLines = (params: {
  range: RangePreset;
  from?: string;
  to?: string;
}) => download(`${BASE}/spare-part-lines.xlsx`, params);

export const validateSparePartLines = async (file: File) => {
  const body = new FormData();
  body.append("file", file);
  const resp = await api.post<WorkbookValidateResult>(
    `${BASE}/spare-part-lines/validate`,
    body,
  );
  return resp.data;
};

export const applySparePartLines = (file: File) =>
  upload(`${BASE}/spare-part-lines/apply`, file);

/** ②项目总表（六 sheet）／③各 tab 单 sheet（传 sheets）。 */
export const downloadProjectMaster = (projectId: string, sheets?: string[]) =>
  download(`${BASE}/projects/stable/${encodeURIComponent(projectId)}/master-workbook.xlsx`,
    sheets && sheets.length ? { sheets: sheets.join(",") } : undefined);

export const validateProjectMaster = async (projectId: string, file: File) => {
  const body = new FormData();
  body.append("file", file);
  const resp = await api.post<WorkbookValidateResult>(
    `${BASE}/projects/stable/${encodeURIComponent(projectId)}/master-workbook/validate`,
    body,
  );
  return resp.data;
};

export const applyProjectMaster = (projectId: string, file: File) =>
  upload(`${BASE}/projects/stable/${encodeURIComponent(projectId)}/master-workbook/apply`,
    file);

/** 项目面板「报销」tab 的只读行（含备注，#47）。改动仍只走下载→改→上传覆盖。 */
export interface ProjectExpenseRow {
  raw_line_id: string;
  bxd_no: string | null;
  expense_date: string | null;
  person: string | null;
  expense_type: string | null;
  fee_category: string | null;
  reason: string | null;
  contract_no: string | null;
  amount_ex_tax: string | null;
  amount_inc_tax: string | null;
  data_status: string | null;
  remark: string | null;
}

export const listProjectExpenseRows = async (projectId: string) => {
  const pageSize = 200;
  const rows: ProjectExpenseRow[] = [];
  let page = 1;
  let total = 0;

  // 项目报销并非天然少于一页。必须把服务端分页完整拉取，否则表格与下载的
  // 04 sheet 会使用不同母集，人工会误以为后半段报销“上传后没生效”。
  do {
    const resp = await api.get<{
      rows: ProjectExpenseRow[];
      total: number;
      page: number;
      page_size: number;
    }>(`${BASE}/projects/stable/${encodeURIComponent(projectId)}/expense-rows`, {
      params: { page, page_size: pageSize },
    });
    total = resp.data.total;
    rows.push(...resp.data.rows);
    if (!resp.data.rows.length) break;
    page += 1;
  } while (rows.length < total);

  return { rows, total };
};

/** 03 备件明细行级（PN）只读数据源——兼容 V1/V2 协议。 */
export interface ProjectPartsRow {
  line_id: number;
  part_id?: number | null;
  order_no: string | null;
  order_date: string | null;
  sales_order_no?: string | null;
  project_raw?: string | null;
  pn_std: string | null;
  description: string | null;
  qty: number | string | null;
  return_qty?: number | string | null;
  serial_numbers?: string | null;
  warehouse: string | null;
  cost_source: string | null;
  cost_source_label?: string | null;
  cost_amount_inc_tax?: string | null;
  confidence?: "high" | "medium" | "low" | "none" | null;
  unit_cost_ex_tax: number | string | null;
  unit_cost_inc_tax: number | string | null;
  change_reason?: string | null;
  manual_unit_cost_ex_tax?: string | null;
  manual_reason?: string | null;
  missing_kind?: "out_of_scope" | "none" | null;
  can_refill?: boolean;
}

export const listProjectPartsRows = async (projectId: string) => {
  const resp = await api.get<{ sheet: string; total: number; rows: ProjectPartsRow[] }>(
    `${BASE}/projects/stable/${encodeURIComponent(projectId)}/master-workbook/rows`,
    { params: { sheet: SHEETS.parts } },
  );
  return resp.data;
};

/** 浏览器落盘：后端返回的是 attachment，这里只负责触发保存。 */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Safari/WebKit 可能在 click 返回后才真正读取 object URL。同步 revoke 会出现
  // 后端已 200、浏览器却报下载失败或落下空文件的竞态。
  globalThis.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

/** 回款计划行（02）+ 到款状态（对比实收累计计算）。 */
export interface CollectionPlanRow {
  milestone_id: string;
  contract_no: string;
  sequence: number;
  planned_date: string | null;
  date_precision?: string | null;
  planned_amount: string | null;
  cumulative_planned: string;
  cumulative_actual: string;
  arrival_state: "paid" | "partial" | "pending" | "overdue" | string;
  follow_up_status?: string | null;
  note?: string | null;
  version: number;
}

export const getCollectionPlan = async (projectId: string) => {
  const resp = await api.get<{ total: number; rows: CollectionPlanRow[] }>(
    `${BASE}/projects/stable/${encodeURIComponent(projectId)}/master-workbook/collection-plan`,
  );
  return resp.data;
};
