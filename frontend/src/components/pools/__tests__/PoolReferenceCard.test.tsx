import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { PoolReference, PoolReferenceSide } from "../../../api/poolAnalysis";
import PoolReferenceCard from "../PoolReferenceCard";

const side = (over: Partial<PoolReferenceSide> = {}): PoolReferenceSide => ({
  restricted: false,
  pool_stats: {
    weighted_avg: 698.2, median: 682.3, min: 420, max: 920, latest: 800,
    total_amount: 649326, total_qty: 930, order_count: 42, line_count: 51,
  },
  part_stats: {
    weighted_avg: 760, median: 735, min: 610, max: 920, latest: 800,
    total_amount: 66880, total_qty: 88, order_count: 7, line_count: 9,
  },
  constraint: { status: "set", value: 725.66 },
  delta_to_pool_avg: 61.8,
  delta_to_constraint: 34.34,
  relation_to_constraint: "above",
  ...over,
});

const reference = (over: Partial<PoolReference> = {}): PoolReference => ({
  part_id: 101,
  pn_std: "ST4000NM0035",
  pool: { group_id: 12, name: "4T SAS 3.5硬盘池", member_count: 16 },
  window: { range: "90d", date_from: "2026-04-14", date_to: "2026-07-12" },
  basis: "ex_tax",
  purchase_reference: side(),
  sales_reference: side({
    pool_stats: { ...side().pool_stats!, weighted_avg: 1198.5, median: 1180, order_count: 67 },
    part_stats: { ...side().part_stats!, weighted_avg: 1210 },
    constraint: { status: "unset", value: null },
    delta_to_pool_avg: 11.5,
    delta_to_constraint: null,
    relation_to_constraint: "unset",
  }),
  ...over,
});

afterEach(cleanup);

function renderCard(value: PoolReference, props: { side?: "both" | "purchase" | "sales" } = {}) {
  render(<MemoryRouter><PoolReferenceCard reference={value} {...props} /></MemoryRouter>);
}

describe("PoolReferenceCard", () => {
  it("展示同一池的采购/销售参考、窗口、样本与未税口径，并可进入池详情", () => {
    renderCard(reference());

    expect(screen.getByRole("region", { name: "ST4000NM0035 的池价格参考" })).toBeInTheDocument();
    expect(screen.getByText("4T SAS 3.5硬盘池")).toBeInTheDocument();
    expect(screen.getByText("16 个 PN")).toBeInTheDocument();
    const purchase = screen.getByLabelText("采购参考");
    expect(purchase).toHaveTextContent("池均价 ¥698.20");
    expect(purchase).toHaveTextContent("中位 ¥682.30");
    expect(purchase).toHaveTextContent("人工上限 ¥725.66");
    expect(purchase).toHaveTextContent("本型号均价 ¥760.00");
    expect(screen.getByText(/高于池均价.*61\.80/)).toBeInTheDocument();
    expect(screen.getByText(/高于人工约束.*34\.34/)).toBeInTheDocument();
    expect(screen.getByText("未设置")).toBeInTheDocument();
    expect(screen.getByText(/采购 42 单.*销售 67 单.*统一未税.*近 90 天/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看互通池详情" })).toHaveAttribute(
      "href",
      "/pool-analysis/12?range=90d&pn=ST4000NM0035",
    );
  });

  it("无价格权限明确显示权限状态，不以 0 或横线伪装", () => {
    renderCard(reference({ purchase_reference: side({
      restricted: true,
      pool_stats: null,
      part_stats: null,
      constraint: { status: "restricted", value: null },
      delta_to_pool_avg: null,
      delta_to_constraint: null,
      relation_to_constraint: null,
    }) }), { side: "purchase" });

    expect(screen.getByText("无价格权限")).toBeInTheDocument();
    expect(screen.getByText(/采购价格无权限/)).toBeInTheDocument();
    expect(screen.queryByText(/采购 0 单/)).toBeNull();
    expect(screen.queryByText("¥0.00")).not.toBeInTheDocument();
    expect(screen.queryByText("未设置")).not.toBeInTheDocument();
  });

  it("仅约束价受限时仍展示池价格统计，但隐藏人工约束", () => {
    renderCard(reference({ purchase_reference: side({
      constraint: { status: "restricted", value: null },
      delta_to_constraint: null,
      relation_to_constraint: null,
    }) }), { side: "purchase" });

    const purchase = screen.getByLabelText("采购参考");
    expect(purchase).toHaveTextContent("池均价 ¥698.20");
    expect(purchase).toHaveTextContent("中位 ¥682.30");
    expect(purchase).toHaveTextContent("无约束价权限");
    expect(screen.queryByText(/人工约束.*34\.34/)).toBeNull();
    expect(screen.queryByText("无价格权限")).toBeNull();
  });

  it("没有加入互通池时给出事实提示，不渲染价格", () => {
    renderCard(reference({ pool: null }));
    expect(screen.getByText("该型号尚未加入互通池")).toBeInTheDocument();
    expect(screen.queryByText(/池均价/)).not.toBeInTheDocument();
  });
});
