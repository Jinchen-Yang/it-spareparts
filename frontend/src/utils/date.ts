import dayjs, { type Dayjs } from "dayjs";

export const ISO_DATE_FORMAT = "YYYY-MM-DD";

/**
 * Strict calendar-date validation without relying on dayjs customParseFormat.
 * dayjs accepts impossible dates by rolling them into the next month, so the
 * formatted round-trip is required (for example 2026-02-31 must be rejected).
 */
export function isStrictIsoDate(value: string | null | undefined): value is string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = dayjs(value);
  return parsed.isValid() && parsed.format(ISO_DATE_FORMAT) === value;
}

export function strictIsoDateOrNull(value: string | null | undefined): string | null {
  return isStrictIsoDate(value) ? value : null;
}

export function strictIsoDateRange(
  from: string | null | undefined,
  to: string | null | undefined,
): { from: string; to: string } | null {
  if (!isStrictIsoDate(from) || !isStrictIsoDate(to)) return null;
  if (dayjs(from).isAfter(dayjs(to), "day")) return null;
  return { from, to };
}

/** 业务时区：与后端 app.business_time.BUSINESS_TZ 同口径（甲方按中国标准时间看「今天」）。 */
export const BUSINESS_TIME_ZONE = "Asia/Shanghai";

/**
 * 业务日「今天」（Asia/Shanghai），不受浏览器/系统时区影响。
 * 返回该日历日的本地零点 dayjs，只用于日期加减与 YYYY-MM-DD 格式化。
 */
export function businessToday(now: Date = new Date()): Dayjs {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: BUSINESS_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? "";
  return dayjs(`${part("year")}-${part("month")}-${part("day")}`);
}

const BUSINESS_DATETIME_FORMAT = new Intl.DateTimeFormat("zh-CN", {
  timeZone: BUSINESS_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/**
 * 带时区偏移的 ISO datetime（后端统一存 UTC、isoformat 输出 +00:00）→ 业务时区
 * Asia/Shanghai 的「YYYY-MM-DD HH:mm」。按偏移换算后再格式化，不受浏览器时区影响；
 * 空值与无法解析的值一律「—」（不把原串当时间展示）。
 */
export function formatBusinessDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const parts = BUSINESS_DATETIME_FORMAT.formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}`;
}
