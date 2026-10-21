// 订单明细表定向回归（A-AC 返工）：
// 1) 全权限 + 三金额口径下 14 个可见数据列全部双向可排（desc → asc，含客户与
//    对账块四列）——任何一列被漏出 sortable 链路这里直接红；
// 2) 窄屏布局：横向滚动由 Table 自身 scroll.x 容器（.ant-table-content）承担，
//    外层容器 min-width:0 / max-width:100% 可收缩，客户列固定宽 + ellipsis。
// 只测受控排序回调与 DOM 结构；真实 320/390/1366 视口滚动行为由独立浏览器复验。
import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "../../../../test/antdRender";
import { afterEach, describe, expect, it } from "vitest";
import DrillTable from "../DrillTable";
import type {
  SalesAnalyticsOrderRow,
  SalesAnalyticsOrdersResp,
  SalesAnalyticsSortKey,
} from "../../../../api/salesAnalytics";
import type { SortOrder } from "../../../../api/salesAnalytics";

const ROW: SalesAnalyticsOrderRow = {
  order_id: 201, order_no: "XS-2026-201", order_date: "2026-10-16", is_future: false,
  salesperson: "王五", customer: "客户甲", business_type: "备件销售", customer_type: "经销商",
  data_status: "ok",
  amount_ex_tax: 200, amount_inc_tax: 226, revenue: 150,
  gross_profit: 40, gross_margin: 0.2,
  reconciliation: {
    has_data: true,
    reconciled_as_of: "2026-10-15T08:00:00+00:00",
    order_amount_at_snapshot: 226, reconciled_amount: 100, pending_amount: 126,
    return_reconciled_amount: 10, reconciled_display: 100,
  },
};

const BASE_RESP: SalesAnalyticsOrdersResp = {
  total: 1, page: 1, page_size: 50, as_of: "2026-10-17",
  sort: "reconciled_as_of", order: "desc", include_returns: false,
  items: [ROW],
};

/** 受控排序壳：onSortChange 回灌 sort/order（页面同款受控用法），否则第二次点击拿不到 ascend。 */
const calls: [SalesAnalyticsSortKey, SortOrder][] = [];
function Harness() {
  // 初始受控序取「列表最后一列」：rc-table 的循环是 null→descend→ascend→(cancel)，
  // 当前受控列的点击路径与未排序列不同——让循环里每个被测列起始都不是受控列，
  // 统一断言首击 desc、次击 asc
  const [sort, setSort] = useState<SalesAnalyticsSortKey>("reconciled_as_of");
  const [order, setOrder] = useState<SortOrder>("desc");
  return (
    <DrillTable
      data={{ ...BASE_RESP, sort, order }}
      loading={false}
      amounts={["ex", "inc", "revenue"]}
      sort={sort}
      order={order}
      includeReturns={false}
      costRestricted={false}
      reconRestricted={false}
      page={1}
      pageSize={50}
      onSortChange={(k, o) => { calls.push([k, o]); setSort(k); setOrder(o); }}
      onPageChange={() => {}}
      onOpenOrder={() => {}}
    />
  );
}

afterEach(() => {
  cleanup();
  calls.length = 0;
});

describe("订单明细表：全权限 14 个可见数据列双向排序（A-AC07/A-AC 返工）", () => {
  // [列头可访问名, 期望排序键]——与 DrillTable columns 一一对应
  const SORTABLE_COLUMNS: [string, SalesAnalyticsSortKey][] = [
    ["订单日期", "order_date"],
    ["订单号", "order_no"],
    ["业务类型", "business_type"],
    ["销售人员", "salesperson"],
    ["客户", "customer"],
    ["客户类型", "customer_type"],
    ["不含税金额", "amount_ex_tax"],
    ["含税订单金额", "amount_inc_tax"],
    ["营收", "revenue"],
    ["毛利", "gross_profit"],
    ["已对账金额", "reconciled_display"],
    ["待对账金额", "pending_amount"],
    ["退货已对账金额", "return_reconciled_amount"],
    ["数据截至时间", "reconciled_as_of"],
  ];

  it("每列首次点击降序、再次点击升序（sortDirections 无 cancel，第三击回降序由后端兜底）", () => {
    render(<Harness />);
    expect(screen.getByRole("row", { name: /XS-2026-201/ })).toBeInTheDocument();
    for (const [title, key] of SORTABLE_COLUMNS) {
      calls.length = 0;
      fireEvent.click(screen.getByRole("columnheader", { name: title }));
      expect(calls, `${title} 首击应发 ${key} desc`).toEqual([[key, "desc"]]);
      fireEvent.click(screen.getByRole("columnheader", { name: title }));
      expect(calls, `${title} 次击应发 ${key} asc`).toEqual([[key, "desc"], [key, "asc"]]);
    }
  });
});

describe("订单明细表：窄屏滚动几何（A-AC 返工，表格自身承担横向滚动）", () => {
  it("外层 min-width:0 / max-width:100%；.ant-table-content 承担横向滚动；table fixed 布局；客户列固定宽 + ellipsis", () => {
    const { container } = render(<Harness />);
    // 外层容器：可收缩、不越界（不带 overflow——滚动交给表格自身）。
    // render 外层还有 ant-app 包裹，从 table 反查 DrillTable 的收缩容器
    const table = screen.getByRole("table");
    const wrapper = table.closest('div[style*="min-width: 0"]') as HTMLElement | null;
    expect(wrapper).not.toBeNull();
    expect(wrapper!.getAttribute("style")).toContain("max-width: 100%");
    expect(wrapper!.getAttribute("style") ?? "").not.toContain("overflow");
    // scroll.x 生效标志：内容滚动容器存在，表格 fixed 布局（列宽不被内容挤压）
    const content = wrapper!.querySelector(".ant-table-content");
    expect(content).not.toBeNull();
    expect(getComputedStyle(table).tableLayout).toBe("fixed");
    // 客户列：固定 160 宽（colgroup 唯一 160 列）+ 单元格省略
    const widths = Array.from(container.querySelectorAll("colgroup col"))
      .map((c) => c.getAttribute("style") ?? "");
    expect(widths.some((w) => w.includes("width: 160px"))).toBe(true);
    const customerTh = screen.getByRole("columnheader", { name: "客户" });
    expect(customerTh.className).toContain("ant-table-cell-ellipsis");
  });
});
