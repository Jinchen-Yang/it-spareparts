import { useEffect, useState, type ReactNode } from "react";
import { Tag, Tooltip } from "antd";
import type { PurchaseAnalysisRow } from "../../api";

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

export function PriceCell({ row, basis }: { row: PurchaseAnalysisRow; basis: string }) {
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
      flex: "1 1 150px", minWidth: 140, padding: "12px 14px", borderRadius: 8,
      background: highlight ? "var(--ant-color-warning-bg, #fdf3e3)" : "rgba(0,0,0,0.025)",
    }}>
      <div style={{ fontSize: 12.5, color: highlight ? "#9a7b43" : "#6b665e" }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 500, marginTop: 4, color: highlight ? "#9a7b43" : undefined }}>{value}</div>
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
