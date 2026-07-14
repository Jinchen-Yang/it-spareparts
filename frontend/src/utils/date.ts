import dayjs from "dayjs";

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
