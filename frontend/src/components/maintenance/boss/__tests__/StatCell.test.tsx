import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import StatCell from "../StatCell";
import KnownCostCell from "../KnownCostCell";
import type { KnownCostStat, Stat } from "../../../../api/maintenanceBossBoard";

/**
 * 六态渲染唯一入口（plan v1.3 §5.3）。
 * 核心不变量：not_imported / restricted / error **绝不渲染 0 或任何数字**（铁律 5）。
 */
// globals: false → RTL 自动清理未启用，逐用例手动清理，避免 testid 跨用例重复
afterEach(cleanup);

describe("StatCell 六态", () => {
  it("ready 渲染数值", () => {
    render(<StatCell stat={{ state: "ready", value: 42, as_of: null }} />);
    expect(screen.getByTestId("stat-ready")).toHaveTextContent("42");
  });

  it("not_imported 显示「尚未导入」且不出现 0", () => {
    const stat: Stat = { state: "not_imported", value: null, as_of: null };
    const { container } = render(<StatCell stat={stat} />);
    expect(screen.getByTestId("stat-not-imported")).toHaveTextContent("尚未导入");
    expect(container.textContent).not.toMatch(/\d/);
  });

  it("restricted 显示受限且不出现数字", () => {
    const stat: Stat = { state: "restricted", value: null, as_of: null };
    const { container } = render(<StatCell stat={stat} />);
    expect(screen.getByTestId("stat-restricted")).toHaveTextContent("受限");
    expect(container.textContent).not.toMatch(/\d/);
  });

  it("error 显示加载失败且不出现数字", () => {
    const stat: Stat = { state: "error", value: null, as_of: null };
    const { container } = render(<StatCell stat={stat} />);
    expect(screen.getByTestId("stat-error")).toHaveTextContent("加载失败");
    expect(container.textContent).not.toMatch(/\d/);
  });

  it("partial 渲染已知下限值并标注部分", () => {
    render(
      <StatCell
        stat={{ state: "partial", value: 7, as_of: null, unlinked: 3 }}
      />,
    );
    const node = screen.getByTestId("stat-partial");
    expect(node).toHaveTextContent("7");
    expect(node).toHaveTextContent("部分");
  });

  it("stale 渲染值并附截止日期小注", () => {
    render(
      <StatCell stat={{ state: "stale", value: 5, as_of: "2026-05-01" }} />,
    );
    const node = screen.getByTestId("stat-stale");
    expect(node).toHaveTextContent("5");
    expect(node).toHaveTextContent("2026-05-01");
  });

  it("缺省 stat 也不渲染 0", () => {
    const { container } = render(<StatCell stat={null} />);
    expect(container.textContent).toContain("尚未导入");
    expect(container.textContent).not.toMatch(/\d/);
  });
});

describe("KnownCostCell 成本五件套", () => {
  const base: KnownCostStat = {
    state: "ready",
    as_of: null,
    value: {
      actual_amount: "100.00",
      estimated_amount: "20.00",
      known_amount: "120.00",
      missing_lines: 0,
      coverage_pct: 100,
      quality: "actual_only",
    },
  };

  it("缺价时显示「不完整 · 已知下限」并带 ≥ 号", () => {
    const incomplete: KnownCostStat = {
      ...base,
      value: { ...base.value!, missing_lines: 3, coverage_pct: 40, quality: "incomplete" },
    };
    render(<KnownCostCell stat={incomplete} />);
    const node = screen.getByTestId("cost-ready");
    expect(node).toHaveTextContent("已知下限");
    expect(node).toHaveTextContent("≥");
    expect(node).toHaveTextContent("缺价 3 行");
  });

  it("无成本权限时显示受限且无金额", () => {
    const restricted: KnownCostStat = {
      state: "restricted",
      value: null,
      as_of: null,
    };
    const { container } = render(<KnownCostCell stat={restricted} />);
    expect(screen.getByTestId("cost-restricted")).toHaveTextContent("受限");
    expect(container.textContent).not.toContain("120");
  });

  it("全部实际价时不显示已知下限文案", () => {
    render(<KnownCostCell stat={base} />);
    const node = screen.getByTestId("cost-ready");
    expect(node).toHaveTextContent("全部实际价");
    expect(node.textContent).not.toContain("已知下限");
  });
});
