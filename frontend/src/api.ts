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
