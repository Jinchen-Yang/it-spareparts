import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";

const { post } = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("../../../api", () => ({ api: { post } }));

import {
  cleanupAntd, render, screen, waitFor,
} from "../../../test/antdRender";
import PriceRefBadge from "../PriceRefBadge";
import { clearPriceRefCache } from "../../../api/priceRefs";
import type { PriceRef } from "../../../api/priceRefs";

const side = {
  avg_inc_tax: 113,
  avg_ex_tax: 100,
  sample_count: 2,
  order_count: 1,
  latest_date: "2026-10-19",
  oldest_date: "2026-10-12",
};

const normal: PriceRef = {
  sale: side,
  purchase: { ...side, avg_inc_tax: 90, avg_ex_tax: 79.65 },
  purchase_restricted: false,
  inquiry_ref: {
    min_money: 10, max_money: 20, last_money: 15, count: 3,
  },
  window: { start: "2026-09-20", end: "2026-10-19", days: 30 },
  method: "time_decay_qty_weighted",
};

const responseFor = (ids: number[]) => ({
  data: {
    items: Object.fromEntries(ids.map((id) => [String(id), normal])),
  },
});

beforeEach(() => {
  post.mockReset();
  post.mockImplementation(async (_url: string, body: { part_ids: number[] }) => (
    responseFor(body.part_ids)
  ));
  clearPriceRefCache();
  localStorage.clear();
  localStorage.setItem("token", "badge-account");
});

afterEach(async () => {
  await cleanupAntd();
  vi.restoreAllMocks();
  clearPriceRefCache();
});

describe("PriceRefBadge", () => {
  it("正常状态显示含税角标，悬停看双口径、计数、窗口与免责文案", async () => {
    const user = userEvent.setup();
    render(<PriceRefBadge data={normal} />);

    expect(screen.getByText(/售 ¥113/)).toBeInTheDocument();
    expect(screen.getByText(/采 ¥90/)).toBeInTheDocument();
    expect(screen.getAllByText("含税").length).toBeGreaterThanOrEqual(2);
    expect(post).not.toHaveBeenCalled();

    await user.hover(screen.getByRole("status", { name: "价格参考角标" }));
    expect(await screen.findByText("参考价，不等于定价")).toBeInTheDocument();
    expect(screen.getAllByText(/样本 2 行 \/ 1 单/).length).toBe(2);
    expect(screen.getAllByText(/2026-10-12 ~ 2026-10-19/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/共 3 条/)).toBeInTheDocument();
  });

  it("无采购价权限时显示「采购价：无权查看」", () => {
    render(<PriceRefBadge data={{ ...normal, purchase: null, purchase_restricted: true }} />);
    expect(screen.getByText("采购价：无权查看")).toBeInTheDocument();
    expect(screen.queryByText(/采 ¥/)).not.toBeInTheDocument();
  });

  it("有权但没有样本时显示「暂无数据」，与无权状态区分", () => {
    render(<PriceRefBadge data={{
      ...normal,
      sale: null,
      purchase: null,
      purchase_restricted: false,
      inquiry_ref: { min_money: null, max_money: null, last_money: null, count: 0 },
    }} />);
    expect(screen.getByText("售 暂无数据")).toBeInTheDocument();
    expect(screen.getByText("采购价：暂无数据")).toBeInTheDocument();
    expect(screen.queryByText("采购价：无权查看")).not.toBeInTheDocument();
  });

  it("不传 partId 且没有数据时不发请求、不渲染", () => {
    render(<PriceRefBadge />);
    expect(screen.queryByRole("status", { name: "价格参考角标" })).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });

  it("3 个组件同时挂载合并为 1 次请求", async () => {
    render(<>
      <PriceRefBadge partId={1} />
      <PriceRefBadge partId={2} />
      <PriceRefBadge partId={3} />
    </>);

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0][0]).toBe("/parts/price-refs");
    expect(post.mock.calls[0][1]).toEqual({
      part_ids: [1, 2, 3],
      window_days: 30,
    });
  });
});
