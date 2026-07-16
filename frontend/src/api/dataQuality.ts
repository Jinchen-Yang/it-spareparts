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
  review_note_restricted: boolean;
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
  qty: string | number | null;
  unit: string | null;
  unit_price: string | number | null;
  line_amount: string | number | null;
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
  review_note_restricted?: boolean;
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

export type CalibrationDirection = "increase" | "decrease";

export interface PurchasePriceCalibrationThreshold {
  threshold: number;
  eligible_pairs: number;
  candidate_count: number;
  candidate_rate: number;
  increase_count: number;
  decrease_count: number;
}

export interface PurchasePriceCalibrationTypeDistribution {
  purchase_type: string;
  eligible_pairs: number;
  thresholds: PurchasePriceCalibrationThreshold[];
}

export interface PurchasePriceCalibrationLine {
  line_id: number;
  order_no: string | null;
  order_date: string;
  quantity: number;
  unit: string | null;
  tax_basis: string;
  unit_price_ex_tax: number;
}

export interface PurchasePriceCalibrationSample {
  threshold: number;
  direction: CalibrationDirection;
  ratio: number;
  pn_std: string | null;
  purchase_type: string;
  current: PurchasePriceCalibrationLine;
  previous: PurchasePriceCalibrationLine;
}

export interface PurchasePriceCalibration {
  rule_code: string;
  rule_version: string;
  generated_at: string;
  data_through: string | null;
  eligible_pairs: number;
  distinct_parts: number;
  thresholds: PurchasePriceCalibrationThreshold[];
  purchase_types: PurchasePriceCalibrationTypeDistribution[];
  samples: PurchasePriceCalibrationSample[];
  sample_boundary: {
    limit_per_threshold_direction: number;
    ordering: string;
    contains_people_or_parties: boolean;
  };
}

export interface PurchasePriceCalibrationParams {
  date_from?: string;
  date_to?: string;
  purchase_type?: string;
  sample_limit?: number;
}

type NumericWire = number | string;

interface PurchasePriceCalibrationThresholdWire {
  multiplier: NumericWire;
  eligible_pairs: number;
  candidate_pairs: number;
  candidate_rate: NumericWire;
  increased_pairs: number;
  decreased_pairs: number;
}

interface PurchasePriceCalibrationWire {
  rule_code: string;
  rule_version: string;
  generated_at: string;
  data_through: string | null;
  eligible_pairs: number;
  distinct_parts: number;
  thresholds: PurchasePriceCalibrationThresholdWire[];
  purchase_types: Array<{
    purchase_type: string;
    eligible_pairs: number;
    thresholds: PurchasePriceCalibrationThresholdWire[];
  }>;
  samples: Array<{
    multiplier: NumericWire;
    sample_rank: number;
    direction: CalibrationDirection;
    ratio: NumericWire;
    pn_std: string | null;
    purchase_type: string;
    current_line_id: number;
    current_order_no: string | null;
    current_order_date: string;
    current_qty: NumericWire;
    current_unit: string | null;
    current_unit_price_ex_tax: NumericWire;
    current_tax_basis: string;
    previous_line_id: number;
    previous_order_no: string | null;
    previous_order_date: string;
    previous_qty: NumericWire;
    previous_unit: string | null;
    previous_unit_price_ex_tax: NumericWire;
    previous_tax_basis: string;
  }>;
  sample_boundary: PurchasePriceCalibration["sample_boundary"];
}

/**
 * 数据疑点接口的唯一适配层。页面不直接拼路径，也不依赖 AxiosResponse，后端契约若微调只改这里。
 */
function nullableNumber(value: string | number | null | undefined): number | null {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function wireNumber(value: NumericWire): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function normalizeCalibrationThreshold(
  wire: PurchasePriceCalibrationThresholdWire,
): PurchasePriceCalibrationThreshold {
  return {
    threshold: wireNumber(wire.multiplier),
    eligible_pairs: wire.eligible_pairs,
    candidate_count: wire.candidate_pairs,
    candidate_rate: wireNumber(wire.candidate_rate),
    increase_count: wire.increased_pairs,
    decrease_count: wire.decreased_pairs,
  };
}

export async function getPurchasePriceCalibration(params: PurchasePriceCalibrationParams = {}) {
  const { data } = await api.get<PurchasePriceCalibrationWire>(
    "/data-quality/calibration/purchase-price",
    { params },
  );
  return {
    ...data,
    thresholds: data.thresholds.map(normalizeCalibrationThreshold),
    purchase_types: data.purchase_types.map((row) => ({
      ...row,
      thresholds: row.thresholds.map(normalizeCalibrationThreshold),
    })),
    samples: data.samples.map((sample): PurchasePriceCalibrationSample => ({
      threshold: wireNumber(sample.multiplier),
      direction: sample.direction,
      ratio: wireNumber(sample.ratio),
      pn_std: sample.pn_std,
      purchase_type: sample.purchase_type,
      current: {
        line_id: sample.current_line_id,
        order_no: sample.current_order_no,
        order_date: sample.current_order_date,
        quantity: wireNumber(sample.current_qty),
        unit: sample.current_unit,
        tax_basis: sample.current_tax_basis,
        unit_price_ex_tax: wireNumber(sample.current_unit_price_ex_tax),
      },
      previous: {
        line_id: sample.previous_line_id,
        order_no: sample.previous_order_no,
        order_date: sample.previous_order_date,
        quantity: wireNumber(sample.previous_qty),
        unit: sample.previous_unit,
        tax_basis: sample.previous_tax_basis,
        unit_price_ex_tax: wireNumber(sample.previous_unit_price_ex_tax),
      },
    })),
  } satisfies PurchasePriceCalibration;
}

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
    quantity: nullableNumber(fact?.qty),
    unit: fact?.unit ?? null,
    unit_price: priceRestricted ? null : nullableNumber(fact?.unit_price),
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
    review_note_restricted: wire.review_note_restricted === true,
    fact: {
      description: fact?.description ?? null,
      brand: null,
      quantity: nullableNumber(fact?.qty),
      unit: fact?.unit ?? null,
      unit_price: base.price_restricted ? null : nullableNumber(fact?.unit_price),
      line_amount: base.price_restricted ? null : nullableNumber(fact?.line_amount),
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
  const { data } = await api.post<DataQualityWireIssue>(`/data-quality/issues/${id}/decision`, body);
  return normalizeDetail(data);
}

export async function reopenDataQualityIssue(id: number, body: ReopenDataQualityIssueBody) {
  const { data } = await api.post<DataQualityWireIssue>(`/data-quality/issues/${id}/reopen`, body);
  return normalizeDetail(data);
}
