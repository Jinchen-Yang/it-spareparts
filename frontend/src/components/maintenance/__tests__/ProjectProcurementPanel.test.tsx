import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  getProjectProcurement: vi.fn(),
}));

vi.mock("../../../api/maintenanceProjectProcurement", async () => {
  const actual = await vi.importActual<
    typeof import("../../../api/maintenanceProjectProcurement")
  >("../../../api/maintenanceProjectProcurement");
  return { ...actual, ...api };
});

import ProjectProcurementPanel, { unitPriceText } from "../ProjectProcurementPanel";

type Line = {
  pn: string; description: string; qty: string;
  unit_price: string | null; unit_price_masked: boolean;
};

const order = (no: string, lines: Line[]) => ({
  purchase_order_no: no,
  purchase_date: "2026-07-30",
  purchaser: "采购甲",
  demand_source_order_id: "RAW-1",
  demand_order_no: "WBDD-1",
  demand_date: "2026-08-01",
  line_count: lines.length,
  lines,
});

const payload = (purchases: ReturnType<typeof order>[], total = purchases.length) => ({
  data: { project_id: "p1", purchases, total, page: 1, page_size: 10 },
});

beforeEach(() => {
  vi.resetAllMocks();
});

afterEach(() => {
  cleanup();
});

describe("ProjectProcurementPanel（#259 挂到备件与需求单 tab 第三段）", () => {
  it("按选中需求单收敛请求；单价区分「无权查看」与「缺失」", async () => {
    api.getProjectProcurement.mockResolvedValue(payload([
      order("PO-1", [
        { pn: "PN-A", description: "有价行", qty: "2.000", unit_price: "88.00", unit_price_masked: false },
        { pn: "PN-B", description: "脱敏行", qty: "1.000", unit_price: null, unit_price_masked: true },
        { pn: "PN-C", description: "缺价行", qty: "1.000", unit_price: null, unit_price_masked: false },
      ]),
    ]));
    const { container } = render(
      <ProjectProcurementPanel projectId="p1" sourceOrderId="RAW-1" sourceOrderNo="WBDD-1" />,
    );
    expect(await screen.findByText("PO-1")).toBeInTheDocument();
    expect(api.getProjectProcurement).toHaveBeenCalledWith("p1", {
      page: 1, page_size: 10, source_order_id: "RAW-1",
    });
    // 范围提示在卡体里而不是 extra：375px 下长 extra 会把标题挤成省略号
    expect(screen.getByText(/当前范围：需求单 WBDD-1/).closest(".ant-card-body")).not.toBeNull();
    expect(container.querySelector(".ant-card-extra")).toBeNull();

    fireEvent.click(container.querySelector(".ant-table-row-expand-icon")!);
    expect(await screen.findByText("¥88.00")).toBeInTheDocument();
    expect(screen.getByText("无权查看")).toBeInTheDocument();
    expect(screen.getByText("—（缺失）")).toBeInTheDocument();
  });

  it("unitPriceText：masked 优先于 null，null 未脱敏才是缺失", () => {
    expect(unitPriceText({ unit_price: null, unit_price_masked: true })).toBe("无权查看");
    expect(unitPriceText({ unit_price: "1.00", unit_price_masked: true })).toBe("无权查看");
    expect(unitPriceText({ unit_price: null, unit_price_masked: false })).toBe("—（缺失）");
    expect(unitPriceText({ unit_price: "1234.5", unit_price_masked: false })).toBe("¥1,234.50");
  });

  it("加载失败给出告警与重试，重试后重新请求", async () => {
    api.getProjectProcurement
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce(payload([]));
    render(<ProjectProcurementPanel projectId="p1" />);
    expect(await screen.findByText("采购订单关联数据加载失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /重\s?试/ }));
    expect(await screen.findByText("尚未找到关联采购订单")).toBeInTheDocument();
    expect(api.getProjectProcurement).toHaveBeenCalledTimes(2);
    expect(api.getProjectProcurement).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 10, source_order_id: undefined,
    });
  });

  it("服务端分页：翻页带 page 参数；范围一换页码回到 1", async () => {
    api.getProjectProcurement.mockResolvedValue(payload([order("PO-1", [])], 25));
    const { rerender } = render(<ProjectProcurementPanel projectId="p1" />);
    await screen.findByText("PO-1");
    fireEvent.click(screen.getByTitle("2"));
    await waitFor(() => expect(api.getProjectProcurement).toHaveBeenLastCalledWith("p1", {
      page: 2, page_size: 10, source_order_id: undefined,
    }));

    rerender(<ProjectProcurementPanel projectId="p1" sourceOrderId="RAW-2" sourceOrderNo="WBDD-2" />);
    await waitFor(() => expect(api.getProjectProcurement).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 10, source_order_id: "RAW-2",
    }));
  });

  it("向所在 tab 登记读回函数：调用即按当前范围重读，失败 resolve false；卸载登记 null", async () => {
    api.getProjectProcurement.mockResolvedValue(payload([order("PO-1", [])]));
    const registerRefresh = vi.fn();
    const { unmount } = render(
      <ProjectProcurementPanel projectId="p1" sourceOrderId="RAW-1" registerRefresh={registerRefresh} />,
    );
    await screen.findByText("PO-1");
    const registered = registerRefresh.mock.calls.filter(([fn]) => fn);
    const refresh = registered[registered.length - 1][0] as () => Promise<boolean>;

    await act(async () => {
      expect(await refresh()).toBe(true);
    });
    expect(api.getProjectProcurement).toHaveBeenCalledTimes(2);
    expect(api.getProjectProcurement).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 10, source_order_id: "RAW-1",
    });

    api.getProjectProcurement.mockRejectedValueOnce(new Error("boom"));
    await act(async () => {
      expect(await refresh()).toBe(false);
    });
    expect(await screen.findByText("采购订单关联数据加载失败")).toBeInTheDocument();

    unmount();
    expect(registerRefresh).toHaveBeenLastCalledWith(null);
  });
});
