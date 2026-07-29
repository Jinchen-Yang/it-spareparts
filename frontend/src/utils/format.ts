// 显示格式化工具单一真值源（此前 money 在 4 个页面、pct 在 3 个页面各复制一份）。
// 架构体检 2026-06-29 F2。pct 精度用 digits 参数保留各调用点既有行为。

export const EMPTY = "-";

export const money = (v: number | null | undefined): string =>
  v == null ? EMPTY : `¥${v.toLocaleString()}`;

export const pct = (v: number | null | undefined, digits = 1): string =>
  v == null ? EMPTY : `${(v * 100).toFixed(digits)}%`;

// ===== 图表底座补充（dashboard-chart-foundation）=====
// 空值语义全局约定：null/undefined 一律渲染 EMPTY，绝不折算成 0——
// "没有数据"和"金额为 0"是两个业务事实，图表和表格都不得混淆。

/** 数量：千分位、最多 3 位小数（与看板/利润页既有 num 口径一致）。 */
export const qty = (v: number | null | undefined): string =>
  v == null ? EMPTY : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 3 });

/** 精确金额（tooltip/明细用）：千分位、最多 2 位小数。负数沿用全站 "¥-1,234.5" 形态。 */
export const moneyExact = (v: number | null | undefined): string =>
  v == null ? EMPTY : `¥${Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;

/** 轴刻度金额：压缩成 万/亿，去尾零（轴上寸土寸金，精确值交给 tooltip）。 */
export const moneyAxis = (v: number | null | undefined): string => {
  if (v == null) return EMPTY;
  const n = Number(v);
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  const trim = (x: number) => String(parseFloat(x.toFixed(1)));
  if (abs >= 1e8) return `${sign}¥${trim(abs / 1e8)}亿`;
  if (abs >= 1e4) return `${sign}¥${trim(abs / 1e4)}万`;
  return `${sign}¥${trim(abs)}`;
};

/** 带符号百分比（同比/环比）：正值显式带 +，让方向不依赖颜色。 */
export const pctSigned = (v: number | null | undefined, digits = 1): string => {
  if (v == null) return EMPTY;
  const s = (v * 100).toFixed(digits);
  return v > 0 ? `+${s}%` : `${s}%`;
};

/** tooltip HTML 转义：PN/描述/客户名等任何进自定义 formatter 的字符串必须先过这里。 */
export const escapeHtml = (s: string | null | undefined): string =>
  s == null ? "" : s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

// 采购、销售统一按 13% 增值税率补齐双口径。API 已明确给出双值时优先使用 API；
// 只有一侧原始值时才走这里换算，避免“选择含税后整列为空”。
export type TaxSplit = { inc: number | null; ex: number | null };

export const STANDARD_VAT_RATE = 0.13;

/**
 * 与 PostgreSQL round(numeric, 2) / 后端 Decimal ROUND_HALF_UP 对齐。
 * JSON 数字进入 JS 后是二进制浮点，2.675 * 100 可能落在 267.499…；
 * 按量级补一个极小容差，再对绝对值取整，才能同时保证正负中点远离 0。
 */
const roundMoney = (value: number): number => {
  const scaled = Math.abs(value) * 100;
  const tolerance = Number.EPSILON * Math.max(1, scaled) * 4;
  return Math.sign(value) * Math.round(scaled + tolerance) / 100;
};

/** 从一侧原始金额按固定 13% 税率得到完整含税/未税金额。 */
export function splitFixed(
  value: number | null | undefined,
  sourceBasis: "inc" | "ex",
): TaxSplit {
  if (value == null) return { inc: null, ex: null };
  const source = roundMoney(value);
  return sourceBasis === "inc"
    ? { inc: source, ex: roundMoney(source / (1 + STANDARD_VAT_RATE)) }
    : { inc: roundMoney(source * (1 + STANDARD_VAT_RATE)), ex: source };
}

/**
 * 优先保留 API 明确给出的双值，只在其中一侧缺失时按固定 13% 补齐。
 * 这使后端未来返回更精确的逐侧金额时不会被前端估算覆盖。
 */
export function completeTaxPair(
  inc: number | null | undefined,
  ex: number | null | undefined,
): TaxSplit {
  if (inc != null && ex != null) return { inc, ex };
  if (inc != null) return splitFixed(inc, "inc");
  if (ex != null) return splitFixed(ex, "ex");
  return { inc: null, ex: null };
}

// 采购：明确含税时原值为含税；明确未税或未标注时原值统一按未税处理。
export const splitByFlag = (
  value: number | null | undefined,
  isInc: boolean | null,
): TaxSplit => splitFixed(value, isInc === true ? "inc" : "ex");
