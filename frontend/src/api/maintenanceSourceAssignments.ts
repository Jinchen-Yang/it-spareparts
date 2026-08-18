import { api } from "../api";

export const maintenanceSourceOrderParamsSerializer = { indexes: null } as const;

export interface AssignedMaintenanceProject {
  project_id: string;
  project_code: string;
  display_name: string;
  is_active: boolean;
}

/** 归属候选（只出候选、人工确认；ADR-0002：名称只是线索）。 */
export interface MaintenanceAssignmentCandidate {
  project_id: string;
  project_code: string;
  display_name: string;
  match_type: "exact" | "trgm";
  score: number;
}

export interface MaintenanceSourceOrderRow {
  raw_order_id: string;
  order_no: string;
  order_date: string | null;
  project_raw: string | null;
  project_std: string | null;
  assignment_id: string | null;
  assignment_version: number | null;
  assigned_project: AssignedMaintenanceProject | null;
  /** 仅当 include_candidates=true 时出现。 */
  candidates?: MaintenanceAssignmentCandidate[];
  /** 仅当传了 xsdd_project_id 时出现：该单的 XSDD 是否属于本项目（#48）。 */
  matches_project_xsdd?: boolean;
  is_pre_delivery?: boolean;
}

export interface MaintenanceSourceOrderDirectory {
  rows: MaintenanceSourceOrderRow[];
  total: number;
  page: number;
  page_size: number;
}

export interface MaintenanceAssignmentExpectation {
  source_order_id: string;
  expected_assignment_id?: string | null;
  expected_version?: number | null;
}

export interface MaintenanceAssignmentResult {
  assignment_id: string;
  source_order_id: string;
  project_id: string;
  is_active: boolean;
  version: number;
}

export const listMaintenanceSourceOrders = (params: {
  q?: string;
  source_order_id?: string[];
  assignment_status?: "unassigned" | "assigned" | "all";
  project_id?: string;
  /** 展示板扩展（plan v1.3 M2-1）：只读归属候选与预交付徽标；默认关闭时响应形状不变。 */
  include_candidates?: boolean;
  /** #48：命中该项目 XSDD 集合的未归属单排最前（**排序**不是过滤，其余仍在列表里）。 */
  xsdd_project_id?: string;
  page?: number;
  page_size?: number;
} = {}) => api.get<MaintenanceSourceOrderDirectory>(
  "/maintenance/project-assignments/orders",
  { params, paramsSerializer: maintenanceSourceOrderParamsSerializer },
);

export const assignMaintenanceSourceOrders = (body: {
  project_id: string;
  items: MaintenanceAssignmentExpectation[];
  reason: string;
}) => api.post<{ assignments: MaintenanceAssignmentResult[] }>(
  "/maintenance/project-assignments/orders/assign",
  body,
);

export const unassignMaintenanceSourceOrders = (body: {
  items: Array<{ assignment_id: string; expected_version: number }>;
  reason: string;
}) => api.post<{ assignments: MaintenanceAssignmentResult[] }>(
  "/maintenance/project-assignments/orders/unassign",
  body,
);

/** 自动补挂靠：未归属维保订单按 project_std 精确匹配已有项目主档并挂靠。 */
export const autoAssignMaintenanceSourceOrders = () =>
  api.post<{
    result: {
      assigned_orders: number;
      matched_projects: number;
      skipped_groups: number;
      skipped_ambiguous: number;
    };
  }>("/maintenance/project-assignments/orders/auto-assign", {});
