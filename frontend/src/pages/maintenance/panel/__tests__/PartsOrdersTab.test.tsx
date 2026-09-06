import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

const lineRow = (id: number, pn: string, orderNo: string, contract = "XSDD-1") => ({
  line_id: id, pn_std: pn, order_no: orderNo, order_date: "2026-08-01",
  sales_order_no: contract, description: `${pn} 描述`, qty: "2", return_qty: "0",
  returned_qty: null, pending_return_qty: null, warehouse: "北京成品仓",
  unit_cost_ex_tax: "88.50", unit_cost_inc_tax: "100.00",
  cost_amount_inc_tax: "200.00", cost_source: "direct", confidence: "high",
});

/** 两张需求单各一行：PN 明细 mock 按 order_no / contract_no 真过滤，断言不靠无关文本撑着。 */
const LINES = [
  lineRow(1, "PN-1", "WBDD-1", "XSDD-1"),
  lineRow(2, "PN-2", "WBDD-2", "XSDD-2"),
];

const ordersPayload = (rows: ReturnType<typeof orderRow>[]) => ({
  data: { rows, total: rows.length, page: 1, page_size: 200 },
});

const emptyProcurement = {
  data: { project_id: "p1", purchases: [], total: 0, page: 1, page_size: 10 },
};

function renderTab(contractNos = ["XSDD-1", "XSDD-2"], registerRefresh = vi.fn()) {
  return render(
    <PartsOrdersTab
      projectId="p1"
      exportBase="合成项目A"
      canUpload={false}
      contractNos={contractNos}
      onChanged={vi.fn().mockResolvedValue(true)}
      registerRefresh={registerRefresh}
    />,
  );
}

/** 需求单表里的单号是可点的 <a>；PN 明细表「维保单号」列是纯文本。找不到时返回 undefined 而不是抛。 */
const orderLink = (no: string) =>
  screen.queryAllByText(no).find((el) => el.tagName === "A");

/** 页面向父级最后一次登记的本 tab 读回函数（落库后的读回屏障）。 */
const lastRegistered = (registerRefresh: ReturnType<typeof vi.fn>) => {
  const registered = registerRefresh.mock.calls
    .filter(([key, fn]) => key === "parts-orders" && fn);
  expect(registered.length).toBeGreaterThan(0);
  return registered[registered.length - 1][1] as () => Promise<boolean>;
};

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
  mocks.listProjectPartsRows.mockImplementation(
    (_id: string, params?: { order_no?: string; contract_no?: string; page?: number }) => {
      const rows = LINES.filter((row) =>
        (!params?.order_no || row.order_no === params.order_no)
        && (!params?.contract_no || row.sales_order_no === params.contract_no));
      return Promise.resolve({
        sheet: "03_备件订单", total: rows.length, page: params?.page ?? 1, page_size: 20, rows,
      });
    },
  );
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
    expect(await screen.findByText("PN-2")).toBeInTheDocument();

    // 先点选一张需求单：PN 明细按 order_no 向服务端收敛，采购段按 raw id 收敛
    fireEvent.click(orderLink("WBDD-1")!);
    expect(await screen.findByText(/当前过滤：WBDD-1/)).toBeInTheDocument();
    await waitFor(() => expect(mocks.listProjectPartsRows).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 20, order_no: "WBDD-1", contract_no: undefined,
    }));
    await waitFor(() => expect(mocks.getProjectProcurement).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 10, source_order_id: "RAW-WBDD-1",
    }));
    await waitFor(() => expect(screen.queryByText("PN-2")).toBeNull());

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
    // PN 明细同样只剩 XSDD-2 的行；上面的断言不再靠 PN 表里残留的 WBDD-1 文本撑着
    expect(await screen.findByText("PN-2")).toBeInTheDocument();
    expect(screen.queryByText("PN-1")).toBeNull();
    expect(screen.queryAllByText("WBDD-1")).toHaveLength(0);
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

  it("PN 明细停在第 2 页时，点选需求单或换合同都回到第 1 页", async () => {
    mocks.listProjectPartsRows.mockResolvedValue({
      sheet: "03_备件订单", total: 45, page: 1, page_size: 20,
      rows: [lineRow(1, "PN-1", "WBDD-1")],
    });
    renderTab();
    const linesTable = (await screen.findByText("PN-1")).closest<HTMLElement>(".ant-table-wrapper")!;
    fireEvent.click(within(linesTable).getByTitle("2"));
    await waitFor(() => expect(mocks.listProjectPartsRows).toHaveBeenLastCalledWith("p1", {
      page: 2, page_size: 20, order_no: undefined, contract_no: undefined,
    }));

    // 第 2 页 → 点选需求单 → 第 1 页
    fireEvent.click(orderLink("WBDD-1")!);
    await waitFor(() => expect(mocks.listProjectPartsRows).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 20, order_no: "WBDD-1", contract_no: undefined,
    }));

    fireEvent.click(within(linesTable).getByTitle("2"));
    await waitFor(() => expect(mocks.listProjectPartsRows).toHaveBeenLastCalledWith("p1", {
      page: 2, page_size: 20, order_no: "WBDD-1", contract_no: undefined,
    }));

    // 第 2 页 → 换合同 → 第 1 页（选中也清空）
    await chooseContract("XSDD-2");
    await waitFor(() => expect(mocks.listProjectPartsRows).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 20, order_no: undefined, contract_no: "XSDD-2",
    }));
  });

  it("落库后 PN 明细总数收缩、当前页越界时夹到最后一页重读，不停在空白页", async () => {
    const registerRefresh = vi.fn();
    mocks.listProjectPartsRows.mockResolvedValue({
      sheet: "03_备件订单", total: 45, page: 1, page_size: 20,
      rows: [lineRow(1, "PN-1", "WBDD-1")],
    });
    renderTab(["XSDD-1", "XSDD-2"], registerRefresh);
    const linesTable = (await screen.findByText("PN-1")).closest<HTMLElement>(".ant-table-wrapper")!;
    fireEvent.click(within(linesTable).getByTitle("3"));
    await waitFor(() => expect(mocks.listProjectPartsRows).toHaveBeenLastCalledWith("p1", {
      page: 3, page_size: 20, order_no: undefined, contract_no: undefined,
    }));

    // 回传把明细收缩到 3 行：第 3 页为空但 total>0，必须夹到第 1 页重读
    mocks.listProjectPartsRows.mockImplementation(
      (_id: string, params?: { page?: number }) => Promise.resolve(
        params?.page === 1
          ? { sheet: "03_备件订单", total: 3, page: 1, page_size: 20, rows: [lineRow(9, "PN-9", "WBDD-1")] }
          : { sheet: "03_备件订单", total: 3, page: params?.page ?? 1, page_size: 20, rows: [] },
      ),
    );
    await act(async () => {
      expect(await lastRegistered(registerRefresh)()).toBe(true);
    });
    expect(await screen.findByText("PN-9")).toBeInTheDocument();
    await waitFor(() => expect(mocks.listProjectPartsRows).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 20, order_no: undefined, contract_no: undefined,
    }));
    await waitFor(() => expect(
      within(linesTable).getByTitle("1").closest("li"),
    ).toHaveClass("ant-pagination-item-active"));
  });

  it("需求单表分页受控：合同筛选收窄列表时回到第 1 页，而不是被 antd 夹到最后一页", async () => {
    const many = Array.from({ length: 25 }, (_, i) =>
      orderRow(`WBDD-${String(i + 1).padStart(2, "0")}`, "XSDD-1"));
    const narrowed = Array.from({ length: 12 }, (_, i) =>
      orderRow(`WBDD-B${String(i + 1).padStart(2, "0")}`, "XSDD-2"));
    mocks.getBoardProjectOrders.mockImplementation(
      (_id: string, params?: { contract_no?: string }) => Promise.resolve(
        ordersPayload(params?.contract_no === "XSDD-2" ? narrowed : many)),
    );
    renderTab();
    const ordersTable = (await screen.findByText("WBDD-01")).closest<HTMLElement>(".ant-table-wrapper")!;
    fireEvent.click(within(ordersTable).getByTitle("3"));
    expect(await within(ordersTable).findByText("WBDD-21")).toBeInTheDocument();

    await chooseContract("XSDD-2");
    // 12 张单只有 2 页：非受控分页会停在被夹住的第 2 页（WBDD-B11 起），受控后回第 1 页
    expect(await within(ordersTable).findByText("WBDD-B01")).toBeInTheDocument();
    expect(within(ordersTable).queryByText("WBDD-B11")).toBeNull();
    expect(within(ordersTable).getByTitle("1").closest("li"))
      .toHaveClass("ant-pagination-item-active");
  });

  it("落库后的读回屏障把采购段一起算进去：三段都重读，采购段失败则屏障为 false", async () => {
    const registerRefresh = vi.fn();
    renderTab(["XSDD-1", "XSDD-2"], registerRefresh);
    await waitFor(() => expect(orderLink("WBDD-1")).toBeTruthy());
    await waitFor(() => expect(mocks.getProjectProcurement).toHaveBeenCalledTimes(1));
    const before = {
      orders: mocks.getBoardProjectOrders.mock.calls.length,
      lines: mocks.listProjectPartsRows.mock.calls.length,
      procurement: mocks.getProjectProcurement.mock.calls.length,
    };

    // 03 回传（will_reassign_orders）/ 概览挂靠后父页调用本 tab 的读回：采购链的唯一输入
    // （需求单归属）变了，采购段必须跟着重读，否则「已覆盖并刷新」对它是假的
    await act(async () => {
      expect(await lastRegistered(registerRefresh)()).toBe(true);
    });
    expect(mocks.getBoardProjectOrders).toHaveBeenCalledTimes(before.orders + 1);
    expect(mocks.listProjectPartsRows).toHaveBeenCalledTimes(before.lines + 1);
    expect(mocks.getProjectProcurement).toHaveBeenCalledTimes(before.procurement + 1);

    mocks.getProjectProcurement.mockRejectedValueOnce(new Error("boom"));
    await act(async () => {
      expect(await lastRegistered(registerRefresh)()).toBe(false);
    });
    expect(await screen.findByText("采购订单关联数据加载失败")).toBeInTheDocument();
  });

  it("读回后选中的需求单已不在列表：清范围并提示，两段按新范围重读，屏障仍为 true", async () => {
    const registerRefresh = vi.fn();
    renderTab(["XSDD-1", "XSDD-2"], registerRefresh);
    await waitFor(() => expect(orderLink("WBDD-1")).toBeTruthy());
    fireEvent.click(orderLink("WBDD-1")!);
    await waitFor(() => expect(mocks.getProjectProcurement).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 10, source_order_id: "RAW-WBDD-1",
    }));

    // 03 回传把 WBDD-1 改派走了：读回只剩 WBDD-2
    mocks.getBoardProjectOrders.mockResolvedValue(ordersPayload([orderRow("WBDD-2", "XSDD-2")]));
    await act(async () => {
      expect(await lastRegistered(registerRefresh)()).toBe(true);
    });
    expect(await screen.findByText(/需求单 WBDD-1 已不在当前列表/)).toBeInTheDocument();
    expect(screen.queryByText(/当前过滤：/)).toBeNull();
    await waitFor(() => expect(mocks.getProjectProcurement).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 10, source_order_id: undefined,
    }));
    await waitFor(() => expect(mocks.listProjectPartsRows).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 20, order_no: undefined, contract_no: undefined,
    }));
  });

  it("需求单重载失败时选中不漂移：PN 明细与采购段仍按原选中单过滤", async () => {
    const registerRefresh = vi.fn();
    renderTab(["XSDD-1", "XSDD-2"], registerRefresh);
    await waitFor(() => expect(orderLink("WBDD-1")).toBeTruthy());
    fireEvent.click(orderLink("WBDD-1")!);
    await waitFor(() => expect(mocks.getProjectProcurement).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 10, source_order_id: "RAW-WBDD-1",
    }));

    mocks.getBoardProjectOrders.mockRejectedValueOnce({
      response: { data: { detail: "需求单暂不可用" } },
    });
    await act(async () => {
      expect(await lastRegistered(registerRefresh)()).toBe(false);
    });
    expect(await screen.findByText("需求单暂不可用")).toBeInTheDocument();
    expect(orderLink("WBDD-1")).toBeUndefined();   // 需求单列表已清空
    // 但选中的范围是点击时拍的快照，不随列表消失而漂移成「全部」
    expect(screen.getByText(/当前过滤：WBDD-1/)).toBeInTheDocument();
    expect(mocks.getProjectProcurement).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 10, source_order_id: "RAW-WBDD-1",
    });
    expect(mocks.listProjectPartsRows).toHaveBeenLastCalledWith("p1", {
      page: 1, page_size: 20, order_no: "WBDD-1", contract_no: undefined,
    });
  });
});
