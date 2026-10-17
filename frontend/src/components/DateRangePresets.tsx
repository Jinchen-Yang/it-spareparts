// 日期预设选择（D-39 / Q10）：全部/一年/半年/30天/15天/7天/今天/昨天/前天 + 自定义区间。
// 采购分析（PR-4a）与销售分析（PR-4b）共用；默认今天（Q10），按业务时区 Asia/Shanghai 计算，
// 「全部」= 不传 date_from/date_to（后端约定：不传即全部历史）。
import dayjs, { type Dayjs } from "dayjs";
import { DatePicker, Select, Typography } from "antd";
import { ISO_DATE_FORMAT, businessToday, isStrictIsoDate, strictIsoDateRange } from "../utils/date";

export type DatePreset =
  | "all"
  | "1y"
  | "6m"
  | "30d"
  | "15d"
  | "7d"
  | "today"
  | "yesterday"
  | "before_yesterday"
  | "custom";

export const DEFAULT_DATE_PRESET: DatePreset = "today";

export const DATE_PRESET_OPTIONS: { value: DatePreset; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "1y", label: "一年" },
  { value: "6m", label: "半年" },
  { value: "30d", label: "30 天" },
  { value: "15d", label: "15 天" },
  { value: "7d", label: "7 天" },
  { value: "today", label: "今天" },
  { value: "yesterday", label: "昨天" },
  { value: "before_yesterday", label: "前天" },
  { value: "custom", label: "自定义" },
];

const PRESET_KEYS = new Set(DATE_PRESET_OPTIONS.map((o) => o.value));

export function isDatePreset(v: string | null | undefined): v is DatePreset {
  return v != null && PRESET_KEYS.has(v as DatePreset);
}

export interface DateRange {
  from: string;
  to: string;
}

/**
 * 预设 → 闭区间（YYYY-MM-DD，本地日期）。返回 null = 「全部」（不传日期）。
 * 滚动窗口一律「N 天含今天」（subtract(N-1)），与全仓既有口径一致：
 * boss/shared.tsx 近 7/30 天、PurchaseRecordsPage/PoolAnalysisPage、
 * 旧采购分析后端 purchase_analysis.py 的 N 天日桶（since = until - (days-1)）。
 * 一年/半年按日历月相减（dayjs 对 02-29、01-31 等溢出日期钉回目标月末：
 * 2024-02-29 − 1 年 = 2023-02-28，2024-03-31 − 6 月 = 2023-09-30）。
 */
export function resolvePresetRange(
  preset: DatePreset,
  today: Dayjs,
): DateRange | null {
  switch (preset) {
    case "all":
      return null;
    case "1y":
      return wrap(today.subtract(1, "year"), today);
    case "6m":
      return wrap(today.subtract(6, "month"), today);
    case "30d":
      return wrap(today.subtract(29, "day"), today);
    case "15d":
      return wrap(today.subtract(14, "day"), today);
    case "7d":
      return wrap(today.subtract(6, "day"), today);
    case "today":
      return wrap(today, today);
    case "yesterday":
      return wrap(today.subtract(1, "day"), today.subtract(1, "day"));
    case "before_yesterday":
      return wrap(today.subtract(2, "day"), today.subtract(2, "day"));
    case "custom":
      return null;
  }
}

function wrap(from: Dayjs, to: Dayjs): DateRange {
  return { from: from.format(ISO_DATE_FORMAT), to: to.format(ISO_DATE_FORMAT) };
}

/**
 * 完整解析（含自定义区间）：custom 需要 from ≤ to 的合法 ISO 日期，
 * 非法时回退默认「今天」（请求侧永远拿到一个确定区间，只有 all 显式返回 null）。
 */
export function resolveDateRange(
  preset: DatePreset,
  from: string | null | undefined,
  to: string | null | undefined,
  today: Dayjs,
): DateRange | null {
  if (preset === "custom") {
    if (isStrictIsoDate(from) && isStrictIsoDate(to)) {
      const range = strictIsoDateRange(from, to);
      if (range) return { from: range.from, to: range.to };
    }
    return wrap(today, today);
  }
  return resolvePresetRange(preset, today);
}

export interface DateRangePresetsProps {
  preset: DatePreset;
  from: string | null;
  to: string | null;
  /** preset 切到 custom 时把当前生效区间快照进自定义值（状态始终完整、可分享）。 */
  onChange: (next: { preset: DatePreset; from: string | null; to: string | null }) => void;
  disabled?: boolean;
}

export default function DateRangePresets({
  preset,
  from,
  to,
  onChange,
  disabled,
}: DateRangePresetsProps) {
  const today = businessToday();
  const current = resolveDateRange(preset, from, to, today);
  // 自定义区间非法（格式错/开始晚于结束/缺边）时请求侧已回退今天，这里如实提示
  const customInvalid = preset === "custom" && !strictIsoDateRange(from, to);

  const pickPreset = (next: DatePreset) => {
    if (next !== "custom") {
      onChange({ preset: next, from: null, to: null });
      return;
    }
    // 自定义：以当前区间为起点，用户再在 RangePicker 里改
    onChange({
      preset: "custom",
      from: current?.from ?? today.format(ISO_DATE_FORMAT),
      to: current?.to ?? today.format(ISO_DATE_FORMAT),
    });
  };

  return (
    <span style={{ display: "inline-flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <Select
        aria-label="日期预设"
        style={{ width: 108 }}
        value={preset}
        onChange={pickPreset}
        disabled={disabled}
        options={DATE_PRESET_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
      />
      {preset === "custom" && (
        <DatePicker.RangePicker
          value={
            isStrictIsoDate(from) && isStrictIsoDate(to) && from <= to
              ? [dayjs(from), dayjs(to)]
              : null
          }
          onChange={(vals) => {
            if (!vals || !vals[0] || !vals[1]) return;
            onChange({
              preset: "custom",
              from: vals[0].format(ISO_DATE_FORMAT),
              to: vals[1].format(ISO_DATE_FORMAT),
            });
          }}
          disabled={disabled}
          allowEmpty={[false, false]}
        />
      )}
      {customInvalid && (
        <Typography.Text type="warning" role="alert">
          自定义区间无效（日期格式错误或开始晚于结束），已按今天查询
        </Typography.Text>
      )}
    </span>
  );
}
