import { api } from "../api";

const root = "/presales/pool";

export type PoolTab = "pending" | "quoted" | "skip" | "mine" | "all";
export type AnswerState = "pending" | "quoted" | "skip";
export type PriceBasis = "sale" | "cost";

export interface PoolPrice {
  offer_id: number;
  revision_no: number;
  unit_price: string | null;
  is_tax_inclusive: boolean | null;
  price_basis: PriceBasis;
  lead_time: string | null;
  stock_location: string | null;
  qty_available: string | null;
  notes: string | null;
  supplier_id: number | null;
  supplier_name: string | null;
}

export interface PoolLatestPrice {
  offer_id: number;
  unit_price: string;
  is_tax_inclusive: boolean | null;
  price_basis: PriceBasis;
  reporter: string | null;
  at: string | null;
}

export interface PoolLine {
  line_id: number;
  line_version: number;
  inquiry_id: number;
  inquiry_no: string;
  inquiry_version: number;
  inquiry_status: "submitted" | "revising" | "closed";
  closed_outcome: "won" | "lost" | "cancelled" | null;
  priority: "normal" | "urgent";
  submitted_at: string | null;
  requester: string | null;
  is_mine: boolean;
  inquiry_notes: string | null;
  customer_id?: number | null;
  customer_name?: string | null;
  part_id: number | null;
  pn: string;
  description: string | null;
  qty: string;
  require_exact_pn: boolean;
  return_defective: boolean;
  notes: string | null;
  expected_delivery: string | null;
  answer_state: AnswerState;
  answer_requires_review?: boolean;
  answer_reason: string | null;
  answered_by: string | null;
  answered_at: string | null;
  price: PoolPrice | null;
  latest_same_pn: PoolLatestPrice | null;
}

export interface PoolCounts { pending: number; quoted: number; skip: number; mine: number; all: number; drafts: number }

export interface PoolPage { items: PoolLine[]; total: number; page: number; page_size: number; counts: PoolCounts }

export interface PoolNewLine {
  part_id?: number | null;
  pn_raw: string;
  description?: string | null;
  qty: number;
  require_exact_pn: boolean;
  return_defective: boolean;
  notes?: string | null;
  expected_delivery?: string | null;
}

export interface PoolPriceInput {
  expected_version: number;
  unit_price: number;
  is_tax_inclusive: boolean | null;
  price_basis: PriceBasis;
  lead_time?: string | null;
  stock_location?: string | null;
  qty_available?: number | null;
  supplier_id?: number | null;
  notes?: string | null;
}

export interface PoolLineEdit {
  expected_version: number;
  confirm_reset?: boolean;
  qty?: number;
  return_defective?: boolean;
  require_exact_pn?: boolean;
  notes?: string | null;
}

export interface PoolHistoryItem {
  kind: "price" | "line.edited" | "line.skipped";
  at: string;
  actor: string | null;
  reason?: string | null;
  unit_price?: string | null;
  status?: string;
  is_tax_inclusive?: boolean | null;
  price_basis?: PriceBasis;
  changed?: string[];
}

export const presalesPool = {
  async lines(params: { tab: PoolTab; q?: string; page: number; page_size: number }, signal?: AbortSignal) {
    return (await api.get<PoolPage>(`${root}/lines`, { params, signal })).data;
  },
  async counts(signal?: AbortSignal) {
    return (await api.get<PoolCounts>(`${root}/counts`, { signal })).data;
  },
  async create(body: { customer_id?: number | null; priority: "normal" | "urgent"; notes?: string | null; visible_to?: string[]; lines: PoolNewLine[] }) {
    return (await api.post<{ inquiry_id: number; inquiry_no: string; line_count: number }>(`${root}/inquiries`, body)).data;
  },
  async setPrice(lineId: number, body: PoolPriceInput) {
    return (await api.put<PoolLine>(`${root}/lines/${lineId}/price`, body)).data;
  },
  async skip(lineId: number, body: { expected_version: number; reason: string }) {
    return (await api.post<PoolLine>(`${root}/lines/${lineId}/skip`, body)).data;
  },
  async edit(lineId: number, body: PoolLineEdit) {
    return (await api.patch<PoolLine>(`${root}/lines/${lineId}`, body)).data;
  },
  async history(lineId: number) {
    return (await api.get<PoolHistoryItem[]>(`${root}/lines/${lineId}/history`)).data;
  },
};

/** 服务端冲突里带本行最新内容；取不到时返回 undefined，由通用错误提示处理。 */
export function poolConflict(error: unknown): { code: string; latest: PoolLine } | undefined {
  const detail = (error as { response?: { status?: number; data?: { detail?: { code?: string; latest?: PoolLine } } } })?.response;
  const body = detail?.status === 409 ? detail.data?.detail : undefined;
  return body?.code && body.latest ? { code: body.code, latest: body.latest } : undefined;
}

export const POOL_CHANGED_EVENT = "presales-pool-changed";
export const notifyPoolChanged = () => window.dispatchEvent(new Event(POOL_CHANGED_EVENT));
