// 业务日（Asia/Shanghai）：与后端 business_today 同口径，不随浏览器时区漂移。
import { describe, expect, it } from "vitest";

import { BUSINESS_TIME_ZONE, businessToday } from "../date";

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
