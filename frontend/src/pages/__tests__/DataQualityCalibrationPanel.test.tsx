import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Grid, message } from "antd";
import type { PurchasePriceCalibration } from "../../api/dataQuality";

const getPurchasePriceCalibration = vi.fn();

vi.mock("../../api/dataQuality", () => ({
  getPurchasePriceCalibration: (...args: unknown[]) => getPurchasePriceCalibration(...args),
}));

import DataQualityCalibrationPanel from "../governance/DataQualityCalibrationPanel";

const breakpoint = vi.spyOn(Grid, "useBreakpoint");

const PREVIEW: PurchasePriceCalibration = {
  rule_code: "purchase_adjacent_price_ratio",
  rule_version: "preview-v1",
  generated_at: "2026-07-16T10:00:00Z",
  data_through: "2026-07-15",
  eligible_pairs: 120,
  distinct_parts: 36,
  thresholds: [2, 3, 5, 10].map((threshold, index) => ({
    threshold,
    eligible_pairs: 120,
    candidate_count: [12, 7, 3, 1][index],
    candidate_rate: [0.1, 0.0583, 0.025, 0.0083][index],
    increase_count: [8, 5, 2, 1][index],
    decrease_count: [4, 2, 1, 0][index],
  })),
  purchase_types: [{
    purchase_type: "销售订单",
    eligible_pairs: 80,
    thresholds: [
      { threshold: 2, eligible_pairs: 80, candidate_count: 9, candidate_rate: 0.1125,
        increase_count: 6, decrease_count: 3 },
      { threshold: 3, eligible_pairs: 80, candidate_count: 4, candidate_rate: 0.05,
        increase_count: 3, decrease_count: 1 },
    ],
  }],
  sample_boundary: {
    limit_per_threshold_direction: 6,
    ordering: "md5(preview-v1:previous_line_id:current_line_id), line ids",
    contains_people_or_parties: false,
  },
  samples: [{
    threshold: 2,
    direction: "increase",
    ratio: 2.5,
    pn_std: "PN-4T",
    purchase_type: "销售订单",
    current: { line_id: 12, order_no: "CG-2", order_date: "2026-07-15", quantity: 2,
      unit: "块", tax_basis: "inc_tax_or_unknown_div_1_13", unit_price_ex_tax: 1000 },
    previous: { line_id: 8, order_no: "CG-1", order_date: "2026-07-01", quantity: 1,
      unit: "块", tax_basis: "ex_tax_original", unit_price_ex_tax: 400 },
  }],
};

function login(pageGovernance = true, purchaseCost = true) {
  localStorage.setItem("role", "readonly");
  localStorage.setItem("permissions", JSON.stringify({
    page_governance: pageGovernance,
    data_purchase_cost: purchaseCost,
  }));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  message.destroy();
  breakpoint.mockReturnValue({ xs: false, sm: true, md: true, lg: true, xl: true, xxl: true });
  getPurchasePriceCalibration.mockResolvedValue(PREVIEW);
});

afterEach(() => {
  cleanup();
  message.destroy();
});

describe("规则校准预览", () => {
  it("醒目标明只读模拟，不生成疑点也不改变经营数据，并展示固定四档", async () => {
    login();
    render(<DataQualityCalibrationPanel />);

    expect(await screen.findByText("仅为模拟预览，不会生成数据疑点")).toBeInTheDocument();
    expect(screen.getByText(/不会修改采购、利润、库存、池均价或员工排名/)).toBeInTheDocument();
    // 提示文案同步出现，指标要等异步预览请求完成；全量并发测试下不能假设同一 tick 返回。
    expect(await screen.findByText("可比相邻对")).toBeInTheDocument();
    expect(screen.getByText("涉及 PN")).toBeInTheDocument();
    for (const threshold of [2, 3, 5, 10]) {
      expect(screen.getByText(`${threshold} 倍档`)).toBeInTheDocument();
    }
    expect(screen.getByText("12 条候选")).toBeInTheDocument();
    expect(screen.getByText("10.00%")).toBeInTheDocument();
    expect(screen.getByText("变贵 8 · 变便宜 4")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /启用|保存阈值|批量生成疑点|确认错误/ })).toBeNull();
  });

  it("显示采购类型和涨跌分布，并用确定性样本对照前后两笔采购", async () => {
    login();
    render(<DataQualityCalibrationPanel />);

    expect(await screen.findByText("采购类型分布")).toBeInTheDocument();
    expect(screen.getAllByText("销售订单").length).toBeGreaterThan(0);
    const samples = screen.getByLabelText("确定性抽样样本");
    expect(within(samples).getByText("PN-4T")).toBeInTheDocument();
    expect(within(samples).getByText("本次变贵")).toBeInTheDocument();
    expect(within(samples).getByText("2.50 倍")).toBeInTheDocument();
    expect(within(samples).getByText("CG-1 · 2026-07-01")).toBeInTheDocument();
    expect(within(samples).getByText("CG-2 · 2026-07-15")).toBeInTheDocument();
    expect(within(samples).getByText(/¥400.00 · 1 块 · 原值不含税/)).toBeInTheDocument();
    expect(within(samples).getByText(/¥1,000.00 · 2 块 · 含税或未知，已÷1.13换算/)).toBeInTheDocument();
  });

  it("无采购成本权限时给出明确无权限状态，且绝不发送预览请求", async () => {
    login(true, false);
    render(<DataQualityCalibrationPanel />);
    expect(screen.getByText("无采购成本查看权限")).toBeInTheDocument();
    expect(screen.getByText(/倍率、候选排序和样本价格都可能反推采购成本/)).toBeInTheDocument();
    expect(getPurchasePriceCalibration).not.toHaveBeenCalled();
  });

  it("无数据治理页面权限时不加载内容，也不发送预览请求", () => {
    login(false, true);
    render(<DataQualityCalibrationPanel />);
    expect(screen.getByText("无数据治理页面权限")).toBeInTheDocument();
    expect(getPurchasePriceCalibration).not.toHaveBeenCalled();
  });

  it("当前筛选加载失败时清空旧结果并给出可重试错态", async () => {
    login();
    render(<DataQualityCalibrationPanel />);
    expect(await screen.findByText("PN-4T")).toBeInTheDocument();
    getPurchasePriceCalibration.mockRejectedValueOnce(new Error("network"));
    fireEvent.click(screen.getByRole("button", { name: "刷新模拟预览" }));

    expect(await screen.findByText("校准预览加载失败，旧结果已清空。"))
      .toBeInTheDocument();
    expect(screen.queryByText("PN-4T")).toBeNull();
    expect(screen.getByRole("button", { name: /重\s*试/ })).toBeInTheDocument();
  });

  it("旧筛选响应最后返回时不会覆盖最新预览", async () => {
    login();
    const oldRequest = deferred<PurchasePriceCalibration>();
    const newRequest = deferred<PurchasePriceCalibration>();
    getPurchasePriceCalibration.mockReset();
    getPurchasePriceCalibration
      .mockImplementationOnce(() => oldRequest.promise)
      .mockImplementationOnce(() => newRequest.promise);
    render(<DataQualityCalibrationPanel />);
    fireEvent.change(screen.getByText("本次采购起始日").closest("label")!.querySelector("input")!, {
      target: { value: "2026-07-01" },
    });
    await waitFor(() => expect(getPurchasePriceCalibration).toHaveBeenCalledTimes(2));
    newRequest.resolve({ ...PREVIEW, samples: [{ ...PREVIEW.samples[0], pn_std: "LATEST-PN" }] });
    expect(await screen.findByText("LATEST-PN")).toBeInTheDocument();
    oldRequest.resolve(PREVIEW);
    await waitFor(() => expect(screen.queryByText("PN-4T")).toBeNull());
    expect(screen.getByText("LATEST-PN")).toBeInTheDocument();
  });

  it("390px 使用纵向指标卡与样本卡片，不渲染宽表", async () => {
    login();
    breakpoint.mockReturnValue({ xs: true, sm: false, md: false, lg: false, xl: false, xxl: false });
    render(<DataQualityCalibrationPanel />);
    expect(await screen.findByText("PN-4T")).toBeInTheDocument();
    expect(screen.getByTestId("calibration-threshold-grid")).toHaveStyle({ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" });
    expect(screen.getByTestId("calibration-mobile-samples")).toBeInTheDocument();
    expect(screen.getByText("原值不含税", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("含税或未知，已÷1.13换算", { exact: false })).toBeInTheDocument();
    expect(screen.queryByText(/ex_tax_original|inc_tax_or_unknown_div_1_13/)).toBeNull();
    expect(document.querySelector(".ant-table")).toBeNull();
  });
});
