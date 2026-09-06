import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { message } from "antd";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getBoardProjectOrders: vi.fn(),
  listProjectPartsRows: vi.fn(),
  getProjectProcurement: vi.fn(),
}));

vi.mock("../../../../api/maintenanceBossBoard", async () => {
  const actual = await vi.importActual<
    typeof import("../../../../api/maintenanceBossBoard")
  >("../../../../api/maintenanceBossBoard");
  return { ...actual, getBoardProjectOrders: mocks.getBoardProjectOrders };
});

vi.mock("../../../../api/maintenanceWorkbooks", async () => {
  const actual = await vi.importActual<
    typeof import("../../../../api/maintenanceWorkbooks")
  >("../../../../api/maintenanceWorkbooks");
  return { ...actual, listProjectPartsRows: mocks.listProjectPartsRows, saveBlob: vi.fn() };
});

vi.mock("../../../../api/maintenanceProjectProcurement", async () => {
  const actual = await vi.importActual<
    typeof import("../../../../api/maintenanceProjectProcurement")
  >("../../../../api/maintenanceProjectProcurement");
  return { ...actual, getProjectProcurement: mocks.getProjectProcurement };
});

import PartsOrdersTab from "../PartsOrdersTab";

const stat = <T,>(value: T) => ({ state: "ready" as const, value, as_of: null });
const notImported = () => ({ state: "not_imported" as const, value: null, as_of: null });

/** 需求单行：WBDD 单号与项目名都不含合同号——旧「包含匹配」在这种数据上永远为空。 */
const orderRow = (no: string, contract: string) => ({
  source_order_id: `RAW-${no}`,
  order_no: no,
  order_date: "2026-08-01",
  data_status: "已生效",
  linked_sales_order_no: contract,
  project_raw: "合成项目A",
  is_pre_delivery: false,
  line_count: 1,
  known_apply_cost_inc_tax: stat({
    actual_amount: "100.00", estimated_amount: "0.00", known_amount: "100.00",
    missing_lines: 0, coverage_pct: 100, quality: "actual_only",
  }),
  self_report: {
    head_demand_qty: "1", head_purchase_qty: "1",
    head_shipped_qty: "1", head_returned_qty: "0",
  },
  facts: {
    shipped_qty: stat("1.000"),
    returned_good_qty: notImported(),
    returned_bad_qty: notImported(),
  },
  facts_scope: "project" as const,
});

const lineRow = (id: number, pn: string, orderNo: string) => ({
  line_id: id, pn_std: pn, order_no: orderNo, order_date: "2026-08-01",
  sales_order_no: "XSDD-1", description: `${pn} 描述`, qty: "2", return_qty: "0",
  returned_qty: null, pending_return_qty: null, warehouse: "北京成品仓",
  unit_cost_ex_tax: "88.50", unit_cost_inc_tax: "100.00",
  cost_amount_inc_tax: "200.00", cost_source: "direct", confidence: "high",
});

const ordersPayload = (rows: ReturnType<typeof orderRow>[]) => ({
  data: { rows, total: rows.length, page: 1, page_size: 200 },
});

const emptyProcurement = {
  data: { project_id: "p1", purchases: [], total: 0, page: 1, page_size: 10 },
};

function renderTab(contractNos = ["XSDD-1", "XSDD-2"]) {
  return render(
    <PartsOrdersTab
      projectId="p1"
      exportBase="合成项目A"
      canUpload={false}
      contractNos={contractNos}
      onChanged={vi.fn().mockResolvedValue(true)}
      registerRefresh={vi.fn()}
    />,
  );
}

/** 需求单表里的单号是可点的 <a>；PN 明细表「维保单号」列是纯文本。 */
const orderLink = (no: string) =>
  screen.getAllByText(no).find((el) => el.tagName === "A");

async function chooseContract(no: string) {
  fireEvent.mouseDown(screen.getByRole("combobox"));
  const option = (await screen.findAllByText(no))
    .map((el) => el.closest(".ant-select-item-option"))
    .find(Boolean);
  expect(option).toBeTruthy();
  fireEvent.click(option!);
}

beforeEach(() => {
  vi.resetAllMocks();
  mocks.getBoardProjectOrders.mockImplementation(
    (_id: string, params?: { contract_no?: string }) => Promise.resolve(
      params?.contract_no === "XSDD-2"
        ? ordersPayload([orderRow("WBDD-2", "XSDD-2")])
        : ordersPayload([orderRow("WBDD-1", "XSDD-1"), orderRow("WBDD-2", "XSDD-2")]),
    ),
  );
  mocks.listProjectPartsRows.mockResolvedValue({
    sheet: "03_备件订单", total: 1, page: 1, page_size: 20,
    rows: [lineRow(1, "PN-1", "WBDD-1")],
  });
  mocks.getProjectProcurement.mockResolvedValue(emptyProcurement);
});

afterEach(() => {
  cleanup();
  message.destroy();
});

describe("备件与需求单 tab（#259 三处修正）", () => {
  it("合同筛选走服务端相等（不是单号包含匹配），并清空已选需求单", async () => {
    renderTab();
    await waitFor(() => expect(orderLink("WBDD-1")).toBeTruthy());
    expect(mocks.getBoardProjectOrders).toHaveBeenCalledWith("p1", {
      page: 1, page_size: 200, contract_no: undefined,
    });

    // 先点选一张需求单：PN 明细按 order_no 向服务端收敛，采购段按 raw id 收敛
    fireEvent.click(orderLink("WBDD-1")!);
    expect(await screen.findByText(/当前过滤：WBDD-1/)).toBeInTheDocument();
    await waitFor(() => expect(mocks.listProjectPartsRows).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 20, order_no: "WBDD-1", contract_no: undefined,
    }));
    await waitFor(() => expect(mocks.getProjectProcurement).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 10, source_order_id: "RAW-WBDD-1",
    }));

    // 再选合同：三段一起换范围，选中的需求单被清空
    await chooseContract("XSDD-2");
    await waitFor(() => expect(mocks.getBoardProjectOrders).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 200, contract_no: "XSDD-2",
    }));
    await waitFor(() => expect(mocks.listProjectPartsRows).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 20, order_no: undefined, contract_no: "XSDD-2",
    }));
    await waitFor(() => expect(mocks.getProjectProcurement).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 10, source_order_id: undefined,
    }));
    expect(screen.queryByText(/当前过滤：/)).toBeNull();
    // 服务端按 XSDD 相等返回的 WBDD-2：单号里没有合同号，也必须原样展示（旧包含匹配会把它滤掉）
    await waitFor(() => expect(orderLink("WBDD-2")).toBeTruthy());
    await waitFor(() => expect(orderLink("WBDD-1")).toBeUndefined());
  });

  it("采购段加载失败只影响自己：需求单与 PN 明细照常展示，且可重试", async () => {
    mocks.getProjectProcurement
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce(emptyProcurement);
    renderTab();
    expect(await screen.findByText("采购订单关联数据加载失败")).toBeInTheDocument();
    expect(orderLink("WBDD-1")).toBeTruthy();
    expect(screen.getByText("PN-1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /重\s?试/ }));
    expect(await screen.findByText("尚未找到关联采购订单")).toBeInTheDocument();
  });

  it("PN 明细加载失败不清空需求单列表", async () => {
    mocks.listProjectPartsRows.mockRejectedValue({
      response: { data: { detail: "备件明细暂不可用" } },
    });
    renderTab();
    await waitFor(() => expect(orderLink("WBDD-1")).toBeTruthy());
    expect(await screen.findByText("备件明细暂不可用")).toBeInTheDocument();
    expect(orderLink("WBDD-1")).toBeTruthy();
    expect(screen.queryByText("PN-1")).toBeNull();
  });

  it("PN 明细服务端分页：分页器按服务端 total 画页码，翻页带 page 参数", async () => {
    mocks.listProjectPartsRows.mockResolvedValue({
      sheet: "03_备件订单", total: 45, page: 1, page_size: 20,
      rows: [lineRow(1, "PN-1", "WBDD-1")],
    });
    renderTab();
    const linesTable = (await screen.findByText("PN-1")).closest<HTMLElement>(".ant-table-wrapper")!;
    expect(within(linesTable).getByTitle("3")).toBeInTheDocument();
    fireEvent.click(within(linesTable).getByTitle("2"));
    await waitFor(() => expect(mocks.listProjectPartsRows).toHaveBeenLastCalledWith("p1", {
      page: 2, page_size: 20, order_no: undefined, contract_no: undefined,
    }));
  });
});
