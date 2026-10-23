import { TaxBasisProvider } from "../../context/TaxBasis";
import { useLayoutEffect } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter, useNavigate, useLocation } from "react-router-dom";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "../../test/antdRender";
import { AuthzProvider, useAuthz } from "../../authz/AuthzProvider";
import { vocabulary } from "../../authz/rules";
import { authzSnapshot } from "../../test/authzFixtures";
import type { AuthzSnapshot } from "../../api/authz";
import api from "../../api";
import MasterDataPage from "../MasterDataPage";
import GovernancePage from "../GovernancePage";
import PartSearchPage from "../PartSearchPage";

const { getAuthzSnapshot } = vi.hoisted(() => ({ getAuthzSnapshot: vi.fn() }));
vi.mock("../../api/authz", async (original) => ({
  ...(await original<typeof import("../../api/authz")>()),
  getAuthzSnapshot,
}));
const locationPaints: { search: string; text: string }[] = [];
const originalAdapter = api.defaults.adapter;
let requests: { url: string; authorization: unknown; signal: any }[] = [];
const decision = (allow: boolean) => ({
  effect: allow ? ("allow" as const) : ("deny" as const),
  reason: null,
});
function snapshot(
  grants: Record<string, string[]>,
  version = "v1",
): AuthzSnapshot {
  const result = authzSnapshot(
    {
      enabled: true,
      read: false,
      export: false,
      scope: "none",
      customer_count: 0,
      reason: "NOT_GRANTED",
      field_permissions: [],
    },
    { mode: "unified_modules", version },
  );
  for (const [id, definition] of Object.entries(vocabulary)) {
    const actions = grants[id] || [];
    result.resources[id] = {
      migration_state: "implemented",
      runtime_state: "enabled",
      configurable: true,
      grant_version: actions.length ? 1 : 0,
      scope_kind: actions.length ? "all" : "none",
      field_permissions: [],
      decisions: Object.fromEntries(
        Object.keys(definition.actions).map((action) => [
          action,
          decision(actions.includes(action)),
        ]),
      ),
    };
  }
  return result;
}
function Refresh() {
  const authz = useAuthz(),
    navigate = useNavigate(),
    location = useLocation();
  useLayoutEffect(() => {
    locationPaints.push({
      search: location.search,
      text: document.body.textContent || "",
    });
  });
  return (
    <>
      <button onClick={() => void authz.refresh()}>更新权限</button>
      <button onClick={() => navigate("/parts?part_id=8")}>选择对象8</button>
      <button onClick={() => navigate("/master?q=next")}>切主数据查询</button>
    </>
  );
}
function mount(component: React.ReactNode, path: string) {
  return render(
    <AuthzProvider token="actor-A">
      <MemoryRouter initialEntries={[path]}>
        <Refresh />
        {component}
      </MemoryRouter>
    </AuthzProvider>,
  );
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function adapter(handler: (url: string) => unknown | Promise<unknown>) {
  api.defaults.adapter = async (config) => {
    requests.push({
      url: config.url || "",
      authorization: config.headers?.Authorization,
      signal: config.signal,
    });
    return {
      data: await handler(config.url || ""),
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    };
  };
}
const overview = () => ({
  part: null,
  purchases_recent: [],
  sales_recent: [],
  inventory: [{ warehouse: "授权仓库", qty: 7 }],
  substitutes: [],
  profit_summary: {
    avg_purchase_cost: null,
    avg_sale_price: null,
    avg_cost_moving: null,
    avg_cost_fifo: null,
    avg_margin_moving: null,
    avg_margin_fifo: null,
    total_qty_sold: null,
  },
  inquiry_ref: {
    min_money: null,
    max_money: null,
    last_money: null,
    count: null,
  },
  sales_velocity: null,
  stock_dynamic: null,
  sales_recent_restricted: true,
  block_access: Object.fromEntries(
    [
      "identity",
      "purchases_recent",
      "sales_recent",
      "inventory",
      "profit_summary",
      "market_prices",
      "substitutes",
      "substitute_inventory",
    ].map((key) => [key, decision(key === "inventory")]),
  ),
});
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("token", "actor-A");
  localStorage.setItem("role", "admin");
  requests = [];
  locationPaints.length = 0;
  getAuthzSnapshot.mockReset();
});
afterEach(() => {
  cleanup();
  api.defaults.adapter = originalAdapter;
});
it("master read uses own endpoint and cannot create/edit despite old admin role", async () => {
  getAuthzSnapshot.mockResolvedValue({
    data: snapshot({ part_master: ["read", "show_list"] }),
  });
  adapter((url) =>
    url.endsWith("categories")
      ? { categories: [] }
      : { items: [{ id: 7, pn_std: "MASTER-ONLY" }], total: 1 },
  );
  mount(<MasterDataPage />, "/master");
  expect(await screen.findByText("MASTER-ONLY")).toBeVisible();
  expect(requests.some((r) => r.url === "/parts/master/search")).toBe(true);
  expect(requests.some((r) => r.url === "/parts/search")).toBe(false);
  expect(screen.getByRole("button", { name: /编\s*辑/ })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "新建 PN" })).toBeNull();
  expect(
    requests.every(
      (r) =>
        r.authorization === "Bearer actor-A" && r.signal instanceof AbortSignal,
    ),
  ).toBe(true);
});
it("create surface opens without mounting list or borrowing its show action", async () => {
  getAuthzSnapshot.mockResolvedValue({
    data: snapshot({ part_master: ["read", "create", "show_create"] }),
  });
  adapter(() => ({ categories: [] }));
  mount(<MasterDataPage />, "/master");
  fireEvent.click(await screen.findByRole("button", { name: "新建 PN" }));
  await waitFor(() => expect(screen.getByRole("dialog")).toBeVisible());
  expect(requests.map((r) => r.url)).not.toContain("/parts/master/search");
  expect(screen.queryByRole("table")).toBeNull();
});
it("inventory-only part surface accepts null identity and no denied blocks", async () => {
  getAuthzSnapshot.mockResolvedValue({
    data: snapshot({ part_lookup: ["inventory_read", "show_inventory"] }),
  });
  adapter((url) => {
    if (url === "/parts/overview") return overview();
    throw Error("unexpected " + url);
  });
  mount(<PartSearchPage />, "/parts?part_id=7");
  expect(await screen.findByText("授权仓库")).toBeVisible();
  expect(screen.getByText(/型号身份详情未获授权/)).toBeVisible();
  expect(screen.queryByRole("region", { name: "型号基本信息" })).toBeNull();
  expect(screen.queryByRole("tab", { name: /采购历史/ })).toBeNull();
  expect(screen.queryByText("近期成交参考价")).toBeNull();
  expect(requests.map((r) => r.url)).toEqual(["/parts/overview"]);
});
it("missing canonical block decisions fail closed", async () => {
  getAuthzSnapshot.mockResolvedValue({
    data: snapshot({ part_lookup: ["inventory_read", "show_inventory"] }),
  });
  adapter(() => ({ ...overview(), block_access: undefined }));
  mount(<PartSearchPage />, "/parts?part_id=7");
  expect(await screen.findByText("加载失败")).toBeVisible();
  expect(screen.queryByText("授权仓库")).toBeNull();
});
it("independent identity history never requests governance summary or other queues", async () => {
  getAuthzSnapshot.mockResolvedValue({
    data: snapshot({ part_identity: ["read", "show_merges"] }),
  });
  adapter((url) => {
    if (url === "/governance/merges") return { items: [], total: 0 };
    throw Error("unexpected " + url);
  });
  mount(<GovernancePage />, "/governance");
  expect(await screen.findByRole("tab", { name: "合并历史" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0].url).toBe("/governance/merges");
  expect(screen.queryByRole("tab", { name: "型号治理" })).toBeNull();
});
it("candidate reject is independent of merge and confirm actions", async () => {
  getAuthzSnapshot.mockResolvedValue({
    data: snapshot({
      governance: ["read", "review"],
      part_identity: ["show_candidates"],
    }),
  });
  adapter((url) =>
    url === "/governance/candidates"
      ? { items: [{ id: 1, source_pn: "A", candidate_pn: "B" }], total: 1 }
      : {},
  );
  mount(<GovernancePage />, "/governance?tab=candidates");
  expect(await screen.findByRole("button", { name: /合\s*并/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: "独立型号" })).toBeDisabled();
  expect(screen.getByRole("button", { name: /否\s*决/ })).toBeEnabled();
});
it("real Provider refresh aborts pending overview and rejects its late response", async () => {
  const pending = deferred<unknown>();
  getAuthzSnapshot.mockResolvedValue({
    data: snapshot({ part_lookup: ["inventory_read", "show_inventory"] }),
  });
  adapter(() => pending.promise);
  mount(<PartSearchPage />, "/parts?part_id=7");
  await waitFor(() => expect(requests).toHaveLength(1));
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({}, "v2") });
  fireEvent.click(screen.getByText("更新权限"));
  expect(requests[0].signal.aborted).toBe(true);
  expect(await screen.findByText("没有此页面访问权限")).toBeVisible();
  await act(async () => pending.resolve(overview()));
  expect(screen.queryByText("授权仓库")).toBeNull();
  expect(requests).toHaveLength(1);
});
it("a pending create cannot show success or refetch after permissions are revoked", async () => {
  const pending = deferred<unknown>();
  getAuthzSnapshot.mockResolvedValue({
    data: snapshot({ part_master: ["read", "create", "show_create"] }),
  });
  adapter((url) =>
    url === "/parts/master"
      ? pending.promise
      : url.endsWith("categories")
        ? { categories: [] }
        : { near_duplicates: [] },
  );
  mount(<MasterDataPage />, "/master");
  fireEvent.click(await screen.findByRole("button", { name: "新建 PN" }));
  fireEvent.change(await screen.findByPlaceholderText("如 ST8000NM000A"), {
    target: { value: "NEW-PN" },
  });
  fireEvent.click(screen.getByRole("button", { name: /保\s*存/ }));
  await waitFor(() =>
    expect(requests.filter((r) => r.url === "/parts/master")).toHaveLength(1),
  );
  getAuthzSnapshot.mockResolvedValue({ data: snapshot({}, "revoked") });
  fireEvent.click(screen.getByText("更新权限"));
  expect(await screen.findByText("没有此页面访问权限")).toBeVisible();
  await act(async () => pending.resolve({ created: true, pn_std: "NEW-PN" }));
  expect(screen.queryByText("已新建 NEW-PN")).toBeNull();
  expect(requests.filter((r) => r.url === "/parts/master")).toHaveLength(1);
  expect(requests.map((r) => r.url)).not.toContain("/parts/master/search");
});

it("same actor and grant cannot paint a previous object's delayed inventory response", async () => {
  getAuthzSnapshot.mockResolvedValue({
    data: snapshot({ part_lookup: ["inventory_read", "show_inventory"] }),
  });
  const old = deferred<ReturnType<typeof overview>>();
  let calls = 0;
  adapter(() =>
    ++calls === 1
      ? old.promise
      : { ...overview(), inventory: [{ warehouse: "对象8仓库", qty: 8 }] },
  );
  mount(<PartSearchPage />, "/parts?part_id=7");
  await waitFor(() => expect(requests).toHaveLength(1));
  fireEvent.click(screen.getByText("选择对象8"));
  expect(await screen.findByText("对象8仓库")).toBeVisible();
  await act(async () =>
    old.resolve({
      ...overview(),
      inventory: [{ warehouse: "对象7迟到仓库", qty: 7 }],
    }),
  );
  expect(screen.queryByText("对象7迟到仓库")).toBeNull();
  expect(screen.getByText("对象8仓库")).toBeVisible();
  expect(requests).toHaveLength(2);
});
it("part cost fields are never borrowed from another resource with the same field name", async () => {
  const value = snapshot({
    part_lookup: ["financial_summary_read", "show_quick_pricing"],
    company_summary: ["read"],
  });
  value.resources.company_summary.field_permissions = ["data_purchase_cost"];
  getAuthzSnapshot.mockResolvedValue({ data: value });
  const data = overview();
  data.block_access.inventory = decision(false);
  data.block_access.profit_summary = decision(true);
  adapter(() => ({
    ...data,
    profit_summary: {
      ...data.profit_summary,
      avg_purchase_cost: 7654321,
      avg_sale_price: 40,
      total_qty_sold: 2,
    },
  }));
  mount(<PartSearchPage />, "/parts?part_id=7");
  expect(await screen.findByText("平均销售价")).toBeVisible();
  expect(screen.queryByText(/7,654,321|7654321/)).toBeNull();
  expect(screen.queryByText("平均采购价")).toBeNull();
  expect(requests.map((r) => r.url)).toEqual(["/parts/overview"]);
});

it("the first committed frame after an object change hides previous data and aborts its refresh", async () => {
  getAuthzSnapshot.mockResolvedValue({
    data: snapshot({ part_lookup: ["inventory_read", "show_inventory"] }),
  });
  const pending = deferred<ReturnType<typeof overview>>();
  let calls = 0;
  adapter(() =>
    ++calls === 1
      ? { ...overview(), inventory: [{ warehouse: "旧对象仓库", qty: 7 }] }
      : pending.promise,
  );
  mount(<PartSearchPage />, "/parts?part_id=7");
  await screen.findByText("旧对象仓库");
  fireEvent.click(screen.getByText("选择对象8"));
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(
    locationPaints
      .filter((x) => x.search === "?part_id=8")
      .some((x) => x.text.includes("旧对象仓库")),
  ).toBe(false);
  expect(requests[0].signal.aborted).toBe(true);
  await act(async () =>
    pending.resolve({
      ...overview(),
      inventory: [{ warehouse: "新对象仓库", qty: 8 }],
    }),
  );
  expect(await screen.findByText("新对象仓库")).toBeVisible();
});
it("a market-only grant cannot acquire the financial block from response permissions", async () => {
  getAuthzSnapshot.mockResolvedValue({
    data: snapshot({
      part_lookup: ["market_prices_read", "show_quick_pricing"],
    }),
  });
  const data = overview();
  data.block_access.inventory = decision(false);
  data.block_access.profit_summary = decision(true);
  data.block_access.market_prices = decision(true);
  adapter(() => ({
    ...data,
    profit_summary: { ...data.profit_summary, avg_sale_price: 7654321 },
  }));
  mount(<PartSearchPage />, "/parts?part_id=7");
  expect(await screen.findByText("询价区间")).toBeVisible();
  expect(screen.queryByText("平均销售价")).toBeNull();
});

it("master search changes hide previous rows in the first frame and abort the old query", async () => {
  getAuthzSnapshot.mockResolvedValue({
    data: snapshot({ part_master: ["read", "show_list"] }),
  });
  const pending = deferred<{
    items: { id: number; pn_std: string }[];
    total: number;
  }>();
  let calls = 0;
  adapter((url) =>
    url.endsWith("categories")
      ? { categories: [] }
      : ++calls === 1
        ? { items: [{ id: 7, pn_std: "OLD-MASTER" }], total: 1 }
        : pending.promise,
  );
  mount(<MasterDataPage />, "/master?q=old");
  await screen.findByText("OLD-MASTER");
  fireEvent.click(screen.getByText("切主数据查询"));
  await waitFor(() => expect(calls).toBe(2));
  expect(
    locationPaints
      .filter((x) => x.search === "?q=next")
      .some((x) => x.text.includes("OLD-MASTER")),
  ).toBe(false);
  expect(
    requests.find((x) => x.url === "/parts/master/search")?.signal.aborted,
  ).toBe(true);
  await act(async () =>
    pending.resolve({ items: [{ id: 8, pn_std: "NEW-MASTER" }], total: 1 }),
  );
  expect(await screen.findByText("NEW-MASTER")).toBeVisible();
});

it.each([false, true])(
  "inventory quantities remain usable without shared tax permission (cost field=%s)",
  async (cost) => {
    const value = snapshot({
      part_lookup: ["inventory_read", "show_inventory"],
    });
    if (cost)
      value.resources.part_lookup.field_permissions = ["data_purchase_cost"];
    getAuthzSnapshot.mockResolvedValue({ data: value });
    adapter((url) => {
      if (url !== "/parts/overview")
        throw Error("unexpected shared setting request");
      return {
        ...overview(),
        inventory: [
          {
            warehouse: "数量仍可读",
            display_qty: 7,
            source_qty: 7,
            unit_cost: 9876543,
            inventory_value: 9876543,
          },
        ],
      };
    });
    mount(
      <TaxBasisProvider>
        <PartSearchPage />
      </TaxBasisProvider>,
      "/parts?part_id=7",
    );
    expect(await screen.findByText("数量仍可读")).toBeVisible();
    expect(screen.getByRole("row", {name: /数量仍可读 7 7/})).toBeVisible();
    expect(screen.queryByText(/单位成本\(|库存金额\(|9,876,543/)).toBeNull();
    if (cost)
      expect(screen.getByText("缺少金额展示设置的读取权限")).toBeVisible();
    else expect(screen.queryByText("缺少金额展示设置的读取权限")).toBeNull();
    expect(requests.map((r) => r.url)).toEqual(["/parts/overview"]);
  },
);
