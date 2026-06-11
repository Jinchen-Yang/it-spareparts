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
    if (err.response?.status === 401) {
      localStorage.removeItem("token");
      if (location.pathname !== "/login") location.reload();
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
}

// ===== 二期 AI 助手 =====
export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}
export interface AgentToolCall {
  name: string;
  args: Record<string, unknown>;
}
export interface AgentChatResponse {
  configured: boolean;
  answer: string;
  tool_calls: AgentToolCall[];
}

export const agentChat = (messages: ChatMessage[]) =>
  api.post<AgentChatResponse>("/agent/chat", { messages });

/** SSE 事件（/agent/chat/stream） */
export type AgentStreamEvent =
  | { type: "delta"; text: string }
  | { type: "tool"; name: string; args: Record<string, unknown> }
  | { type: "tool_done"; name: string; ok: boolean }
  | { type: "done"; tool_calls: AgentToolCall[]; answer?: string; configured?: boolean }
  | { type: "error"; message: string };

/** 流式问答：axios 不支持浏览器流式，用 fetch + ReadableStream 解析 SSE */
export async function agentChatStream(
  messages: ChatMessage[],
  onEvent: (ev: AgentStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch("/api/agent/chat/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${localStorage.getItem("token") || ""}`,
    },
    body: JSON.stringify({ messages }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    if (resp.status === 401) {
      localStorage.removeItem("token");
      location.reload();
    }
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
        onEvent(JSON.parse(line.slice(6)) as AgentStreamEvent);
      } catch {
        /* 跳过坏帧 */
      }
    }
  }
}

export interface AgentUploadResult {
  file_id: string;
  filename: string;
  sheets: { name: string; n_rows: number; n_cols: number }[];
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

export interface Overview {
  part: {
    pn_std: string;
    description: string | null;
    brand: string | null;
    category_major: string | null;
    category_minor: string | null;
    unit: string | null;
    needs_review: boolean;
  };
  purchases_recent: PurchaseRow[];
  sales_recent: SalesRow[];
  inventory: InventoryRow[];
  substitutes: { pn_std: string; description: string | null; source: string }[];
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
}

export interface PurchaseRow {
  order_no: string;
  order_date: string | null;
  supplier: string | null;
  qty: number | null;
  unit_price: number | null;
  source_type: string | null;
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
  display_qty: number | null;
  source_qty: number | null;
  manual_qty: number | null;
  unit_cost: number | null;
  inventory_value: number | null;
}
