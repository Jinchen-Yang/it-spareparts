/** 看板共用层单测：竞态守卫（旧响应不得覆盖新筛选结果）的确定性证据 + 窗口/下钻编解码。 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import dayjs from "dayjs";
import { drillRangeOf, poolAnalysisPath, rangeToDates, useGuardedFetch } from "../shared";
import { isStrictIsoDate, strictIsoDateOrNull, strictIsoDateRange } from "../../../utils/date";

afterEach(cleanup);

// ---------------------------------------------------------------- useGuardedFetch 竞态

function Harness({ dep, fetcher, scoped = false }: { dep: string;
  fetcher: (dep: string) => Promise<{ data: string }>; scoped?: boolean }) {
  const { data, loading, error } = useGuardedFetch<string>(
    () => fetcher(dep), [dep], scoped ? dep : undefined,
  );
  return (
    <div>
      <span data-testid="data">{data ?? "(null)"}</span>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="error">{error ?? "(none)"}</span>
    </div>
  );
}

describe("useGuardedFetch 代次守卫", () => {
  it("慢的旧请求最后返回，也不得覆盖新筛选的数据（A-delayed → B-immediate → A-resolved-last）", async () => {
    let resolveA!: (v: { data: string }) => void;
    const fetcher = (dep: string) =>
      dep === "A"
        ? new Promise<{ data: string }>((res) => { resolveA = res; })   // A 挂起
        : Promise.resolve({ data: "B 的数据" });

    const { rerender } = render(<Harness dep="A" fetcher={fetcher} />);
    expect(screen.getByTestId("loading").textContent).toBe("true");

    rerender(<Harness dep="B" fetcher={fetcher} />);                    // 快速切换筛选
    await waitFor(() => expect(screen.getByTestId("data").textContent).toBe("B 的数据"));

    resolveA({ data: "A 的旧数据" });                                    // 旧请求最后才回来
    await Promise.resolve(); await Promise.resolve();
    expect(screen.getByTestId("data").textContent).toBe("B 的数据");     // 终态必须仍是 B
    expect(screen.getByTestId("loading").textContent).toBe("false");
  });

  it("接口失败与空数据分离：失败落 error，data 为 null", async () => {
    const fetcher = () => Promise.reject({ response: { status: 500 } });
    render(<Harness dep="X" fetcher={fetcher} />);
    await waitFor(() => expect(screen.getByTestId("error").textContent).toBe("接口错误（500）"));
    expect(screen.getByTestId("data").textContent).toBe("(null)");
  });

  it("scope 改变首帧立即隐藏旧数据，不等 effect 或新响应", async () => {
    let resolveB!: (v: { data: string }) => void;
    const fetcher = (dep: string) => dep === "A"
      ? Promise.resolve({ data: "A 的旧数据" })
      : new Promise<{ data: string }>((resolve) => { resolveB = resolve; });
    const { rerender } = render(<Harness dep="A" fetcher={fetcher} scoped />);
    await waitFor(() => expect(screen.getByTestId("data")).toHaveTextContent("A 的旧数据"));

    rerender(<Harness dep="B" fetcher={fetcher} scoped />);
    expect(screen.getByTestId("data")).toHaveTextContent("(null)");
    expect(screen.getByTestId("loading")).toHaveTextContent("true");

    resolveB({ data: "B 的数据" });
    await waitFor(() => expect(screen.getByTestId("data")).toHaveTextContent("B 的数据"));
  });
});

// ---------------------------------------------------------------- 窗口编解码

describe("rangeToDates", () => {
  const D = "YYYY-MM-DD";
  it("today/7d/30d/month 按当天推算", () => {
    const today = dayjs().format(D);
    expect(rangeToDates("today", null, null)).toEqual({ date_from: today, date_to: today });
    expect(rangeToDates("7d", null, null)).toEqual({
      date_from: dayjs().subtract(6, "day").format(D), date_to: today });
    expect(rangeToDates("month", null, null)).toEqual({
      date_from: dayjs().startOf("month").format(D), date_to: today });
  });
  it("custom 带全参用原值；缺参退回 30d（不产出半开窗口）", () => {
    expect(rangeToDates("custom", "2026-06-01", "2026-06-30"))
      .toEqual({ date_from: "2026-06-01", date_to: "2026-06-30" });
    expect(rangeToDates("custom", "2026-06-01", null)).toEqual({
      date_from: dayjs().subtract(29, "day").format(D), date_to: dayjs().format(D) });
    expect(rangeToDates("custom", "2026-06-30", "2026-06-01")).toEqual({
      date_from: dayjs().subtract(29, "day").format(D), date_to: dayjs().format(D) });
  });
});

describe("drillRangeOf（趋势点击 → 订单窗口）", () => {
  it("日=当天；周=起点+6天；月=整月", () => {
    expect(drillRangeOf("2026-07-01", "day")).toEqual({ from: "2026-07-01", to: "2026-07-01" });
    expect(drillRangeOf("2026-06-29", "week")).toEqual({ from: "2026-06-29", to: "2026-07-05" });
    expect(drillRangeOf("2026-06-01", "month")).toEqual({ from: "2026-06-01", to: "2026-06-30" });
  });
  it("桶末截断到全局 date_to 与今天两者中更早的一天", () => {
    expect(drillRangeOf("2026-07-13", "week", { dateTo: "2026-07-15", today: "2026-07-20" }))
      .toEqual({ from: "2026-07-13", to: "2026-07-15" });
    expect(drillRangeOf("2026-07-01", "month", { dateTo: "2026-07-31", today: "2026-07-18" }))
      .toEqual({ from: "2026-07-01", to: "2026-07-18" });
  });
  it("周/月桶起点不得早于全局 date_from", () => {
    expect(drillRangeOf("2026-07-13", "week", {
      dateFrom: "2026-07-15", dateTo: "2026-07-15", today: "2026-07-20",
    })).toEqual({ from: "2026-07-15", to: "2026-07-15" });
    expect(drillRangeOf("2026-07-01", "month", {
      dateFrom: "2026-07-10", dateTo: "2026-07-20", today: "2026-07-31",
    })).toEqual({ from: "2026-07-10", to: "2026-07-20" });
  });
});

describe("严格日期与池深链", () => {
  it("拒绝 dayjs 会自动滚入下月的不可能日期", () => {
    expect(isStrictIsoDate("2026-02-31")).toBe(false);
    expect(strictIsoDateOrNull("2026-02-31")).toBeNull();
    expect(isStrictIsoDate("2026-02-28")).toBe(true);
    expect(strictIsoDateRange("2026-06-30", "2026-06-01")).toBeNull();
  });

  it("池详情路径只携带严格合法的当前窗口", () => {
    expect(poolAnalysisPath(7, { date_from: "2026-06-01", date_to: "2026-06-30" }))
      .toBe("/pool-analysis/7?from=2026-06-01&to=2026-06-30");
    expect(poolAnalysisPath(7, { date_from: "2026-02-31", date_to: "2026-06-30" }))
      .toBe("/pool-analysis/7");
  });
});
