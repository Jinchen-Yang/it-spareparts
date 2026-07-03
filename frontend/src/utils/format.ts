// 显示格式化工具单一真值源（此前 money 在 4 个页面、pct 在 3 个页面各复制一份）。
// 架构体检 2026-06-29 F2。pct 精度用 digits 参数保留各调用点既有行为。

export const EMPTY = "-";

export const money = (v: number | null | undefined): string =>
  v == null ? EMPTY : `¥${v.toLocaleString()}`;

export const pct = (v: number | null | undefined, digits = 1): string =>
  v == null ? EMPTY : `${(v * 100).toFixed(digits)}%`;

// 含税/不含税分列（零计算，只镜像真实值；缺的一侧 null → 显示 EMPTY，绝不用税率换算）。
export type TaxSplit = { inc: number | null; ex: number | null };

// 采购：口径跟随订单 is_tax_inclusive（含税单→含税侧、不含单→不含税侧、未标注→含税侧）。
export const splitByFlag = (v: number | null | undefined, isInc: boolean | null): TaxSplit =>
  isInc === false ? { inc: null, ex: v ?? null } : { inc: v ?? null, ex: null };

// 固定口径：销售价/参考价/询价=含税；成本/库存估值/营收=不含税。只填该侧，另一侧留空。
export const splitFixed = (v: number | null | undefined, side: "inc" | "ex"): TaxSplit =>
  side === "inc" ? { inc: v ?? null, ex: null } : { inc: null, ex: v ?? null };
