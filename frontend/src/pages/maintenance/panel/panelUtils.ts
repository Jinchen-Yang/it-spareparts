import { createElement } from "react";
import { Tag } from "antd";

/**
 * 项目面板各 tab 的共享渲染件（2026-08-19 面板重设计拆出）：
 * 非 ready 状态一律说人话、绝不落 0（铁律 5）；流转状态列只展示不计算（铁律 3）。
 */

/** 导出文件名片段清洗：去掉路径/非法字符，避免项目名破坏文件名（2026-08-17）。 */
export function safeFilenamePart(value: string): string {
  return value.replace(/[\\/:*?"<>|\r\n\t]+/g, "_").trim().replace(/\.+$/, "") || "项目";
}

/** 数值渲染：非 ready 状态一律说人话，绝不落回 0（铁律 5）。 */
export function statText(stat: { state: string; value: unknown } | undefined): string {
  if (!stat) return "—";
  if (stat.state === "not_imported") return "尚未导入";
  if (stat.state === "restricted") return "无权限";
  if (stat.state === "error") return "暂不可用";
  return stat.value === null || stat.value === "" ? "—" : String(stat.value);
}

export function raw(value: unknown) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

export const COST_SOURCE_LABEL: Record<string, string> = {
  direct: "直接采购价",
  window: "7天采购窗口",
  purchase_history: "采购历史",
  pool_purchase: "备件池采购",
  sales_history: "销售历史",
  pool_sales: "备件池销售",
  month_avg: "月均价",
  none: "暂无成本",
};

/** CostSourceTag 的最小入参：ProjectPartsRow 天然满足，board 行级明细取 Stat 值后组装。 */
export interface CostSourceLike {
  cost_source?: string | null;
  cost_source_label?: string | null;
  confidence?: "high" | "medium" | "low" | "none" | null;
  missing_kind?: "out_of_scope" | "none" | null;
}

export function CostSourceTag({ row }: { row: CostSourceLike }) {
  const confidence = row.confidence ?? (row.cost_source === "none" ? "none" : null);
  const color = confidence === "high" ? "green"
    : confidence === "medium" ? "orange"
      : confidence === "low" ? "red" : "default";
  const label = row.cost_source_label || COST_SOURCE_LABEL[row.cost_source || ""] || raw(row.cost_source);
  const suffix = row.missing_kind === "out_of_scope" ? "（起算日前）"
    : row.missing_kind === "none" ? "（未找到）" : "";
  return createElement(Tag, { color }, `${label}${suffix}`);
}

export const LIFECYCLE_LABEL: Record<string, string> = {
  ongoing: "进行中",
  ended: "已结束",
  missing: "期限缺失",
};

/** 三态色与卡墙一致（#35/#43）：<80% 绿、80–100% 黄、>100% 红。 */
export const STATUS_COLOR: Record<string, string> = {
  normal: "#52c41a",
  warning: "#faad14",
  alert: "#ff4d4f",
};

export const COLLECTION_STATUS: Record<string, { label: string; color: string }> = {
  confirmed: { label: "已确认", color: "green" },
  unconfirmed: { label: "待确认", color: "gold" },
  void: { label: "已作废", color: "default" },
};

export const ISSUE_STATUS: Record<string, { label: string; color: string }> = {
  draft: { label: "领用草稿", color: "gold" },
  confirmed: { label: "领用已确认", color: "green" },
  corrected: { label: "领用已更正", color: "blue" },
  void: { label: "领用已作废", color: "default" },
};

export const RETURN_DOCUMENT_STATUS: Record<string, string> = {
  draft: "返还草稿",
  submitted: "已提交返还",
  in_transit: "返还在途",
  warehouse_confirmed: "仓库已确认",
  void: "返还已作废",
};

export function readError(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response
    ?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  return fallback;
}
