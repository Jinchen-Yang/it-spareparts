import api from "../api";

export type DataQualityIssueStatus =
  | "open"
  | "confirmed_valid"
  | "confirmed_source_error"
  | "source_changed";

export type DataQualityIssueSide = "purchase" | "sales";
export type DataQualityDecision = "confirmed_valid" | "confirmed_source_error";

export interface DataQualityIssueListItem {
  id: number;
  status: DataQualityIssueStatus;
  side: DataQualityIssueSide;
  order_date: string | null;
  order_no: string | null;
  pn_std: string | null;
  handler: string | null;
  quantity: number | null;
  unit: string | null;
  /** 后端脱敏后必须为 null；null 在页面统一解释为无价格权限。 */
  unit_price: number | null;
  rule_code: string;
  rule_label: string | null;
  import_batch_id: number | null;
  import_batch_name: string | null;
  updated_at: string;
  version: number;
}

export interface DataQualityIssuePage {
  total: number;
  page: number;
  page_size: number;
  items: DataQualityIssueListItem[];
}

export interface DataQualityIssueFact {
  description: string | null;
  brand: string | null;
  quantity: number | null;
  unit: string | null;
  unit_price: number | null;
  line_amount: number | null;
  [key: string]: string | number | boolean | null | undefined;
}

export interface DataQualityIssueOrder {
  order_no: string | null;
  order_date: string | null;
  handler: string | null;
  counterparty: string | null;
  data_status: string | null;
  [key: string]: string | number | boolean | null | undefined;
}

export interface DataQualityIssueBatch {
  id: number;
  filename: string | null;
  imported_by: string | null;
  imported_at: string | null;
}

export interface DataQualityIssueAudit {
  action: string;
  username: string | null;
  note: string | null;
  created_at: string;
}

export interface DataQualityIssueDetail extends DataQualityIssueListItem {
  detected_by: string | null;
  detected_at: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  fact: DataQualityIssueFact;
  evidence: Record<string, string | number | boolean | null>;
  order: DataQualityIssueOrder;
  batch: DataQualityIssueBatch | null;
  audits: DataQualityIssueAudit[];
}

export interface ListDataQualityIssuesParams {
  status?: DataQualityIssueStatus;
  side?: DataQualityIssueSide;
  rule_code?: string;
  q?: string;
  page?: number;
  page_size?: number;
}

export interface DecideDataQualityIssueBody {
  decision: DataQualityDecision;
  version: number;
  note: string;
}

export interface ReopenDataQualityIssueBody {
  version: number;
  note: string;
}

/**
 * 数据疑点接口的唯一适配层。页面不直接拼路径，也不依赖 AxiosResponse，后端契约若微调只改这里。
 */
export async function listDataQualityIssues(params: ListDataQualityIssuesParams) {
  const { data } = await api.get<DataQualityIssuePage>("/data-quality/issues", { params });
  return data;
}

export async function getDataQualityIssue(id: number) {
  const { data } = await api.get<DataQualityIssueDetail>(`/data-quality/issues/${id}`);
  return data;
}

export async function decideDataQualityIssue(id: number, body: DecideDataQualityIssueBody) {
  const { data } = await api.post<DataQualityIssueDetail>(`/data-quality/issues/${id}/decision`, body);
  return data;
}

export async function reopenDataQualityIssue(id: number, body: ReopenDataQualityIssueBody) {
  const { data } = await api.post<DataQualityIssueDetail>(`/data-quality/issues/${id}/reopen`, body);
  return data;
}
