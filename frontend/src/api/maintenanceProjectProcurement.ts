import { api } from "../api";

export interface ProcurementLine {
  pn: string | null;
  description: string | null;
  qty: number | null;
  unit_price: number | null;
}

export interface ProcurementOrder {
  purchase_order_no: string;
  purchase_date: string | null;
  purchaser: string | null;
  demand_order_no: string | null;
  demand_date: string | null;
  line_count: number;
  lines: ProcurementLine[];
}

export interface ProjectProcurement {
  project_id: string;
  purchases: ProcurementOrder[];
}

export const getProjectProcurement = (projectId: string) =>
  api.get<ProjectProcurement>(
    `/maintenance/projects/stable/${encodeURIComponent(projectId)}/purchases`,
  );
