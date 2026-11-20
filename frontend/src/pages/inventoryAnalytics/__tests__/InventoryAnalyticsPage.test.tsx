import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import { businessToday } from "../../../utils/date";
import type { Cube, WarehouseResults } from "../../../api/warehouseAnalytics";
import { warehouseParams } from "../../../api/warehouseAnalytics";
import Page from "../InventoryAnalyticsPage";
import { boards, operatorRole, parseDrill, parseFilters } from "../warehouseState";
import { reach } from "../Trend";

const request = vi.hoisted(() => vi.fn());
vi.mock("../../../api/warehouseAnalytics", async (original) => ({
  ...(await original()),
  fetchWarehouse: request,
}));

const coverage: WarehouseResults["coverage"] = {
  sources: [
    {
      source: "inbound",
      available: true,
      min_date: "2024-01-01",
      max_date: "2026-11-30",
      documents: 8,
      lines: 12,
    },
    {
      source: "return",
      available: true,
      min_date: "2024-06-01",
      max_date: "2026-11-30",
      documents: 5,
      lines: 10,
    },
    {
      source: "direct",
      available: true,
      min_date: "2024-06-01",
      max_date: "2026-11-30",
      documents: 2,
      lines: 3,
    },
    {
      source: "outbound",
      available: false,
      min_date: null,
      max_date: null,
      documents: null,
      lines: null,
    },
  ],
  options: { sources: ["inbound", "return", "direct"], centers: ["示例仓"], units: ["件"] },
};
const sample = { pn: "TEST-PN", description: "测试用内存条", quantity: 4, unit: "件" };
const record = {
  id: "12",
  number: "TEST-DOC",
  source: "inbound",
  date: "2026-11-03",
  kind: "采购入库",
  center: null,
  operator: "测试人员",
  sample: [sample],
  matched_lines: 1,
  total_lines: 2,
};
function axis(key: string | null, label: string, value: number) {
  return { key, label, value };
}
const cube: Cube = {
  rows: [axis(null, "未填", 1)],
  columns: [axis("采购入库", "采购入库", 1)],
  cells: [{ row_key: null, col_key: "采购入库", value: 1 }],
  total: 1,
  total_rows: 1,
  total_columns: 1,
};
const period = "period=custom&from=2026-11-01&to=2026-11-09";
function Location() {
  return <output data-testid="location">{useLocation().search}</output>;
}
function mount(query: string) {
  render(
    <MemoryRouter initialEntries={["/inventory/analytics?" + query]}>
      <Page />
      <Location />
    </MemoryRouter>,
  );
}
function query() {
  return new URLSearchParams(screen.getByTestId("location").textContent || "");
}
function reads(endpoint: string) {
  return request.mock.calls
    .filter((r) => r[0] === endpoint)
    .map((r) => Object.fromEntries(r[1] as URLSearchParams));
}
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("token", "warehouse-test");
  request.mockReset();
  request.mockImplementation(async (endpoint: string, params: URLSearchParams) => {
    if (endpoint === "coverage") return coverage;
    if (endpoint === "cube") {
      if (params.get("col") === "source") {
        const key = params.get("row") === "week" ? "2026-11-02" : "2026-11-03";
        return {
          ...cube,
          rows: [axis(key, key, 1)],
          columns: [axis("inbound", "入库单", 1)],
          cells: [{ row_key: key, col_key: "inbound", value: 1 }],
        };
      }
      if (params.get("row") === "week")
        return {
          ...cube,
          rows: [axis("2026-11-02", "2026-11-02", 2)],
          columns: [axis(null, "未填", 2)],
          cells: [{ row_key: "2026-11-02", col_key: null, value: 2 }],
        };
      return cube;
    }
    if (endpoint === "rankings") {
      const people = params.get("by") === "operator";
      return {
        items: [
          {
            key: people ? "测试人员" : sample.pn,
            label: people ? "测试人员" : sample.pn,
            description: people ? null : sample.description,
            documents: 1,
            lines: 2,
            pns: 1,
            quantity: null,
            rank: 1,
            active_days: 1,
          },
          ...(people
            ? [
                {
                  key: null,
                  label: "未归属",
                  rank: null,
                  documents: 2,
                  lines: 3,
                  pns: 1,
                  quantity: null,
                  active_days: 1,
                },
              ]
            : []),
        ],
        total: 1,
        repeat_pns: 0,
        top10_line_share: 1,
      };
    }
    if (endpoint === "documents") return { items: [record], total: 1 };
    if (endpoint === "lines") return { items: [sample], total: 1 };
    if (endpoint === "equipment")
      return {
        items: [
          {
            whole_pn: "M01",
            whole_description: "整机示例",
            whole_unit: "台",
            documents: 1,
            whole_qty: 1,
            matched_lines: 2,
            last_date: "2026-11-03",
          },
        ],
        total: 1,
      };
    if (endpoint === "testing")
      return {
        items: [
          {
            key: null,
            label: "未署名",
            required: 2,
            completed: 1,
            pending: 1,
            unknown: 0,
            attributed: 0,
          },
        ],
        required: 2,
        completed: 1,
        pending: 1,
        unknown: 0,
        attributed: 0,
      };
    throw new Error("unexpected endpoint " + endpoint);
  });
});
afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.useRealTimers();
});

describe("库存管理分析", () => {
  it("默认跟随上海业务日含今天近三十日，序列化保留空分组，非法下钻不会扩大范围", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-19T16:02:00Z"));
    expect(parseFilters(new URLSearchParams()).filters).toMatchObject({
      date_from: "2026-10-22",
      date_to: "2026-11-20",
    });
    expect(operatorRole("direct", "pick")).toBe("direct");
    expect(operatorRole("return", "direct")).toBe("pick");
    expect(parseDrill('{"bad":"group"}')).toBeNull();
    expect(parseDrill('{"require_lines":"yes"}')).toBeNull();
    expect(parseDrill('{"scope":{"untrusted":"x"}}')).toBeNull();
    expect(
      warehouseParams({ scope: { kind: "采购入库", center: null }, operator_role: "pick" }).get("scope"),
    ).toBe('{"center":null,"kind":"采购入库"}');
    expect(reach(coverage.sources[0], "2023-12-25", "2024-01-02")).toBe("partial");
    expect(reach(coverage.sources[0], "2023-12-25", "2023-12-30")).toBe("outside");
    expect(reach(coverage.sources[3])).toBe("absent");
  });

  it("总览并列头部排行，默认范围传后端，首次加载不查询单据或其他看板", async () => {
    mount("");
    await screen.findByText("测试用内存条");
    expect(reads("cube")).toContainEqual(
      expect.objectContaining({
        row: "source",
        date_from: businessToday().subtract(29, "day").format("YYYY-MM-DD"),
        date_to: businessToday().format("YYYY-MM-DD"),
      }),
    );
    expect(screen.getByRole("heading", { name: "拣货人员前五" })).toBeInTheDocument();
    expect(reads("rankings")).toHaveLength(2);
    expect(reads("rankings")).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ by: "operator", operator_role: "pick", page_size: "5" }),
        expect.objectContaining({
          by: "pn",
          scope: '{"kind":"销售退货","source":"return"}',
          page_size: "5",
        }),
      ]),
    );
    ["testing", "equipment", "documents", "lines"].forEach((endpoint) =>
      expect(reads(endpoint)).toHaveLength(0),
    );
  });

  it("矩阵双轴保留空仓储中心，继续查看原单再返回时恢复筛选与缓存", async () => {
    mount("view=matrix&" + period + "&unit=件");
    fireEvent.click(await screen.findByRole("button", { name: "未填 × 采购入库" }));
    const doc = await screen.findByRole("button", { name: "TEST-DOC" });
    const filters = {
      date_from: "2026-11-01",
      date_to: "2026-11-09",
      unit: "件",
      scope: '{"center":null,"kind":"采购入库"}',
    };
    expect(reads("documents")).toContainEqual(expect.objectContaining(filters));
    fireEvent.focus(doc);
    expect(await screen.findByText(/测试用内存条/)).toBeInTheDocument();
    fireEvent.click(doc);
    expect(await screen.findByRole("columnheader", { name: "PN / 描述" })).toBeInTheDocument();
    expect(reads("lines")).toContainEqual(expect.objectContaining(filters));
    expect(request.mock.calls.find((r) => r[0] === "lines")?.[2]).toBe("12");
    fireEvent.click(screen.getByRole("button", { name: "返回上一级" }));
    await screen.findByRole("button", { name: "TEST-DOC" });
    fireEvent.click(screen.getByRole("button", { name: "返回上一级" }));
    await screen.findByRole("button", { name: "未填 × 采购入库" });
    expect(query().get("unit")).toBe("件");
    expect(query().has("drill")).toBe(false);
    expect(reads("cube")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "刷新数据" }));
    await waitFor(() => expect(reads("cube")).toHaveLength(2));
  });

  it("矩阵明细维度的全集、单头边际下钻均剔除无子行单据", async () => {
    mount("view=matrix&rows=pn&cols=kind&" + period);
    fireEvent.click(await screen.findByRole("button", { name: "全集单据" }));
    await screen.findByRole("button", { name: "TEST-DOC" });
    expect(reads("documents")[0]).toMatchObject({ require_lines: "true" });
    expect(reads("documents")[0].scope).toBeUndefined();
    fireEvent.click(screen.getByRole("button", { name: "返回上一级" }));
    fireEvent.click(await screen.findByRole("button", { name: "采购入库 · 全行" }));
    await screen.findByRole("button", { name: "TEST-DOC" });
    expect(reads("documents").slice(-1)[0]).toMatchObject({
      require_lines: "true",
      scope: '{"kind":"采购入库"}',
    });
  });

  it("切换岗位和计量单位时人员榜与个人趋势同步，下钻保留同岗身份", async () => {
    mount("view=people&" + period);
    await screen.findByRole("button", { name: "测试人员 · 匹配单据" });
    expect(await screen.findByRole("heading", { name: "测试人员 · 个人作业节奏" })).toBeInTheDocument();
    expect(reads("rankings")).toContainEqual(
      expect.objectContaining({ by: "operator", metric: "documents", operator_role: "pick" }),
    );
    expect(screen.queryByRole("option", { name: "数量（件）" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("岗位范围"), { target: { value: "direct" } });
    fireEvent.change(screen.getByLabelText("明细单位"), { target: { value: "件" } });
    fireEvent.change(screen.getByLabelText("排行指标"), { target: { value: "quantity" } });
    await waitFor(() =>
      expect(reads("rankings")).toContainEqual(
        expect.objectContaining({ metric: "quantity", operator_role: "direct", unit: "件" }),
      ),
    );
    fireEvent.click(await screen.findByRole("button", { name: "测试人员 · 匹配单据" }));
    await screen.findByRole("button", { name: "TEST-DOC" });
    expect(reads("documents")).toContainEqual(
      expect.objectContaining({ operator_role: "direct", unit: "件", scope: '{"operator":"测试人员"}' }),
    );
  });

  it("退货单型号分布只取销售退货，按空中心和周一交集下钻", async () => {
    mount("view=returns&" + period);
    expect(await screen.findByText("测试用内存条")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "未填 × 2026-11-02" }));
    await screen.findByRole("button", { name: "TEST-DOC" });
    expect(JSON.parse(reads("documents")[0].scope)).toEqual({
      kind: "销售退货",
      source: "return",
      pn: sample.pn,
      center: null,
      week: "2026-11-02",
    });
  });

  it("单型号周轴对齐日期尾部八周，保留有覆盖零值与缺覆盖日期", async () => {
    mount("view=returns&period=custom&from=2026-03-01&to=2026-12-09");
    await screen.findByRole("columnheader", { name: "2026-12-07" });
    expect(screen.queryByRole("columnheader", { name: "2026-10-12" })).not.toBeInTheDocument();
    expect(reads("cube")).toContainEqual(
      expect.objectContaining({
        row: "week",
        date_from: "2026-10-19",
        date_to: "2026-12-09",
        require_lines: "true",
      }),
    );
    const totals = screen.getByRole("rowheader", { name: "全部中心" }).closest("tr")!;
    expect(totals.children[1]).toHaveTextContent("0");
    expect(totals.children[8]).toHaveTextContent("—");
    expect(screen.getByText("部分日期覆盖")).toBeInTheDocument();
  });

  it("聚焦图点读取一张匹配单据，键盘下钻包含坐标日期和来源", async () => {
    mount(period + "&source=inbound");
    const point = await screen.findByRole("button", { name: "2026-11-03 · 入库单 · 单据数 1" });
    expect(reads("documents")).toHaveLength(0);
    fireEvent.focus(point);
    await screen.findByText("单据示例 · TEST-DOC");
    expect(screen.getByText(/TEST-PN · 测试用内存条 · 4 件/)).toBeInTheDocument();
    expect(reads("documents")[0]).toMatchObject({
      page_size: "1",
      scope: '{"day":"2026-11-03","source":"inbound"}',
    });
    fireEvent.keyDown(point, { key: "Enter" });
    await screen.findByRole("button", { name: "TEST-DOC" });
    expect(reads("documents").slice(-1)[0]).toMatchObject({
      page_size: "20",
      scope: '{"day":"2026-11-03","source":"inbound"}',
    });
  });

  it("从日柱图切换到周折线时可继续聚焦周坐标，并按周一查看原单", async () => {
    mount(period + "&source=inbound");
    await screen.findByRole("button", { name: "2026-11-03 · 入库单 · 单据数 1" });
    fireEvent.change(screen.getByLabelText("时间粒度"), { target: { value: "week" } });
    fireEvent.change(screen.getByLabelText("图形"), { target: { value: "line" } });
    const point = await screen.findByRole("button", { name: "2026-11-02 起的一周 · 入库单 · 单据数 1" });
    expect(point).toHaveClass("wa-dot");
    fireEvent.focus(point);
    await screen.findByText("单据示例 · TEST-DOC");
    expect(reads("documents")[0]).toMatchObject({ scope: '{"source":"inbound","week":"2026-11-02"}' });
  });

  it("来源覆盖失败后结束加载，重试成功才加载原看板", async () => {
    const normalRead = request.getMockImplementation()!;
    let release: (data: typeof coverage) => void = () => {};
    const retried = new Promise<typeof coverage>((resolve) => { release = resolve; });
    let attempts = 0;
    request.mockImplementation((endpoint, params) => {
      if (endpoint !== "coverage") return normalRead(endpoint, params);
      attempts += 1;
      return attempts === 1 ? Promise.reject(new Error("offline")) : retried;
    });
    mount(period);
    const retry = await screen.findByRole("button", { name: "重试" });
    expect(screen.queryByText(/正在核对|正在读取/)).not.toBeInTheDocument();
    expect(reads("cube")).toHaveLength(0);
    fireEvent.click(retry);
    expect(await screen.findByText("正在核对来源覆盖…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
    expect(reads("cube")).toHaveLength(0);
    release(coverage);
    await screen.findByRole("button", { name: /2026-11-03.*入库单/ });
    expect(reads("coverage")).toHaveLength(2);
    expect(screen.queryByText("正在核对来源覆盖…")).not.toBeInTheDocument();
  });

  it("全仓尚无导入来源时显示空态，切板不查询业务量", async () => {
    const normalRead = request.getMockImplementation()!;
    const emptyCoverage = {
      ...coverage,
      sources: coverage.sources.map((source) => ({
        ...source, available: false, min_date: null, max_date: null, documents: null, lines: null,
      })),
    };
    request.mockImplementation((endpoint, params) =>
      endpoint === "coverage" ? Promise.resolve(emptyCoverage) : normalRead(endpoint, params),
    );
    mount(period);
    expect(await screen.findByText("尚无已导入的仓单")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "人员工作量" }));
    expect(screen.getByText("尚无已导入的仓单")).toBeInTheDocument();
    expect(request.mock.calls.map(([endpoint]) => endpoint)).toEqual(["coverage"]);
    expect(screen.queryByText(/正在核对|正在读取/)).not.toBeInTheDocument();
  });

  it.each(Object.keys(boards))("缺实际出库来源时 %s 不把未知量渲染为零", async (view) => {
    mount("view=" + view + "&source=outbound");
    await screen.findByText("该来源尚无已导入单据，当前无法计算业务量。");
    expect(request.mock.calls.map((c) => c[0])).toEqual(["coverage"]);
  });

  it("分享的单内货品可逐层返回所在看板及列表页", async () => {
    const url = new URLSearchParams({
      view: "matrix",
      drill: '{"scope":{"center":null}}',
      doc: "12",
      listPage: "2",
    });
    mount(url.toString());
    await screen.findByRole("columnheader", { name: "PN / 描述" });
    fireEvent.click(screen.getByRole("button", { name: "返回上一级" }));
    await screen.findByRole("button", { name: "TEST-DOC" });
    expect(reads("documents")[0]).toMatchObject({ page: "2", scope: '{"center":null}' });
    fireEvent.click(screen.getByRole("button", { name: "返回上一级" }));
    await screen.findByRole("heading", { name: "多维交叉矩阵" });
    expect(query().has("drill")).toBe(false);
  });
  it("已登录会话切换时撤下所有旧结果，不再访问原单", async () => {
    mount("view=equipment");
    await screen.findByText("整机示例");
    localStorage.setItem("token", "changed");
    fireEvent(window, new StorageEvent("storage", { key: "token" }));
    expect(await screen.findByText(/登录会话已变更/)).toBeInTheDocument();
    expect(screen.queryByText("整机示例")).not.toBeInTheDocument();
    expect(reads("documents")).toHaveLength(0);
  });
  it("整机按表头 PN 与单位下钻；测试署名未填保留精确空值", async () => {
    mount("view=equipment&" + period);
    fireEvent.click(await screen.findByRole("button", { name: "M01 · 关联单据" }));
    await screen.findByRole("button", { name: "TEST-DOC" });
    expect(JSON.parse(reads("documents")[0].scope)).toEqual({ whole_pn: "M01", whole_unit: "台" });
    fireEvent.click(screen.getByRole("button", { name: "返回上一级" }));
    fireEvent.click(await screen.findByRole("button", { name: "测试与覆盖" }));
    fireEvent.change(screen.getByLabelText("测试分组"), { target: { value: "tester" } });
    fireEvent.click(await screen.findByRole("button", { name: "未署名 · 完成状态未填" }));
    await screen.findByRole("button", { name: "TEST-DOC" });
    expect(reads("documents").slice(-1)[0]).toMatchObject({
      scope: '{"tester":null}',
      testing: "unknown",
    });
  });
});
