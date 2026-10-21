import { formatMoney } from "../../utils/format";
import type { ReactNode } from "react";
import type { SalesReconciliationBlock } from "../../api/salesAnalytics";
import { formatBusinessDateTime } from "../../utils/date";

// 销售展示件按模块就近维护；金额格式与采购共用 utils/format。

export const fmt = (v: number | null | undefined) =>
  v == null ? "—" : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
// 金额场景固定两位小数（含 ¥0.00 对齐）；数量/比值用 fmt。null 是「缺失/不可见」，
// 一律显示「—」，绝不当 0（D-41 §3：不用推算值冒充）。
export const fmtMoney = (v: number | null | undefined) => formatMoney(v);

/**
 * 「数据截至时间」等 datetime 展示：后端存 UTC（cleaner.parse_datetime），isoformat
 * 带偏移输出（如 2026-08-01T01:00:00+00:00）；按偏移换算到业务时区 Asia/Shanghai
 * 显示「2026-08-01 09:00」。空值/非法值「—」。
 */
export const fmtDateTime = formatBusinessDateTime;

/**
 * 对账块「无数据」：无快照（has_data=false），或金额与时间字段全为 null（后端按
 * data_reconciliation 遮蔽、或快照本身全空）。不只依赖本地权限快照——本地快照
 * 可能过期，以响应实际内容为准，避免整行「—」却不提示无数据。
 */
export function isReconBlockEmpty(b: SalesReconciliationBlock | null | undefined): boolean {
  if (!b || !b.has_data) return true;
  return [
    b.reconciled_as_of, b.order_amount_at_snapshot, b.reconciled_amount,
    b.pending_amount, b.return_reconciled_amount, b.reconciled_display,
  ].every((v) => v == null);
}

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
