// 业务日（Asia/Shanghai）：与后端 business_today 同口径，不随浏览器时区漂移。
import { afterEach, describe, expect, it, vi } from "vitest";

import { resolvePresetRange } from "../../components/DateRangePresets";
import { BUSINESS_TIME_ZONE, businessToday, formatBusinessDate, formatBusinessDateTime } from "../date";

describe("businessToday：按 Asia/Shanghai 取业务日", () => {
  it("业务时区固定为 Asia/Shanghai", () => {
    expect(BUSINESS_TIME_ZONE).toBe("Asia/Shanghai");
  });

  it("UTC 16:00 是上海次日零点：跨日边界两侧", () => {
    expect(businessToday(new Date("2026-10-17T15:59:59Z")).format("YYYY-MM-DD")).toBe("2026-10-17");
    expect(businessToday(new Date("2026-10-17T16:00:00Z")).format("YYYY-MM-DD")).toBe("2026-10-18");
  });

  it("跨月/跨年边界", () => {
    expect(businessToday(new Date("2026-10-31T16:30:00Z")).format("YYYY-MM-DD")).toBe("2026-11-01");
    expect(businessToday(new Date("2026-12-31T16:00:00Z")).format("YYYY-MM-DD")).toBe("2027-01-01");
  });

  it("返回值可直接做日期加减（预设区间用）", () => {
    const d = businessToday(new Date("2026-03-01T03:00:00Z"));
    expect(d.subtract(1, "day").format("YYYY-MM-DD")).toBe("2026-02-28");
  });
});

describe("formatBusinessDateTime：带偏移 ISO → Asia/Shanghai「YYYY-MM-DD HH:mm」", () => {
  it("+00:00 输入显示北京时间", () => {
    expect(formatBusinessDateTime("2026-10-15T01:30:00+00:00")).toBe("2026-10-15 09:30");
  });

  it("08:00Z → 16:00", () => {
    expect(formatBusinessDateTime("2026-10-15T08:00:00Z")).toBe("2026-10-15 16:00");
  });

  it("跨午夜：UTC 当天 16:00 起是上海次日（含跨月/跨年）", () => {
    expect(formatBusinessDateTime("2026-10-15T16:05:00+00:00")).toBe("2026-10-16 00:05");
    expect(formatBusinessDateTime("2026-10-31T23:59:00+00:00")).toBe("2026-11-01 07:59");
    expect(formatBusinessDateTime("2026-12-31T16:00:00Z")).toBe("2027-01-01 00:00");
  });

  it("非 UTC 偏移按偏移换算（+08:00 原样、-05:00 加 13 小时）", () => {
    expect(formatBusinessDateTime("2026-10-15T09:00:00+08:00")).toBe("2026-10-15 09:00");
    expect(formatBusinessDateTime("2026-10-15T09:00:00-05:00")).toBe("2026-10-15 22:00");
  });

  it("null / undefined / 空串 / 非法值 → 「—」", () => {
    for (const v of [null, undefined, "", "not-a-date", "2026-13-45T99:00:00Z"]) {
      expect(formatBusinessDateTime(v)).toBe("—");
    }
  });
});

// 前端 tsconfig 不带 @types/node；测试运行在 Node 下，仅声明用到的 env
declare const process: { env: Record<string, string | undefined> };

describe("时区无关性：同一时刻在任意机器时区下结果一致", () => {
  const ORIGINAL_TZ = process.env.TZ;
  afterEach(() => {
    if (ORIGINAL_TZ === undefined) delete process.env.TZ;
    else process.env.TZ = ORIGINAL_TZ;
    vi.useRealTimers();
  });

  // Node 运行时改 process.env.TZ 即时生效：逐个机器时区重放，结果必须逐字相同
  it.each(["UTC", "Asia/Shanghai", "America/Los_Angeles"])("TZ=%s", (tz) => {
    process.env.TZ = tz;
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-17T16:30:00Z")); // 上海 2026-10-18 00:30
    const today = businessToday();
    expect(today.format("YYYY-MM-DD")).toBe("2026-10-18");
    expect(resolvePresetRange("7d", today)).toEqual({ from: "2026-10-12", to: "2026-10-18" });
    expect(resolvePresetRange("yesterday", today)).toEqual({ from: "2026-10-17", to: "2026-10-17" });
    expect(formatBusinessDateTime("2026-10-15T08:00:00+00:00")).toBe("2026-10-15 16:00");
  });
});


describe("严格 ISO 业务时间输入", () => {
  it("无偏移 datetime 按后端 UTC，跨午夜换日", () => {
    expect(formatBusinessDateTime("2026-10-01T08:00:00")).toBe("2026-10-01 16:00");
    expect(formatBusinessDateTime("2026-10-01T16:00:00")).toBe("2026-10-02 00:00");
  });
  it.each(["1", "123456", "123456789", "999999999"])("小数秒 %s 不移动分钟（包括 Safari 不支持的长小数）", (fraction) => {
    expect(formatBusinessDateTime(`2026-10-15T15:59:59.${fraction}+00:00`)).toBe("2026-10-15 23:59");
    expect(formatBusinessDateTime(`2026-10-15T16:00:00.${fraction}`)).toBe("2026-10-16 00:00");
  });
  it.each(["2026-10-01", "2026-02-29T08:00:00Z", "2026-04-31T08:00:00Z", "2026-10-01 08:00:00", "2026-10-01T24:00:00Z", "2026-10-01T08:60:00Z", "2026-10-01T08:00:60Z", "2026-10-01T08:00:00+24:00", "2026-10-01T08:00:00+08:60"])("非法/非 datetime %s → —", (value) => {
    expect(formatBusinessDateTime(value)).toBe("—");
  });
  it("日期 helper 保留纯业务日期，时间戳则先换算业务时区", () => {
    expect(formatBusinessDate("2026-10-01")).toBe("2026-10-01");
    expect(formatBusinessDate("2026-10-01T16:00:00Z")).toBe("2026-10-02");
    expect(formatBusinessDate("2026-02-30")).toBe("—");
    expect(formatBusinessDate(null)).toBe("—");
  });
});
