import { legacyAuthorizationState, prepareLegacyPageFixture } from "../../test/legacyAuthorization";
vi.mock("../../authz/AuthzProvider", async importOriginal => ({
  ...(await importOriginal<typeof import("../../authz/AuthzProvider")>()), useAuthz: () => legacyAuthorizationState(),
}));
function renderUrlPage(...args: Parameters<typeof fixtureRenderurlpage>) {
  prepareLegacyPageFixture({"page_profit": true}, "readonly");
  return fixtureRenderurlpage(...args);
}
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen, waitFor } from "../../test/antdRender";
import { renderUrlPage as fixtureRenderurlpage } from "../../test/urlStateRender";
import api from "../../api";
import ProfitPage from "../ProfitPage";

const adapter = api.defaults.adapter;
let requests: { url: string | undefined; params: Record<string, unknown> }[];
beforeEach(() => {
  localStorage.clear(); localStorage.setItem("role", "admin"); requests = [];
  api.defaults.adapter = async (config) => {
    requests.push({ url: config.url, params: config.params });
    return { status: 200, statusText: "OK", headers: {}, config,
      data: { rows: [{ dimension: "合成结果", revenue: 113, lines: 1 }] } };
  };
});
afterEach(() => { cleanup(); api.defaults.adapter = adapter; vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("恢复利润维度/异常/完整日期，导出同参，切换保留日期和无关键", async () => {
  const { router } = renderUrlPage(<ProfitPage />,
    "/profit?dimension=customer&only_anomaly=true&date_from=2026-02-01&date_to=2026-02-28&origin=review");
  await screen.findByText("合成结果");
  expect(requests[0]).toEqual({ url: "/profit", params: {
    dimension: "customer", only_anomaly: true, date_from: "2026-02-01", date_to: "2026-02-28",
  } });
  expect(screen.getByRole("radio", { name: "按客户" })).toBeChecked();
  expect(screen.getByRole("switch")).toBeChecked();
  expect(screen.getByDisplayValue("2026-02-01")).toBeInTheDocument();
  expect(screen.getByDisplayValue("2026-02-28")).toBeInTheDocument();
  const create = vi.fn(() => "blob:synthetic");
  vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = vi.fn(); });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
  await waitFor(() => expect(requests[1]).toEqual({ url: "/profit/export", params: requests[0].params }));
  await waitFor(() => expect(create).toHaveBeenCalled());
  fireEvent.click(screen.getByRole("radio", { name: "按销售员" }));
  fireEvent.click(screen.getByRole("switch"));
  await waitFor(() => expect(router.state.location.search).toBe("?date_from=2026-02-01&date_to=2026-02-28&origin=review"));
  expect(router.state.historyAction).toBe("REPLACE");
  fireEvent.click(screen.getByLabelText("close-circle"));
  await waitFor(() => expect(router.state.location.search).toBe("?origin=review"));
  await waitFor(() => expect(requests[requests.length - 1]?.params).toEqual({
    dimension: "salesperson", only_anomaly: false, date_from: undefined, date_to: undefined,
  }));
  await act(() => router.navigate(-1));
  expect(router.state.location.pathname).toBe("/before");
});

it.each([
  "date_from=2026-02-31&date_to=2026-03-01",
  "date_from=2026-03-02&date_to=2026-03-01",
  "date_from=2026-03-01",
  "date_to=2026-03-01",
  "date_from=2026/03/01&date_to=nope",
])("无效或不完整范围整体回退不限日期：%s", async (query) => {
  renderUrlPage(<ProfitPage />, `/profit?dimension=unknown&only_anomaly=maybe&${query}`);
  await screen.findByText("合成结果");
  expect(requests[0].params).toEqual({ dimension: "salesperson", only_anomaly: false,
    date_from: undefined, date_to: undefined });
  expect(screen.getAllByRole("textbox").map((input) => (input as HTMLInputElement).value)).toEqual(["", ""]);
});

it("受限销售 URL 不能打开隐藏维度或财务异常筛选", async () => {
  localStorage.setItem("role", "sales");
  localStorage.setItem("permissions", JSON.stringify({ data_purchase_cost: false, data_profit: false }));
  renderUrlPage(<ProfitPage />, "/profit?dimension=customer&only_anomaly=true");
  await screen.findByText("合成结果");
  expect(requests[0].params).toEqual({ dimension: "part", only_anomaly: false,
    date_from: undefined, date_to: undefined });
  expect(screen.queryByRole("radio", { name: "按客户" })).toBeNull();
  expect(screen.queryByRole("switch")).toBeNull();
});

it("受限销售 URL 带 dimension=salesperson 时回退按型号，且不显示销售员选项", async () => {
  localStorage.setItem("role", "sales");
  localStorage.setItem("permissions", JSON.stringify({ own_customers_only: true }));
  renderUrlPage(<ProfitPage />, "/profit?dimension=salesperson");
  await screen.findByText("合成结果");
  expect(requests[0].params).toEqual({ dimension: "part", only_anomaly: false,
    date_from: undefined, date_to: undefined });
  expect(screen.getByRole("radio", { name: "按型号" })).toBeChecked();
  expect(screen.queryByRole("radio", { name: "按销售员" })).toBeNull();
});

it("同页导航和后退同步日期控件与请求", async () => {
  const { router } = renderUrlPage(<ProfitPage />, "/profit?dimension=part&date_from=2024-02-29&date_to=2024-02-29");
  await screen.findByText("合成结果");
  await act(() => router.navigate("/profit?dimension=customer"));
  await waitFor(() => expect(requests[requests.length - 1]?.params.dimension).toBe("customer"));
  expect(screen.getAllByRole("textbox").map((input) => (input as HTMLInputElement).value)).toEqual(["", ""]);
  await act(() => router.navigate(-1));
  await waitFor(() => expect(requests[requests.length - 1]?.params).toEqual({ dimension: "part", only_anomaly: false,
    date_from: "2024-02-29", date_to: "2024-02-29" }));
  expect(screen.getAllByDisplayValue("2024-02-29")).toHaveLength(2);
});
