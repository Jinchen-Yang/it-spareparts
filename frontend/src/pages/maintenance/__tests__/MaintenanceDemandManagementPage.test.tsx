import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const {
  armMaintenanceDemandDeleteIntent,
  cancelMaintenanceDemandDeleteIntent,
  createMaintenanceDemandDeleteIntent,
  executeMaintenanceDemandDeleteIntent,
  searchMaintenanceDemands,
} = vi.hoisted(() => ({
  armMaintenanceDemandDeleteIntent: vi.fn(),
  cancelMaintenanceDemandDeleteIntent: vi.fn(),
  createMaintenanceDemandDeleteIntent: vi.fn(),
  executeMaintenanceDemandDeleteIntent: vi.fn(),
  searchMaintenanceDemands: vi.fn(),
}));

vi.mock("../../../api/maintenanceDemands", () => ({
  armMaintenanceDemandDeleteIntent,
  cancelMaintenanceDemandDeleteIntent,
  createMaintenanceDemandDeleteIntent,
  executeMaintenanceDemandDeleteIntent,
  searchMaintenanceDemands,
}));

import MaintenanceDemandManagementPage from "../MaintenanceDemandManagementPage";

const first = {
  source_order_id: "RAW-1",
  order_no: "WBDD-20260809-0001",
  order_date: "2026-08-01",
  project: "合成项目一",
  project_raw: "合成项目一",
  linked_sales_order_no: "XSDD-1",
  line_count: 2,
  downstream_references: [
    { kind: "sales_order", label: "关联销售单 XSDD-1", reference_id: "XSDD-1" },
  ],
  version_digest: "a".repeat(64),
};
const second = {
  ...first,
  source_order_id: "RAW-2",
  order_no: "WBDD-20260809-0002",
  project: "合成项目二",
  version_digest: "b".repeat(64),
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "admin");
  localStorage.setItem("permissions", JSON.stringify({
    page_maintenance: true,
    action_maintenance_demand_delete: true,
  }));
  searchMaintenanceDemands.mockImplementation(({ page }: { page: number }) => Promise.resolve({
    data: {
      items: page === 1 ? [first] : [second],
      total: 2,
      page,
      page_size: 1,
    },
  }));
  createMaintenanceDemandDeleteIntent.mockResolvedValue({
    data: {
      intent_id: "intent-1",
      status: "reviewed",
      selection_digest: "c".repeat(64),
      reason: "重复导入",
      operated_by: "admin-user",
      header_count: 2,
      line_count: 4,
      created_at: "2026-08-09T12:00:00Z",
      not_before: null,
      expires_at: "2026-08-09T12:15:00Z",
      executed_at: null,
      items: [first, second],
      result: null,
    },
  });
  armMaintenanceDemandDeleteIntent.mockResolvedValue({
    data: {
      intent_id: "intent-1",
      status: "armed_wait",
      selection_digest: "c".repeat(64),
      reason: "重复导入",
      operated_by: "admin-user",
      header_count: 2,
      line_count: 4,
      created_at: "2026-08-09T12:00:00Z",
      not_before: new Date(Date.now() + 7_000).toISOString(),
      expires_at: new Date(Date.now() + 900_000).toISOString(),
      executed_at: null,
      items: [first, second],
      result: null,
    },
  });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("MaintenanceDemandManagementPage", () => {
  it("keeps stable selections across pages and reviews every header before arming", async () => {
    render(
      <MemoryRouter>
        <MaintenanceDemandManagementPage pageSize={1} />
      </MemoryRouter>,
    );
    const rowOne = await screen.findByText(first.order_no);
    fireEvent.click(within(rowOne.closest("tr")!).getByRole("checkbox"));
    fireEvent.click(screen.getByTitle("2"));
    const rowTwo = await screen.findByText(second.order_no);
    fireEvent.click(within(rowTwo.closest("tr")!).getByRole("checkbox"));
    fireEvent.click(screen.getByTitle("1"));
    const returnedRowOne = await screen.findByText(first.order_no);
    expect(within(returnedRowOne.closest("tr")!).getByRole("checkbox")).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "复核并删除（2）" }));
    expect(await screen.findByText("完整复核清单（2 张 / 4 行）")).toBeInTheDocument();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(first.order_no)).toBeInTheDocument();
    expect(within(dialog).getByText(second.order_no)).toBeInTheDocument();

    const prepare = screen.getByRole("button", { name: "生成服务端完整复核清单" });
    expect(prepare).toBeDisabled();
    fireEvent.change(screen.getByLabelText("删除理由"), { target: { value: "重复导入" } });
    fireEvent.click(prepare);

    await waitFor(() => expect(createMaintenanceDemandDeleteIntent).toHaveBeenCalledWith(
      expect.objectContaining({ source_order_ids: ["RAW-1", "RAW-2"], reason: "重复导入" }),
    ));
    fireEvent.click(await screen.findByRole("button", {
      name: "第一次确认并开始 7 秒等待",
    }));
    await waitFor(() => expect(armMaintenanceDemandDeleteIntent).toHaveBeenCalledWith(
      "intent-1",
      "c".repeat(64),
    ));
    expect(await screen.findByRole("button", { name: /第二次确认删除/ })).toBeDisabled();
  });

  it("removes an individual header from the complete review before creating an intent", async () => {
    render(
      <MemoryRouter>
        <MaintenanceDemandManagementPage pageSize={1} />
      </MemoryRouter>,
    );
    const rowOne = await screen.findByText(first.order_no);
    fireEvent.click(within(rowOne.closest("tr")!).getByRole("checkbox"));
    fireEvent.click(screen.getByTitle("2"));
    const rowTwo = await screen.findByText(second.order_no);
    fireEvent.click(within(rowTwo.closest("tr")!).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "复核并删除（2）" }));

    const dialog = await screen.findByRole("dialog");
    const reviewedSecond = within(dialog).getByText(second.order_no).closest("tr")!;
    fireEvent.click(within(reviewedSecond).getByRole("button", { name: "移出" }));

    expect(await within(dialog).findByText("完整复核清单（1 张 / 2 行）"))
      .toBeInTheDocument();
    expect(within(dialog).queryByText(second.order_no)).toBeNull();
    fireEvent.change(within(dialog).getByLabelText("删除理由"), {
      target: { value: "只删除第一张" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "生成服务端完整复核清单" }));
    await waitFor(() => expect(createMaintenanceDemandDeleteIntent).toHaveBeenCalledWith(
      expect.objectContaining({ source_order_ids: ["RAW-1"], reason: "只删除第一张" }),
    ));
  });

  it("read-only users can search but cannot select or start delete", async () => {
    localStorage.setItem("role", "purchaser");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      action_maintenance_demand_delete: false,
    }));
    render(
      <MemoryRouter>
        <MaintenanceDemandManagementPage pageSize={1} />
      </MemoryRouter>,
    );
    const row = (await screen.findByText(first.order_no)).closest("tr")!;
    expect(within(row).queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /复核并删除/ })).toBeNull();
  });

  it("keeps shared admin credentials read-only when delete permission is false", async () => {
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      action_maintenance_demand_delete: false,
    }));
    render(
      <MemoryRouter>
        <MaintenanceDemandManagementPage pageSize={1} />
      </MemoryRouter>,
    );
    const row = (await screen.findByText(first.order_no)).closest("tr")!;
    expect(within(row).queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("button", { name: /复核并删除/ })).toBeNull();
  });

  it("treats client countdown as advisory and obeys a server 425 retry window", async () => {
    const reviewed = {
      intent_id: "intent-clock",
      status: "reviewed",
      selection_digest: "d".repeat(64),
      reason: "服务端倒计时验证",
      operated_by: "admin-user",
      header_count: 1,
      line_count: 2,
      created_at: "2026-08-09T12:00:00Z",
      not_before: null,
      expires_at: "2026-08-09T12:15:00Z",
      executed_at: null,
      items: [first],
      result: null,
    };
    createMaintenanceDemandDeleteIntent.mockResolvedValueOnce({ data: reviewed });
    armMaintenanceDemandDeleteIntent.mockResolvedValueOnce({
      data: {
        ...reviewed,
        status: "armed_wait",
        not_before: new Date(Date.now() - 1).toISOString(),
      },
    });
    executeMaintenanceDemandDeleteIntent.mockRejectedValueOnce({
      isAxiosError: true,
      response: {
        status: 425,
        data: {
          detail: {
            message: "服务端七秒安全等待尚未结束",
            server_now: "2026-08-09T12:00:00.000Z",
            not_before: "2026-08-09T12:00:07.000Z",
          },
        },
      },
    });

    render(
      <MemoryRouter>
        <MaintenanceDemandManagementPage pageSize={1} />
      </MemoryRouter>,
    );
    const row = (await screen.findByText(first.order_no)).closest("tr")!;
    fireEvent.click(within(row).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "复核并删除（1）" }));
    fireEvent.change(screen.getByLabelText("删除理由"), {
      target: { value: "服务端倒计时验证" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成服务端完整复核清单" }));
    fireEvent.click(await screen.findByRole("button", {
      name: "第一次确认并开始 7 秒等待",
    }));
    const execute = await screen.findByRole("button", { name: "第二次确认删除" });
    fireEvent.click(execute);

    expect(await screen.findByText("服务端七秒安全等待尚未结束")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /第二次确认删除（7 秒）/ }))
      .toBeDisabled();
  });
});
