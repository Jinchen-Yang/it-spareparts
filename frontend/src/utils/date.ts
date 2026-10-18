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
 * 严格读取后端 ISO datetime。无偏移串代表后端存储的 UTC，不能按浏览器本地时间解析。
 * 先校验日历与时分秒，再用数字构造 UTC 时间，不依赖 Date 对非标准字符串的宽松解析。
 * Safari 对空格分隔/超长小数的兼容性不同，因此不把这些串直接交给 Date；
 * 小数秒按位补齐到毫秒，亚毫秒部分仅在数字构造时舍弃，不进位、不改变秒/分钟。
 */
function parseBusinessInstant(value: string | null | undefined): Date | null {
  if (!value) return null;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?$/.exec(value);
  if (!match || !isStrictIsoDate(match[1])) return null;
  const [, calendar, h, m, s, fraction = "", offset = "Z"] = match;
  const hour = Number(h), minute = Number(m), second = Number(s);
  if (hour > 23 || minute > 59 || second > 59) return null;
  const offsetHour = offset === "Z" ? 0 : Number(offset.slice(1, 3));
  const offsetMinute = offset === "Z" ? 0 : Number(offset.slice(4, 6));
  if (offsetHour > 23 || offsetMinute > 59) return null;
  const [year, month, day] = calendar.split("-").map(Number);
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(hour, minute, second, Number(fraction.padEnd(3, "0").slice(0, 3)));
  const direction = offset.startsWith("-") ? -1 : 1;
  date.setTime(date.getTime() - direction * (offsetHour * 60 + offsetMinute) * 60_000);
  return date;
}

/** ISO datetime → Asia/Shanghai YYYY-MM-DD HH:mm；纯日期、空值、非法值统一「—」。 */
export function formatBusinessDateTime(value: string | null | undefined): string {
  const date = parseBusinessInstant(value);
  if (!date) return "—";
  const parts = BUSINESS_DATETIME_FORMAT.formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}`;
}

/** 业务日期保留原日历日；datetime 先换算上海时区再取日期。 */
export function formatBusinessDate(value: string | null | undefined): string {
  if (isStrictIsoDate(value)) return value;
  const formatted = formatBusinessDateTime(value);
  return formatted === "—" ? formatted : formatted.slice(0, 10);
}
