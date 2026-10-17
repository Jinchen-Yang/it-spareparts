import type { ReactNode } from "react";

// 销售分析页展示辅助。fmt / fmtMoney / KpiCard 从采购分析页（PR-4a，
// pages/purchases/shared.tsx）逐字搬运，零逻辑改动——按仓库惯例展示件按模块就近
// 一份（维保分析页同款先例），不做跨模块 import。

export const fmt = (v: number | null | undefined) =>
  v == null ? "—" : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
// 金额场景固定两位小数（含 ¥0.00 对齐）；数量/比值用 fmt。null 是「缺失/不可见」，
// 一律显示「—」，绝不当 0（D-41 §3：不用推算值冒充）。
export const fmtMoney = (v: number | null | undefined) =>
  v == null ? "—" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/**
 * ISO datetime（如 2026-08-01T09:00:00+00:00）→ 「2026-08-01 09:00」。
 * 按原字面截断展示、不做时区换算：后端返回的是氚云修改时间原值，换算成本地时区
 * 反而与原件对不上。
 */
export const fmtDateTime = (v: string | null | undefined) =>
  v ? v.replace("T", " ").slice(0, 16) : "—";

export function KpiCard({ label, value, sub, highlight }: {
  label: ReactNode; value: ReactNode; sub?: ReactNode; highlight?: boolean;
}) {
  return (
    <div style={{
      flex: "1 1 190px", minWidth: 160, padding: "12px 14px", borderRadius: 8,
      overflow: "hidden",
      background: highlight ? "var(--ant-color-warning-bg, #fdf3e3)" : "rgba(0,0,0,0.025)",
    }}>
      <div style={{ fontSize: 12.5, color: highlight ? "#9a7b43" : "#6b665e" }}>{label}</div>
      <div style={{
        fontSize: 20, fontWeight: 500, marginTop: 4, lineHeight: 1.25,
        whiteSpace: "normal", overflowWrap: "anywhere",
        color: highlight ? "#9a7b43" : undefined,
      }}>{value}</div>
      {sub && <div style={{ fontSize: 11.5, color: highlight ? "#9a7b43" : "var(--mb-text-3)", marginTop: 2 }}>{sub}</div>}
    </div>
  );
}
