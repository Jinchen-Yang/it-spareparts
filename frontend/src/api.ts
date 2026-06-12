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
  specs?: Record<string, string>;
}

export interface Overview {
  part: {
    pn_std: string;
    description: string | null;
    brand: string | null;
    category_major: string | null;
    category_minor: string | null;
    unit: string | null;
    needs_review: boolean;
    redirected_from?: string | null;
  };
  purchases_recent: PurchaseRow[];
  sales_recent: SalesRow[];
  inventory: InventoryRow[];
  substitutes: {
    pn_std: string; description: string | null; source: string;
    relation?: string; substitute_type?: string | null;
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
  // 近期加权成交参考价（销售出价用，北极星）。由 PR #1 后端提供；
  // 本分支后端暂未返回 → 运行时可能为 undefined，前端按可空处理。
  sale_price_ref?: {
    ref_sale_price: number | null;   // 近期加权成交参考价
    ref_sale_samples: number;        // 取样成交笔数（0 = 窗口内无成交）
    ref_window_days: number;         // 取样窗口天数（30）
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
