/** 看板共用层单测：竞态守卫（旧响应不得覆盖新筛选结果）的确定性证据 + 窗口/下钻编解码。 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import dayjs from "dayjs";
import { drillRangeOf, rangeToDates, useGuardedFetch } from "../shared";

afterEach(cleanup);

// ---------------------------------------------------------------- useGuardedFetch 竞态

function Harness({ dep, fetcher }: { dep: string; fetcher: (dep: string) => Promise<{ data: string }> }) {
  const { data, loading, error } = useGuardedFetch<string>(() => fetcher(dep), [dep]);
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
  });
});

describe("drillRangeOf（趋势点击 → 订单窗口）", () => {
  it("日=当天；周=起点+6天；月=整月", () => {
    expect(drillRangeOf("2026-07-01", "day")).toEqual({ from: "2026-07-01", to: "2026-07-01" });
    expect(drillRangeOf("2026-06-29", "week")).toEqual({ from: "2026-06-29", to: "2026-07-05" });
    expect(drillRangeOf("2026-06-01", "month")).toEqual({ from: "2026-06-01", to: "2026-06-30" });
  });
});
