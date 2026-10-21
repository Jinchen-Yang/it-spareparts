// 金额区间筛选（D-44 §7/§10）：双端拖动条 + 两端输入框，最小步长 1k；口径单独选择
// （默认不含税）；「含负金额单」「含无金额单」两开关默认开。拖动是本地草稿，松手 /
// 输入框失焦或回车才提交进 URL（避免拖动中每一帧都触发请求）。拖到两端极值 = 不限。
// 无 data_purchase_cost 时整组禁用（含口径选择与两开关）：后端对金额区间 fail-closed
// 403，三口径一视同仁，营收也无例外（A-AC03）。输入下限高于上限时不提交，
// 提交前以警告提示（PRD §A 验收）。
import { useEffect, useState } from "react";
import { App, InputNumber, Select, Slider, Switch, Tooltip } from "antd";
import type { SalesAmountBasis } from "../../../api/salesAnalytics";
import { AMOUNT_STEP } from "./urlState";

export const AMOUNT_BASIS_OPTIONS: { value: SalesAmountBasis; label: string }[] = [
  { value: "amount_ex_tax", label: "不含税" },
  { value: "amount_inc_tax", label: "含税订单金额" },
  { value: "revenue", label: "营收" },
];

export interface AmountRangeValue {
  amountBasis: SalesAmountBasis;
  amountMin: number | null;
  amountMax: number | null;
  includeNegative: boolean;
  includeEmpty: boolean;
}

export interface AmountRangeFilterProps extends AmountRangeValue {
  /** 当前其他筛选下该口径的最大金额（来自 /options amount_range）；null = 未知 / 被遮蔽。 */
  maxAmount: number | null;
  /** data_purchase_cost=false：整组不可用（含营收口径，A-AC03 无例外）。 */
  hidden: boolean;
  onChange: (next: Partial<AmountRangeValue>) => void;
}

/** 拖动条右端：最大金额向上取整到 1k，至少 1k。 */
export function sliderCeiling(maxAmount: number | null): number {
  if (maxAmount == null || maxAmount <= 0) return AMOUNT_STEP;
  return Math.ceil(maxAmount / AMOUNT_STEP) * AMOUNT_STEP;
}

/** 输入值按 1k 取整（D-44 §7）。 */
export function snapAmount(v: number | null | undefined): number | null {
  if (v == null || !Number.isFinite(v)) return null;
  return Math.round(v / AMOUNT_STEP) * AMOUNT_STEP;
}

/** 拖动结果 → 区间：拖到 0 / 右端极值视为该端不限（null）。 */
export function rangeFromSlider(
  [lo, hi]: [number, number],
  ceiling: number,
): { amountMin: number | null; amountMax: number | null } {
  return {
    amountMin: lo <= 0 ? null : lo,
    amountMax: hi >= ceiling ? null : hi,
  };
}

/**
 * 提交前校验（PRD §A）：两端都有限值且下限高于上限 → 返回提示文案，调用方
 * 不提交并警告；null = 可提交。单端 / 不限不构成冲突。
 */
export function rangeInputError(
  min: number | null,
  max: number | null,
): string | null {
  if (min == null || max == null) return null;
  return min > max ? `金额下限（${min.toLocaleString("zh-CN")}）不能高于上限（${max.toLocaleString("zh-CN")}）` : null;
}

const fmtK = (v: number) => (v >= 10_000 ? `${(v / 10_000).toLocaleString("zh-CN")} 万` : v.toLocaleString("zh-CN"));

export default function AmountRangeFilter({
  amountBasis,
  amountMin,
  amountMax,
  includeNegative,
  includeEmpty,
  maxAmount,
  hidden,
  onChange,
}: AmountRangeFilterProps) {
  const { message } = App.useApp();
  const ceiling = Math.max(sliderCeiling(maxAmount), amountMax ?? 0, amountMin ?? 0);
  const [draft, setDraft] = useState<[number, number]>([amountMin ?? 0, amountMax ?? ceiling]);
  useEffect(() => {
    setDraft([amountMin ?? 0, amountMax ?? ceiling]);
  }, [amountMin, amountMax, ceiling]);

  const commitInput = (side: "min" | "max", raw: number | null) => {
    const v = snapAmount(raw);
    const nextMin = side === "min" ? (v != null && v > 0 ? v : null) : amountMin;
    const nextMax = side === "max" ? v : amountMax;
    const err = rangeInputError(nextMin, nextMax);
    if (err) {
      // 下限高于上限：不提交进 URL，提交前提示（PRD §A）
      message.warning(err);
      return;
    }
    if (side === "min") onChange({ amountMin: nextMin });
    else onChange({ amountMax: nextMax });
  };

  const control = (
    <div
      style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}
      aria-label="金额区间"
      role="group"
    >
      <span style={{ whiteSpace: "nowrap" }}>金额区间</span>
      <Select
        size="small"
        aria-label="金额区间口径"
        style={{ width: 128 }}
        value={amountBasis}
        disabled={hidden}
        options={AMOUNT_BASIS_OPTIONS}
        onChange={(v: SalesAmountBasis) => onChange({ amountBasis: v })}
      />
      <InputNumber
        size="small"
        aria-label="金额下限"
        placeholder="不限"
        min={0}
        step={AMOUNT_STEP}
        value={amountMin}
        disabled={hidden}
        style={{ width: 110 }}
        onBlur={(e) => commitInput("min", e.target.value === "" ? null : Number(e.target.value))}
        onPressEnter={(e) => commitInput("min", (e.target as HTMLInputElement).value === "" ? null : Number((e.target as HTMLInputElement).value))}
      />
      <Slider
        range
        aria-label="金额区间拖动条"
        style={{ width: 200, margin: "0 6px" }}
        min={0}
        max={ceiling}
        step={AMOUNT_STEP}
        value={draft}
        disabled={hidden}
        tooltip={{ formatter: (v) => (v == null ? "" : fmtK(v)) }}
        onChange={(v: number[]) => setDraft([v[0], v[1]])}
        onChangeComplete={(v: number[]) => onChange(rangeFromSlider([v[0], v[1]], ceiling))}
      />
      <InputNumber
        size="small"
        aria-label="金额上限"
        placeholder="不限"
        min={0}
        step={AMOUNT_STEP}
        value={amountMax}
        disabled={hidden}
        style={{ width: 110 }}
        onBlur={(e) => commitInput("max", e.target.value === "" ? null : Number(e.target.value))}
        onPressEnter={(e) => commitInput("max", (e.target as HTMLInputElement).value === "" ? null : Number((e.target as HTMLInputElement).value))}
      />
      <span style={{ display: "inline-flex", gap: 4, alignItems: "center", whiteSpace: "nowrap" }}>
        含负金额单
        <Switch
          size="small"
          aria-label="含负金额单"
          checked={includeNegative}
          disabled={hidden}
          onChange={(v) => onChange({ includeNegative: v })}
        />
      </span>
      <span style={{ display: "inline-flex", gap: 4, alignItems: "center", whiteSpace: "nowrap" }}>
        含无金额单
        <Switch
          size="small"
          aria-label="含无金额单"
          checked={includeEmpty}
          disabled={hidden}
          onChange={(v) => onChange({ includeEmpty: v })}
        />
      </span>
    </div>
  );
  return hidden ? (
    <Tooltip title="当前账号无成本权限（data_purchase_cost），金额区间不可用：三口径一视同仁，营收口径也无例外">
      {control}
    </Tooltip>
  ) : control;
}
