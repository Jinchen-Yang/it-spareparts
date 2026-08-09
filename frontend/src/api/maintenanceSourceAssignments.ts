import { api } from "../api";

export const maintenanceSourceOrderParamsSerializer = { indexes: null } as const;

export interface AssignedMaintenanceProject {
  project_id: string;
  project_code: string;
  display_name: string;
  is_active: boolean;
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
