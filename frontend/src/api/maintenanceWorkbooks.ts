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
  collection_creates: number;
  collection_voids: number;
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

export const validateSparePartLines = (file: File) =>
  upload(`${BASE}/spare-part-lines/validate`, file);

export const applySparePartLines = (file: File) =>
  upload(`${BASE}/spare-part-lines/apply`, file);

/** ②项目总表（六 sheet）／③各 tab 单 sheet（传 sheets）。 */
export const downloadProjectMaster = (projectId: string, sheets?: string[]) =>
  download(`${BASE}/projects/stable/${encodeURIComponent(projectId)}/master-workbook.xlsx`,
    sheets && sheets.length ? { sheets: sheets.join(",") } : undefined);

export const validateProjectMaster = (projectId: string, file: File) =>
  upload(`${BASE}/projects/stable/${encodeURIComponent(projectId)}/master-workbook/validate`,
    file);

export const applyProjectMaster = (projectId: string, file: File) =>
  upload(`${BASE}/projects/stable/${encodeURIComponent(projectId)}/master-workbook/apply`,
    file);

/** 浏览器落盘：后端返回的是 attachment，这里只负责触发保存。 */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}
