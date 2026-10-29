import { api } from "../api";
import type { AudienceRule, AudienceRuleRead } from "./audience";
import type { FieldDefinition } from "./fields";
import type { PartSuggestion } from "./search";
import type { PriceRef } from "./priceRefs";
import type { WorkStatus } from "./workItems";

export interface FieldValue {
  field_def_id?: number;
  new_name?: string;
  name?: string;
  value: string | number | boolean | null;
  rule?: AudienceRule;
}

export interface ReadFieldValue extends Omit<FieldValue, "rule"> { rule?: AudienceRuleRead }
export interface PresalesCandidate { id: number; name: string }

export interface LineInput {
  line_key?: string;
  part_id?: number | null;
  pn_raw?: string | null;
  description?: string | null;
  expected_delivery?: string | null;
  qty?: number | string;
  require_exact_pn?: boolean;
  /** D-0047：坏件是否返回，默认返回。 */
  return_defective?: boolean;
  ref_offer_id?: number | null;
  notes?: string | null;
  notes_audience?: AudienceRule;
  field_values?: FieldValue[];
}

export interface InquiryLine extends Omit<LineInput, "notes_audience" | "field_values"> {
  id: number;
  inquiry_id: number;
  line_key: string;
  seq: number;
  pn_raw: string;
  qty: string | number;
  status: "active" | "deleted";
  version: number;
  notes_audience?: AudienceRuleRead;
  field_values?: ReadFieldValue[];
  field_template?: Pick<FieldDefinition, "id" | "name" | "value_type" | "choices">[];
  linked_offer_ids?: number[];
  linked_offers?: Offer[];
  ref_offer_revision_no?: number | null;
  ref_offer?: Offer;
}

export type InquiryPriority = "normal" | "urgent";

export interface InquiryInput {
  kind?: "sales_inquiry" | "proactive_offer";
  priority?: InquiryPriority;
  customer_id?: number | null;
  notes?: string | null;
  lines?: LineInput[];
  assignee_ids?: number[];
  offer_audience?: AudienceRule;
  notes_audience?: AudienceRule;
  field_values?: FieldValue[];
}

export interface OfferInput {
  unit_price?: string | number | null;
  is_tax_inclusive?: boolean | null;
  no_stock?: boolean;
  qty_available?: string | number | null;
  stock_location?: string | null;
  lead_time?: string | null;
  source_key?: string;
  supplier_id?: number | null;
  notes?: string | null;
  /** D-0047：价格性质，售价（默认）或成本价，仅作标注。 */
  price_basis?: "sale" | "cost";
  supplier_audience?: AudienceRule;
  reporter_audience?: AudienceRule;
  notes_audience?: AudienceRule;
}

export interface Offer extends Omit<OfferInput, "supplier_audience" | "reporter_audience" | "notes_audience"> {
  supplier_audience?: AudienceRuleRead;
  reporter_audience?: AudienceRuleRead;
  notes_audience?: AudienceRuleRead;
  id: number;
  inquiry_id: number;
  line_id: number;
  part_id: number | null;
  version: number;
  revision_no: number;
  status: "draft" | "published" | "stale" | "withdrawn";
  valid_from: string | null;
  reporter?: { id: number; name: string };
  created_at?: string;
  updated_at?: string;
  latest?: Pick<Offer, "unit_price" | "is_tax_inclusive" | "revision_no">;
  original_withdrawn?: boolean;
  expired?: boolean;
  more?: Offer[];
}

export interface InquiryWorkItem {
  id: number;
  assignee_id?: number | null;
  claimed_by?: number | null;
  status: WorkStatus;
  paused: boolean;
  version: number;
}

export interface LineResponseInput {
  line_id: number;
  mode: "quote" | "reuse" | "skip";
  ref_offer_id?: number;
  reason?: string;
}

export interface LineResponse extends LineResponseInput {
  is_draft?: boolean;
  created_by: number;
  work_item_id: number | null;
  offer?: Offer;
  expired: boolean;
  ref_offer_revision_no?: number | null;
  reporter_label?: string;
}

export interface ExistingChoice {
  on_existing: "update" | "new";
  existing_offer_id?: number;
}

export interface ExistingChoices {
  on_existing?: "update" | "new";
  existing_offer_id?: number;
  existing_choices?: Record<number, ExistingChoice>;
}

export interface Inquiry {
  id: number;
  kind: "sales_inquiry" | "proactive_offer";
  inquiry_no: string;
  status: "draft" | "submitted" | "revising" | "closed";
  version: number;
  revision: number;
  priority?: InquiryPriority;
  urge_count?: number;
  last_urged_at?: string | null;
  customer_id?: number | null;
  customer_name?: string | null;
  is_owner?: boolean;
  notes?: string | null;
  offer_audience?: AudienceRuleRead;
  notes_audience?: AudienceRuleRead;
  assignee_ids?: number[];
  created_by?: number;
  created_by_name?: string | null;
  created_at: string;
  updated_at: string;
  submitted_at?: string | null;
  closed_at?: string | null;
  closed_outcome?: "won" | "lost" | "cancelled" | null;
  won_offer_id?: number | null;
  won_offer_revision_no?: number | null;
  won_offer?: Offer;
  line_count: number;
  progress: { quoted_lines: number; line_count: number };
  lines?: InquiryLine[];
  offers?: Offer[];
  work_items?: InquiryWorkItem[];
  line_responses?: LineResponse[];
  field_values?: ReadFieldValue[];
}

export interface PartInquiryRecord {
  line_id: number;
  inquiry_id: number;
  created_at: string;
  created_by_name?: string | null;
  qty: string | number;
  status: Inquiry["status"];
  has_quote: boolean;
  lowest_valid_quotes: { unit_price: string | number; is_tax_inclusive: boolean | null }[];
}

export interface InquiryPage { items: Inquiry[]; total: number; page: number; page_size: number }
export interface VersionInput { expected_version: number }
export interface OfferRevision {
  revision_no: number;
  snapshot: Offer;
  changed_fields: string[];
  created_at: string;
  reason?: string | null;
}
export interface LibraryPart extends PartSuggestion { offers: Offer[]; offer_count: number }
export interface NearDuplicate { pn_std: string; description?: string | null; reason?: string }
export interface CreatePartInput extends VersionInput {
  pn_std: string;
  description?: string | null;
  brand?: string | null;
  manufacturer?: string | null;
  summary?: string | null;
  category_major?: string | null;
  category_minor?: string | null;
  machine_or_part?: string | null;
  unit?: string | null;
  spec_updates?: Record<string, import("../api").ManualSpecInput>;
}

const root = "/presales";
async function inquiryAction(id: number, action: string, body: VersionInput) {
  return (await api.post<Inquiry>(`${root}/inquiries/${id}/${action}`, body)).data;
}

/** 返回值保持服务端过滤后的形状，不补出被受众规则删去的字段。 */
export const presales = {
  async partInquiries(partId: number, page = 1, signal?: AbortSignal) {
    return (await api.get<{ items: PartInquiryRecord[]; total: number; page: number; page_size: number }>(`${root}/parts/${partId}/inquiries`, { params: { page, page_size: 20 }, signal })).data;
  },
  async customers(q = "", signal?: AbortSignal) {
    return (await api.get<PresalesCandidate[]>(`${root}/customers`, { params: { q, limit: 20 }, signal })).data;
  },
  async suppliers(q = "", signal?: AbortSignal) {
    return (await api.get<PresalesCandidate[]>(`${root}/suppliers`, { params: { q, limit: 20 }, signal })).data;
  },
  async listInquiries(params: { kind?: Inquiry["kind"]; scope?: "mine" | "shared" | "all"; status?: Inquiry["status"]; page?: number; page_size?: number;
    q?: string; date_from?: string; date_to?: string;
    sort?: "inquiry_no" | "line_count" | "status" | "updated_at" | "created_at"; dir?: "asc" | "desc" } = {}) {
    return (await api.get<InquiryPage>(`${root}/inquiries`, { params })).data;
  },
  async getInquiry(id: number, signal?: AbortSignal) {
    return (await api.get<Inquiry>(`${root}/inquiries/${id}`, { signal })).data;
  },
  async createInquiry(body: InquiryInput) { return (await api.post<Inquiry>(`${root}/inquiries`, body)).data; },
  async updateInquiry(id: number, body: Omit<InquiryInput, "kind"> & VersionInput) {
    return (await api.patch<Inquiry>(`${root}/inquiries/${id}`, body)).data;
  },
  submitInquiry: (id: number, body: VersionInput) => inquiryAction(id, "submit", body),
  resubmitInquiry: (id: number, body: VersionInput) => inquiryAction(id, "resubmit", body),
  withdrawInquiry: (id: number, body: VersionInput) => inquiryAction(id, "withdraw", body),
  copyInquiry: (id: number, body: VersionInput) => inquiryAction(id, "copy", body),
  /** D-0047：发起人催办未回复的询价，30 分钟限一次（过早返回 409 urge_too_soon）。 */
  async urgeInquiry(id: number) { return (await api.post<Inquiry>(`${root}/inquiries/${id}/urge`)).data; },
  async appendLines(id: number, body: VersionInput & { lines: LineInput[] }) {
    return (await api.post<Inquiry>(`${root}/inquiries/${id}/lines`, body)).data;
  },
  async closeInquiry(id: number, body: VersionInput & { outcome: "won" | "lost" | "cancelled"; won_offer_id?: number }) {
    return (await api.post<Inquiry>(`${root}/inquiries/${id}/close`, body)).data;
  },
  async saveOffer(inquiryId: number, lineId: number, body: OfferInput & VersionInput & { offer_id?: number; work_item_id?: number }) {
    return (await api.post<Offer>(`${root}/inquiries/${inquiryId}/lines/${lineId}/offers`, body)).data;
  },
  async updateOffer(id: number, body: OfferInput & VersionInput) {
    return (await api.patch<Offer>(`${root}/offers/${id}`, body)).data;
  },
  async withdrawOffer(id: number, body: VersionInput & { reason: string }) {
    return (await api.post<Offer>(`${root}/offers/${id}/withdraw`, body)).data;
  },
  async replyInquiry(id: number, body: VersionInput & ExistingChoices & { work_item_id?: number; expected_work_item_version?: number; line_responses?: LineResponseInput[] }) {
    return (await api.post<Offer[]>(`${root}/inquiries/${id}/reply`, body)).data;
  },
  async publishInquiry(id: number, body: VersionInput & ExistingChoices & { notice_user_ids?: number[] }) {
    return (await api.post<Offer[]>(`${root}/inquiries/${id}/publish`, body)).data;
  },
  async partCheck(lineId: number, pn?: string) {
    return (await api.get<{ near_duplicates: NearDuplicate[] }>(`${root}/lines/${lineId}/part-check`, { params: { pn_std: pn } })).data;
  },
  async bindPart(lineId: number, body: VersionInput & { part_id: number }) {
    return (await api.post<Inquiry>(`${root}/lines/${lineId}/bind-part`, body)).data;
  },
  async createPart(lineId: number, body: CreatePartInput) {
    return (await api.post<Inquiry>(`${root}/lines/${lineId}/create-part`, body)).data;
  },
  async revisions(offerId: number) { return (await api.get<OfferRevision[]>(`${root}/offers/${offerId}/revisions`)).data; },
  async library(partIds: number[]) {
    return (await api.get<Record<number, Offer[]>>(`${root}/library`, { params: { part_ids: partIds.join(",") } })).data;
  },
  async searchLibrary(q: string, signal?: AbortSignal) {
    return (await api.get<LibraryPart[]>(`${root}/library/search`, { params: { q }, signal })).data;
  },
  async reference(body: { part_ids?: number[]; inquiry_id?: number }) {
    return (await api.post<{ reference: Record<number, PriceRef>; offers: Record<number, Offer[]>; gallery: { part_id: number }[] }>(`${root}/reference`, body)).data;
  },
};
