import api from "../api";

export type DataQualityIssueStatus =
  | "open"
  | "confirmed_valid"
  | "confirmed_source_error"
  | "source_changed";

export type DataQualityIssueSide = "purchase" | "sales";
export type DataQualityDecision = "confirmed_valid" | "confirmed_source_error";
export type DataQualityEvidenceValue =
  | string | number | boolean | null
  | DataQualityEvidenceValue[]
  | { [key: string]: DataQualityEvidenceValue };

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
  price_restricted: boolean;
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
  evidence: Record<string, DataQualityEvidenceValue>;
  evidence_restricted: boolean;
  order: DataQualityIssueOrder;
  batch: DataQualityIssueBatch | null;
  audits: DataQualityIssueAudit[];
}

interface DataQualityWireBatch {
  id: number;
  filename: string | null;
  file_type: string | null;
  uploaded_by: string | null;
  uploaded_at: string | null;
}

interface DataQualityWireFact {
  order_id: number | null;
  order_no: string | null;
  order_date: string | null;
  purchaser: string | null;
  salesperson: string | null;
  part_id: number | null;
  pn_std: string | null;
  description: string | null;
  qty: number | null;
  unit: string | null;
  unit_price: number | null;
  line_amount: number | null;
  batch: DataQualityWireBatch | null;
}

interface DataQualityWireAudit {
  action: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  reason: string | null;
  operated_by: string | null;
  operated_at: string;
}

interface DataQualityWireIssue {
  id: number;
  status: DataQualityIssueStatus;
  side: DataQualityIssueSide;
  rule_code: string;
  rule_version: string;
  version: number;
  import_batch_id: number | null;
  detected_by: string | null;
  detected_at: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  created_at: string;
  updated_at: string;
  fact: DataQualityWireFact | null;
  evidence?: Record<string, DataQualityEvidenceValue> | null;
  audit?: DataQualityWireAudit[];
  price_restricted?: boolean;
  evidence_restricted?: boolean;
}

interface DataQualityWirePage {
  total: number;
  page: number;
  page_size: number;
  items: DataQualityWireIssue[];
  price_restricted?: boolean;
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
function normalizeBase(wire: DataQualityWireIssue, pageRestricted = false): DataQualityIssueListItem {
  const fact = wire.fact;
  const priceRestricted = pageRestricted || wire.price_restricted === true;
  return {
    id: wire.id,
    status: wire.status,
    side: wire.side,
    order_date: fact?.order_date ?? null,
    order_no: fact?.order_no ?? null,
    pn_std: fact?.pn_std ?? null,
    handler: fact?.purchaser ?? fact?.salesperson ?? null,
    quantity: fact?.qty ?? null,
    unit: fact?.unit ?? null,
    unit_price: priceRestricted ? null : fact?.unit_price ?? null,
    rule_code: wire.rule_code,
    rule_label: null,
    import_batch_id: wire.import_batch_id,
    import_batch_name: fact?.batch?.filename ?? null,
    updated_at: wire.updated_at,
    version: wire.version,
    price_restricted: priceRestricted,
  };
}

function normalizeDetail(wire: DataQualityWireIssue): DataQualityIssueDetail {
  const base = normalizeBase(wire);
  const fact = wire.fact;
  const batch = fact?.batch;
  return {
    ...base,
    detected_by: wire.detected_by,
    detected_at: wire.detected_at,
    reviewed_by: wire.reviewed_by,
    reviewed_at: wire.reviewed_at,
    review_note: wire.review_note,
    fact: {
      description: fact?.description ?? null,
      brand: null,
      quantity: fact?.qty ?? null,
      unit: fact?.unit ?? null,
      unit_price: base.price_restricted ? null : fact?.unit_price ?? null,
      line_amount: base.price_restricted ? null : fact?.line_amount ?? null,
    },
    evidence: wire.evidence ?? {},
    evidence_restricted: wire.evidence_restricted === true,
    order: {
      order_no: fact?.order_no ?? null,
      order_date: fact?.order_date ?? null,
      handler: fact?.purchaser ?? fact?.salesperson ?? null,
      counterparty: null,
      data_status: null,
    },
    batch: batch ? {
      id: batch.id,
      filename: batch.filename,
      imported_by: batch.uploaded_by,
      imported_at: batch.uploaded_at,
    } : null,
    audits: (wire.audit ?? []).map((entry) => ({
      action: entry.action,
      username: entry.operated_by,
      note: entry.reason,
      created_at: entry.operated_at,
    })),
  };
}

export async function listDataQualityIssues(params: ListDataQualityIssuesParams) {
  const { data } = await api.get<DataQualityWirePage>("/data-quality/issues", { params });
  return {
    total: data.total,
    page: data.page,
    page_size: data.page_size,
    items: data.items.map((item) => normalizeBase(item, data.price_restricted === true)),
  } satisfies DataQualityIssuePage;
}

export async function getDataQualityIssue(id: number) {
  const { data } = await api.get<DataQualityWireIssue>(`/data-quality/issues/${id}`);
  return normalizeDetail(data);
}

export async function decideDataQualityIssue(id: number, body: DecideDataQualityIssueBody) {
  await api.post(`/data-quality/issues/${id}/decision`, body);
  return getDataQualityIssue(id);
}

export async function reopenDataQualityIssue(id: number, body: ReopenDataQualityIssueBody) {
  await api.post(`/data-quality/issues/${id}/reopen`, body);
  return getDataQualityIssue(id);
}
