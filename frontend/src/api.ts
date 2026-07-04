import axios from "axios";

const api = axios.create({ baseURL: "/api" });

api.interceptors.request.use((cfg) => {
  const token = localStorage.getItem("token");
  if (token) cfg.headers.Authorization = `Bearer ${token}`;
  return cfg;
});

api.interceptors.response.use(
  (r) => r,
  (err) => {
    // 登录 / 登录页改密接口自己的 401(账号或密码错)交给页面行内提示，不在这里 reload 把提示冲掉；
    // 其它接口的 401 = token 失效，清掉并重载回登录页。
    const url = err.config?.url || "";
    const isPublicAuth = url.includes("/auth/login") || url.includes("/auth/change-password-unauth");
    if (err.response?.status === 401 && !isPublicAuth) {
      localStorage.removeItem("token");
      location.reload();
    }
    return Promise.reject(err);
  }
);

export default api;

export interface PartHit {
  pn_std: string;
  description: string | null;
  brand: string | null;
  category_major: string | null;
  needs_review: boolean;
  // 近似搜索附加字段（空查询浏览列表时无）
  score?: number;
  match_reason?: string;
  is_excluded?: boolean;
  // 结构化规格过滤返回（按容量/接口查询时带）
  specs?: Record<string, string>;
}

// ===== 二期 AI 助手 =====
export interface AgentToolCall {
  name: string;
  args: Record<string, unknown>;
}
/** SSE 事件（/agent/chat/stream） */
export type AgentStreamEvent =
  | { type: "delta"; text: string }
  | { type: "thinking"; text: string }
  | { type: "tool"; name: string; args: Record<string, unknown> }
  | { type: "tool_done"; name: string; ok: boolean }
  | { type: "done"; tool_calls: AgentToolCall[]; answer?: string; configured?: boolean; stopped?: boolean }
  | { type: "error"; message: string };

// ===== 服务端会话（平台化 P1）=====
export interface ChatSessionMeta {
  id: number;
  title: string;
  updated_at: string;
  generating?: boolean;   // 后台仍在生成（切走的会话也会续跑）
}
export interface ChatMessageRow {
  id: number;
  role: "user" | "assistant";
  content: string;
  tools: AgentToolCall[];
  stopped: boolean;
  created_at: string;
}

export const listChatSessions = () =>
  api.get<{ items: ChatSessionMeta[] }>("/agent/sessions");
export const createChatSession = (title?: string) =>
  api.post<ChatSessionMeta>("/agent/sessions", { title: title ?? null });
export const renameChatSession = (id: number, title: string) =>
  api.patch(`/agent/sessions/${id}`, { title });
export const deleteChatSession = (id: number) =>
  api.delete(`/agent/sessions/${id}`);
export const getChatMessages = (id: number) =>
  api.get<{ id: number; title: string; items: ChatMessageRow[] }>(
    `/agent/sessions/${id}/messages`,
  );
/** 停止当前生成：服务端 worker 收束并把已生成部分以"已中断"落库。
 * 用 fetch 而非 axios：避开全局 401 拦截器的整页 reload。 */
export const cancelChatStream = (id: number) =>
  fetch(`/api/agent/sessions/${id}/chat/cancel`, {
    method: "POST",
    headers: { Authorization: `Bearer ${localStorage.getItem("token") || ""}` },
  }).catch(() => undefined);

export type SessionStreamEvent =
  | AgentStreamEvent
  | { type: "title"; title: string }
  | { type: "no_active" };   // attach：该会话当前没有进行中的生成

async function _consumeSSE(
  resp: Response,
  onEvent: (ev: SessionStreamEvent) => void,
): Promise<void> {
  if (!resp.ok || !resp.body) {
    // 不在这里 reload：调用方要先把用户刚输入的内容存草稿，避免 reload 丢稿
    throw new Error(`stream http ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const blocks = buf.split("\n\n");
    buf = blocks.pop()!;
    for (const block of blocks) {
      const line = block.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      try {
        onEvent(JSON.parse(line.slice(6)) as SessionStreamEvent);
      } catch {
        /* 跳过坏帧 */
      }
    }
  }
}

/** 会话内流式问答：只发新消息，历史由服务端持有。 */
export async function sessionChatStream(
  sessionId: number,
  message: string,
  onEvent: (ev: SessionStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`/api/agent/sessions/${sessionId}/chat/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${localStorage.getItem("token") || ""}`,
    },
    body: JSON.stringify({ message }),
    signal,
  });
  await _consumeSSE(resp, onEvent);
}

/** 重新订阅会话进行中的生成（切回会话续看连续直播）：先回放已生成部分再续实时。
 * 无进行中的生成时服务端回 {type:"no_active"}。 */
export async function attachChatStream(
  sessionId: number,
  onEvent: (ev: SessionStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`/api/agent/sessions/${sessionId}/chat/attach`, {
    method: "POST",
    headers: { Authorization: `Bearer ${localStorage.getItem("token") || ""}` },
    signal,
  });
  await _consumeSSE(resp, onEvent);
}

// ===== 采购记录（合同重点）=====
export interface RecentPurchaseRow {
  line_id: number;
  order_no: string;
  order_date: string | null;
  purchaser: string | null;
  source_type: string | null;
  data_status: string | null;
  supplier: string | null;
  pn_std: string;
  needs_review: boolean;
  description: string | null;
  brand: string | null;
  qty: number | null;
  is_tax_inclusive: boolean | null;   // 单价/金额税口径：含税单→含税、不含单→不含税（前端按此分两列，零计算）
  unit_price: number | null;
  line_amount: number | null;
}
export const listRecentPurchases = (params: {
  q?: string; days?: number; supplier?: string; page?: number; page_size?: number;
  status?: string;
}) =>
  api.get<{ total: number; page: number; page_size: number; days: number; items: RecentPurchaseRow[] }>(
    "/purchases/recent", { params },
  );

// ===== 取消单统计（宋总：按月/季/年统计采购取消/作废）=====
export interface CancellationPeriodRow {
  period: string;
  total: number;
  cancelled: number;
  cancel_rate: number;
  cancelled_amount: number;
  by_status: Record<string, { count: number; amount: number }>;
}
export interface CancellationStats {
  granularity: string;
  statuses: string[];
  rows: CancellationPeriodRow[];
  summary: { total: number; cancelled: number; cancel_rate: number; cancelled_amount: number };
}
export const fetchCancellationStats = (params: { granularity?: string; days?: number }) =>
  api.get<CancellationStats>("/purchases/cancellation-stats", { params });

// ===== 采购分析面板（早会/周会）=====
export interface PurchaseChannelSplit {
  channel: string;
  times: number;
  qty: number | null;
  amount: number | null;
  price_ex_last: number | null;
  price_inc_last: number | null;
}
export interface PurchaseAnalysisRow {
  part_id: number;
  pn_std: string;
  needs_review: boolean;
  description: string | null;
  brand: string | null;
  buy_times: number;
  total_qty: number | null;
  daily: number[] | null;
  price_ex_min: number | null; price_ex_max: number | null;
  price_ex_last: number | null; price_ex_avg: number | null;
  price_inc_min: number | null; price_inc_max: number | null;
  price_inc_last: number | null; price_inc_avg: number | null;
  price_trend: "up" | "down" | "flat" | "new";
  source_types: string[];
  is_frequent: boolean;
  advice: string;
  channels: PurchaseChannelSplit[];
}
export interface PurchaseAnalysis {
  window: { days: number; since: string; until: string; freq_threshold: number;
            exclude_designated: boolean; daily: boolean };
  kpi: { total_amount: number | null;
         total_amount_inc: number | null; total_amount_ex: number | null;   // 订单级真实含税/不含税总额（零计算）
         order_count: number;
         order_count_by_source: Record<string, number>; part_count: number;
         frequent_count: number; shown: number; truncated: number };
  source_composition: { channel: string; amount: number | null;
                        amount_inc: number | null; amount_ex: number | null;
                        order_count: number; line_count: number }[];
  rows: PurchaseAnalysisRow[];
}
export interface PurchaseDrillItem {
  order_date: string | null; order_no: string | null; purchaser: string | null;
  supplier: string | null; source_channel: string; source_type: string | null;
  qty: number | null; is_tax_inclusive: boolean | null; unit_price: number | null;
  price_ex: number | null; price_inc: number | null;
}
export const fetchPurchaseAnalysis = (params: {
  days?: number; exclude_designated?: boolean; freq_threshold?: number;
  q?: string; supplier?: string; purchaser?: string;
}) => api.get<PurchaseAnalysis>("/purchases/analysis", { params });
export const fetchPurchaseDrill = (params: { part_id: number; days?: number; exclude_designated?: boolean }) =>
  api.get<{ part_id: number; days: number; items: PurchaseDrillItem[] }>(
    "/purchases/analysis/part", { params },
  );

export interface AgentUploadResult {
  file_id: string;
  filename: string;
  ext: string;
  file_kind: string; // 表格/Word/PDF/图片/文本
  sheets?: { name: string; n_rows: number; n_cols: number }[];
}

export const agentUpload = (file: File) => {
  const fd = new FormData();
  fd.append("file", file);
  return api.post<AgentUploadResult>("/agent/upload", fd);
};

/** 带鉴权下载智能体文件。
 * 用 fetch 而非 axios：全局 401 拦截器会 location.reload()，
 * 点下载触发整页刷新会打断对话——下载失败必须就地提示，绝不能刷页面。 */
export const agentDownload = async (url: string, fallbackName = "下载.xlsx") => {
  const m = /\/api\/agent\/files\/([a-f0-9]{6,})/.exec(url);
  if (!m) throw new Error("bad-url");
  const resp = await fetch(`/api/agent/files/${m[1]}`, {
    headers: { Authorization: `Bearer ${localStorage.getItem("token") || ""}` },
    cache: "no-store",
  });
  if (resp.status === 401) throw new Error("auth-expired");
  if (!resp.ok) throw new Error(`http-${resp.status}`);
  const cd = resp.headers.get("content-disposition") || "";
  const fm = /filename\*=UTF-8''([^;]+)/.exec(cd) || /filename="?([^";]+)"?/.exec(cd);
  const name = fm ? decodeURIComponent(fm[1]) : fallbackName;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(await resp.blob());
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
};

export interface FilePreview {
  file_id: string;
  filename: string;
  kind: "table" | "image" | "other";
  ext?: string;
  sheets?: { name: string; rows: string[][]; total_rows: number; truncated: boolean }[];
}

/** 在线预览文件内容（用 fetch 而非 axios：避开全局 401 拦截器的整页刷新，绝不打断对话）。 */
export const agentPreview = async (fileId: string): Promise<FilePreview> => {
  const resp = await fetch(`/api/agent/files/${fileId}/preview`, {
    headers: { Authorization: `Bearer ${localStorage.getItem("token") || ""}` },
    cache: "no-store",
  });
  if (resp.status === 401) throw new Error("auth-expired");
  if (!resp.ok) throw new Error(`http-${resp.status}`);
  return resp.json();
};

/** 取文件 blob 的 object URL（图片预览用，带鉴权）；调用方用完需 URL.revokeObjectURL。 */
export const agentFileBlobUrl = async (fileId: string): Promise<string> => {
  const resp = await fetch(`/api/agent/files/${fileId}`, {
    headers: { Authorization: `Bearer ${localStorage.getItem("token") || ""}` },
    cache: "no-store",
  });
  if (resp.status === 401) throw new Error("auth-expired");
  if (!resp.ok) throw new Error(`http-${resp.status}`);
  return URL.createObjectURL(await resp.blob());
};

// ===== 备件主数据自治（采购可编辑/新建 PN，WP1 + 轻量 C）=====
export interface NearDup { pn_std: string; description: string | null; reason: string }
export interface CategoryNode { code: string; name: string; children: { code: string; name: string }[] }
/** 单字段证据：值 + 来源（原文明写/确定性推导/型号字典）+ 证据片段。 */
export interface SpecField { value: string; source: string; evidence: string }
export interface ClassifySuggestion {
  category_l1?: string | null;
  category_l2?: string | null;
  whole_system?: boolean;
  // 标准化建议（描述标准化工具）
  canonical_description?: string | null;
  brand_norm?: string | null;
  brand_zh?: string | null;
  fields?: Record<string, string>;
  // 确定性引擎：对象类型 + 每字段证据 + 校验 + 审核状态
  object_type?: string | null;
  structured_specs?: Record<string, SpecField>;
  validation_errors?: string[];
  review_status?: string;   // AUTO_OK | REVIEW_REQUIRED
}
/** 证据来源中文标签。 */
export const SPEC_SOURCE_LABEL: Record<string, string> = {
  DESCRIPTION_EXPLICIT: "原文明写",
  DERIVED_SAFE: "确定性推导",
  MODEL_DICTIONARY: "型号字典",
  MANUAL: "人工",
  UNKNOWN: "未知",
};
export interface MasterFields {
  description?: string | null; brand?: string | null;
  category_major?: string | null; category_minor?: string | null;
  machine_or_part?: string | null; unit?: string | null;
}
export const searchParts = (q: string, page = 1, page_size = 20) =>
  api.get<{ items: PartHit[]; total: number }>("/parts/search", { params: { q, page, page_size } });
export const masterCategories = () =>
  api.get<{ categories: CategoryNode[]; battery_subtypes: string[]; cooling_types: string[] }>(
    "/parts/master/categories");
export const masterSuggest = (description: string, pn = "", brand = "") =>
  api.get<{ suggestion: ClassifySuggestion | null }>("/parts/master/suggest",
    { params: { description, pn, brand } });
export const masterCheck = (pn_std: string) =>
  api.get<{ near_duplicates: NearDup[] }>("/parts/master/check", { params: { pn_std } });
export const masterCreate = (body: MasterFields & { pn_std: string; force?: boolean }) =>
  api.post<{ created: boolean; id?: number; pn_std?: string; near_duplicates?: NearDup[]; message?: string }>(
    "/parts/master", body);
export const masterEdit = (body: MasterFields & { pn_std: string }) =>
  api.patch<{ id: number; pn_std: string; updated: string[]; locked_fields: string[] }>(
    "/parts/master", body);

// 批量规范化（WP3）
export interface BatchPreviewItem {
  part_id: number;
  pn_std: string;
  description: string | null;
  brand: string | null;
  category_major: string | null;
  category_minor: string | null;
  recent_sales_amount: number | null;
  suggestion: ClassifySuggestion;
  changes: string[];
  review_status?: string;   // 行级：AUTO_OK 才默认勾选（§17）
}
export const batchPreview = (page = 1, page_size = 20, only_changes = true) =>
  api.get<{ total_beijian: number; page: number; page_size: number; items: BatchPreviewItem[] }>(
    "/parts/master/batch-preview", { params: { page, page_size, only_changes } });
export const batchApply = (part_ids: number[], fields?: string[]) =>
  api.post<{ applied: number; skipped: number }>("/parts/master/batch-apply", { part_ids, fields });

export interface Overview {
  part: {
    pn_std: string;
    description: string | null;
    brand: string | null;
    category_major: string | null;
    category_minor: string | null;
    unit: string | null;
    needs_review: boolean;
    machine_or_part?: string | null;
    locked_fields?: string[];
    redirected_from?: string | null;
  };
  purchases_recent: PurchaseRow[];
  sales_recent: SalesRow[];
  inventory: InventoryRow[];
  substitutes: {
    pn_std: string; description: string | null; source: string;
    relation?: string; substitute_type?: string | null;
    via?: string | null;          // 间接互替：经由哪个通用号连到本组
    stock_qty?: number | null;    // 该通用号当前库存合计
  }[];
  profit_summary: {
    avg_purchase_cost: number | null;
    avg_sale_price: number | null;
    avg_cost_moving: number | null;
    avg_cost_fifo: number | null;
    avg_margin_moving: number | null;
    avg_margin_fifo: number | null;
    total_qty_sold: number;
  };
  inquiry_ref: {
    min_money: number | null;
    max_money: number | null;
    last_money: number | null;
    count: number;
  };
  sales_velocity: {
    qty_sold_90d: number;
    monthly_avg_90d: number;
    last_sale_date: string | null;
  };
  // 近期加权成交参考价（销售出价用）。PR #1 后端 get_overview 已返回；仍按可空处理（防御旧响应）。
  sale_price_ref?: {
    ref_sale_price: number | null;   // 近期加权成交参考价
    ref_sale_samples: number;        // 取样成交笔数（0 = 窗口内无成交）
    ref_window_days: number;         // 取样窗口天数（30）
  };
  // 逐单销售成交明细是否按权限隐藏（与后端 is_scoped_sales 对齐）：true → sales_recent 必空
  sales_recent_restricted?: boolean;
  // 锚定动态库存（型号级主口径）：期初=最近快照/盘点 + 之后单据流水
  stock_dynamic?: {
    dynamic_qty: number | null; anchor_qty: number | null; anchor_date: string | null;
    in_qty: number | null; out_sales: number | null; out_maint: number | null;
  };
}

export interface PurchaseRow {
  order_no: string;
  order_date: string | null;
  supplier: string | null;
  qty: number | null;
  unit_price: number | null;
  source_type: string | null;
  is_tax_inclusive: boolean | null;   // 单价口径：含税单→含税、不含单→不含税（前端分列，零计算）
}
export interface SalesRow {
  order_no: string;
  order_date: string | null;
  customer: string | null;
  qty: number | null;
  unit_price: number | null;
}
export interface InventoryRow {
  warehouse: string;
  pn_std: string;   // 源 pn：合并后同仓多行用它区分
  display_qty: number | null;
  source_qty: number | null;
  manual_qty: number | null;
  unit_cost: number | null;
  inventory_value: number | null;
}
