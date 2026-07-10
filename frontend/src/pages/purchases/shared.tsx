import { useCallback, type KeyboardEvent, type ReactNode } from "react";
import { Tag, Tooltip } from "antd";
import type { SetURLSearchParams } from "react-router-dom";
import type { PurchaseAnalysisRow, RecentPurchaseRow } from "../../api";
import type { TaxBasis } from "../../context/TaxBasis";

// 采购三页共用的展示辅助。全部从原 PurchasesPage.tsx 逐字搬运，零逻辑改动。

// 流程状态：颜色 + 过滤选项（宋总：取消单也要能看/能统计）
export const STATUS_COLOR: Record<string, string> = {
  已生效: "green", 已取消: "red", 作废: "red", 进行中: "blue", 草稿: "default",
};
export const STATUS_FILTER = [
  { label: "已生效", value: "已生效" },
  { label: "已取消", value: "已取消" },
  { label: "进行中", value: "进行中" },
  { label: "全部", value: "全部" },
];
export const GRAN_OPTIONS = [
  { label: "按月", value: "month" },
  { label: "按季", value: "quarter" },
  { label: "按年", value: "year" },
];
export const DAY_OPTIONS = [
  { label: "近 7 天", value: 7 },
  { label: "近 30 天", value: 30 },
  { label: "近 90 天", value: 90 },
  { label: "近一年", value: 365 },
];
export const ANALYSIS_DAYS = [
  { label: "近 7 天", value: 7 },
  { label: "近 14 天", value: 14 },
  { label: "近 30 天", value: 30 },
  { label: "近 90 天", value: 90 },
];
export const ADVICE_COLOR: Record<string, string> = {
  批量补库: "gold", 谈价: "blue", 偶发: "default",
};

export const fmt = (v: number | null | undefined) =>
  v == null ? "—" : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
// 金额场景固定两位小数（含¥0.00 对齐）；数量/比值用 fmt
export const fmtMoney = (v: number | null | undefined) =>
  v == null ? "—" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
// 单价/金额按订单税口径归列（零计算，只镜像 Excel 原值；缺的一侧留空 → "—"）
export const byTax = (v: number | null | undefined, isInc: boolean | null) =>
  isInc === false ? { inc: null, ex: v ?? null } : { inc: v ?? null, ex: null };

export function Sparkline({ data, accent }: { data: number[] | null; accent: string }) {
  if (!data || data.length === 0) return <span style={{ color: "var(--mb-text-3)" }}>—</span>;
  const max = Math.max(...data, 1);
  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 22 }}>
      {data.map((v, i) => (
        <Tooltip key={i} title={`${v}`}>
          <div style={{
            width: 5, height: Math.max(2, Math.round((v / max) * 22)),
            background: v > 0 ? accent : "#e9e5de",
            opacity: v > 0 ? 0.6 : 1, borderRadius: 1,
          }} />
        </Tooltip>
      ))}
    </div>
  );
}

export function TrendArrow({ t }: { t: PurchaseAnalysisRow["price_trend"] }) {
  if (t === "up") return <span style={{ color: "#c0524a" }}>↑</span>;
  if (t === "down") return <span style={{ color: "#3f7a45" }}>↓</span>;
  return <span style={{ color: "var(--mb-text-3)" }}>→</span>;
}

export function PriceCell({ row, basis }: { row: PurchaseAnalysisRow; basis: TaxBasis }) {
  const line = (label: string, min: number | null, max: number | null, last: number | null) => (
    <div style={{ whiteSpace: "nowrap" }}>
      {label && <span style={{ color: "var(--mb-text-3)" }}>{label} </span>}
      {fmt(min)}–{fmt(max)}
      <span style={{ color: "#6b665e" }}> · 最近 {fmt(last)} </span>
      {last != null && <TrendArrow t={row.price_trend} />}
    </div>
  );
  if (basis === "both")
    return (
      <>
        {line("含", row.price_inc_min, row.price_inc_max, row.price_inc_last)}
        {line("不含", row.price_ex_min, row.price_ex_max, row.price_ex_last)}
      </>
    );
  if (basis === "ex") return line("", row.price_ex_min, row.price_ex_max, row.price_ex_last);
  return line("", row.price_inc_min, row.price_inc_max, row.price_inc_last);
}

export function KpiCard({ label, value, sub, highlight }: {
  label: string; value: ReactNode; sub?: string; highlight?: boolean;
}) {
  return (
    <div style={{
      flex: "1 1 190px", minWidth: 160, padding: "12px 14px", borderRadius: 8,
      overflow: "hidden",
      background: highlight ? "var(--ant-color-warning-bg, #fdf3e3)" : "rgba(0,0,0,0.025)",
    }}>
      <div style={{ fontSize: 12.5, color: highlight ? "#9a7b43" : "#6b665e" }}>{label}</div>
      {/* 值区允许换行、断词，避免「两列」口径下含/不含金额横向溢出撞进相邻卡 */}
      <div style={{
        fontSize: 20, fontWeight: 500, marginTop: 4, lineHeight: 1.25,
        whiteSpace: "normal", overflowWrap: "anywhere",
        color: highlight ? "#9a7b43" : undefined,
      }}>{value}</div>
      {sub && <div style={{ fontSize: 11.5, color: highlight ? "#9a7b43" : "var(--mb-text-3)", marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

/** URL query <-> 状态的小工具：读取时带类型默认值 */
export function readNum(sp: URLSearchParams, key: string, def: number): number {
  const raw = sp.get(key);
  if (raw == null) return def;
  const n = Number(raw);
  return Number.isFinite(n) ? n : def;
}

/**
 * 三页共用的 URL query 增量更新：合并、空值删除（含 "" / null / undefined），replace 不堆历史。
 * 传 "" 或 undefined 即从 URL 删除该键——搜索框清除走这条路，条件真正下线。
 */
export function useUrlPatch(sp: URLSearchParams, setSp: SetURLSearchParams) {
  return useCallback(
    (next: Record<string, string | number | undefined | null>) => {
      const merged = new URLSearchParams(sp);
      for (const [k, v] of Object.entries(next)) {
        if (v === undefined || v === null || v === "") merged.delete(k);
        else merged.set(k, String(v));
      }
      setSp(merged, { replace: true });
    },
    [sp, setSp],
  );
}

/**
 * 让非按钮元素（如 List.Item）具备可访问的"按钮"语义：
 * role=button + tabIndex 可聚焦 + Enter/Space 触发 + 可访问名称。返回可展开到元素上的 props。
 */
export function activatableProps(onActivate: () => void, label: string) {
  return {
    role: "button" as const,
    tabIndex: 0,
    "aria-label": label,
    onClick: onActivate,
    onKeyDown: (e: KeyboardEvent) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onActivate(); }
    },
  };
}

/**
 * 移动端卡片里的单价随全局含税/不含税口径显示，并标明价格属性（含/不含）。
 * 零计算，仅按订单税口径把 unit_price 归到含/不含一侧（同桌面双列语义）：
 * - inc：只看含税价，非含税订单显示 "—"
 * - ex：只看不含税价，含税订单显示 "—"
 * - both：按本单实际口径标 "含 X" 或 "不含 X"
 */
export function mobileUnitPrice(row: RecentPurchaseRow, basis: TaxBasis): ReactNode {
  const { inc, ex } = byTax(row.unit_price, row.is_tax_inclusive);
  if (basis === "inc") return <>单价(含税) {fmtMoney(inc)}</>;
  if (basis === "ex") return <>单价(不含税) {fmtMoney(ex)}</>;
  // both：本单要么含税要么不含税，只有一侧有值
  const isInc = row.is_tax_inclusive;
  if (isInc == null) return <>单价 —</>;
  return <>单价{isInc ? "(含税)" : "(不含税)"} {fmtMoney(isInc ? inc : ex)}</>;
}
