import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import MaintenancePnCompactRanking from "../MaintenancePnCompactRanking";

afterEach(cleanup);

describe("PN 截图条形图", () => {
  it("按服务端顺序展示完整 PN、精确值、真零和未知；同名实体不合并", () => {
    const longPn = "PN-VERY-LONG-12345678901234567890-完整型号";
    const unsafePn = "<img src=x onerror=alert(1)>";
    render(<MaintenancePnCompactRanking title="需求净量" metricLabel="需求数量减退货数量" items={[
      { key: "part:2", pn: longPn, value: "9007199254740993.001", displayValue: "9,007,199,254,740,993.001" },
      { key: "part:1", pn: longPn, value: "0", displayValue: "0" },
      { key: "raw:html", pn: unsafePn, value: null, displayValue: "未知" },
      { key: "part:3", pn: "NEGATIVE", value: "-0.001", displayValue: "-0.001", note: "保留负值" },
    ]} />);

    const chart = screen.getByRole("group", { name: "需求净量" });
    const rows = within(chart).getAllByRole("listitem");
    expect(rows).toHaveLength(4);
    expect(within(rows[0]).getByText(longPn)).toBeInTheDocument();
    expect(within(rows[0]).getByText("9,007,199,254,740,993.001")).toBeInTheDocument();
    expect(within(rows[1]).getByText("0")).toBeInTheDocument();
    expect(within(rows[2]).getByText(unsafePn)).toBeInTheDocument();
    expect(within(rows[2]).getByText("未知")).toBeInTheDocument();
    expect(chart.querySelector("img")).toBeNull();
    expect(within(rows[3]).getByText("-0.001")).toBeInTheDocument();
    expect(within(rows[3]).getByText("保留负值")).toBeInTheDocument();
    expect(screen.queryByText(/悬停/)).not.toBeInTheDocument();
    expect(chart.querySelector("canvas")).toBeNull();
  });
});
