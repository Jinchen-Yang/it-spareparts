import { api } from "../api";

export interface ProcurementLine {
  pn: string | null;
  description: string | null;
  qty: string | number | null;
  /** 后端定点字符串（如 "88.00"）；脱敏或源表无价时为 null，靠 unit_price_masked 区分。 */
  unit_price: string | number | null;
  /** true＝无 data_purchase_cost 权限被置空（无权查看）；false 且 unit_price 为 null＝源表本来没价。 */
  unit_price_masked: boolean;
}

export interface ProcurementOrder {
  purchase_order_no: string;
  purchase_date: string | null;
  purchaser: string | null;
  /** 关联需求单 raw id：与看板需求单行的 source_order_id 同一把键（#259）。 */
  demand_source_order_id: string | null;
  demand_order_no: string | null;
  demand_date: string | null;
  line_count: number;
  lines: ProcurementLine[];
}

export interface ProjectProcurement {
  project_id: string;
  purchases: ProcurementOrder[];
  /** 过滤后的真实总数：分页时不等于 purchases.length。 */
  total: number;
  page: number | null;
  page_size: number | null;
}

export interface ProjectProcurementQuery {
  /** 省略＝全量返回（旧协议）。 */
  page?: number;
  page_size?: number;
  /** 只看挂在这张需求单（WBDD raw id）上的采购单。 */
  source_order_id?: string;
}

export const getProjectProcurement = (projectId: string, params?: ProjectProcurementQuery) =>
  api.get<ProjectProcurement>(
    `/maintenance/projects/stable/${encodeURIComponent(projectId)}/purchases`,
    { params },
  );
