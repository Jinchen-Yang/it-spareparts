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

// 含税/不含税分列（零计算，只镜像真实值；缺的一侧 null → 显示 EMPTY，绝不用税率换算）。
export type TaxSplit = { inc: number | null; ex: number | null };

// 采购：口径跟随订单 is_tax_inclusive（含税单→含税侧、不含单→不含税侧、未标注→含税侧）。
export const splitByFlag = (v: number | null | undefined, isInc: boolean | null): TaxSplit =>
  isInc === false ? { inc: null, ex: v ?? null } : { inc: v ?? null, ex: null };

// 固定口径：销售价/参考价/询价=含税；成本/库存估值/营收=不含税。只填该侧，另一侧留空。
export const splitFixed = (v: number | null | undefined, side: "inc" | "ex"): TaxSplit =>
  side === "inc" ? { inc: v ?? null, ex: null } : { inc: null, ex: v ?? null };
