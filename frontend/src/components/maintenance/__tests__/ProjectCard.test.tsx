import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import ProjectCard from "../ProjectCard";
import type { BoardProjectRow, Stat } from "../../../api/maintenanceBossBoard";

afterEach(cleanup);

const ready = <T,>(value: T): Stat<T> => ({ state: "ready", value, as_of: null });
const restricted = (): Stat<string | number> => ({
  state: "restricted", value: null, as_of: null,
});
const notImported = (): Stat<string | number> => ({
  state: "not_imported", value: null, as_of: null,
});

function makeRow(overrides: Partial<BoardProjectRow> = {}): BoardProjectRow {
  return {
    project_id: "p1",
    project_code: "合成项目A",
    display_name: "合成项目A",
    lifecycle: "ongoing",
    is_archived: false,
    contract_nos: ["XSDD-20251230-0027"],
    project_manager: "李经理",
    salesperson: "王销售",
    contract_amount_inc_tax: ready("100000.00"),
    known_apply_cost_ex_tax: ready("70000.00"),
    procured_qty: ready("12.000"),
    expense_cost_inc_tax: ready("113.00"),
    requisition_cost_inc_tax: ready("260.00"),
    collection_preview_inc_tax: ready("30000.00"),
    cost_ratio_pct: ready("79.1"),
    card_status: "normal",
    has_activity_in_window: true,
    pre_delivery_order_count: 0,
    orders_ytd: ready(3),
    lines_ytd: ready(9),
    known_apply_cost_inc_tax: ready({
      actual_amount: "79100.00", estimated_amount: "0",
      known_amount: "79100.00", missing_lines: 2,
      coverage_pct: 88.7, quality: "contains_estimate",
    }),
    shipped_qty: notImported(),
    returned_good_qty: notImported(),
    returned_bad_qty: notImported(),
    ...overrides,
  } as BoardProjectRow;
}

function renderCard(row: BoardProjectRow) {
  return render(
    <MemoryRouter>
      <ProjectCard row={row} />
    </MemoryRouter>,
  );
}

describe("项目卡（#34/#35/#43）", () => {
  it("展示标题、XSDD 合同号、销售与合同总额（2026-08-21：卡片不再显示项目经理）", () => {
    renderCard(makeRow());
    expect(screen.getByText("合成项目A")).toBeInTheDocument();
    expect(screen.getByText("XSDD-20251230-0027")).toBeInTheDocument();
    expect(screen.getByText(/王销售/)).toBeInTheDocument();
    expect(screen.queryByText(/项目经理/)).not.toBeInTheDocument();
    expect(screen.getByText(/100000\.00 元/)).toBeInTheDocument();
  });

  it("成本彩色上卡：备件/报销/领用/发货数 + 回款进度条（2026-08-22）", () => {
    const { container } = renderCard(makeRow());
    const text = container.textContent ?? "";
    expect(text).toContain("备件成本");          // 蓝色备件成本
    expect(text).toContain("79100.00 元");
    expect(text).toContain("2 行无参照价");
    expect(text).toContain("报销成本 113.00 元"); // 橙色
    expect(text).toContain("已领用成本 260.00 元"); // 紫色
    expect(text).toContain("维保备件发货数 12 个"); // 青色（千分位+个，不显示 .000）
    expect(text).not.toContain("12.000");
    // 回款进度条：已回款 / 合同额（千分位）+ 百分比
    expect(text).toContain("¥30,000 / ¥100,000（30%）");
    // 成本率条 + 回款条 = 2 条进度条
    expect(container.querySelectorAll(".ant-progress").length).toBe(2);
  });

  it("回款受限/无值时不画进度条也不造 0（铁律 5）", () => {
    const { container } = renderCard(makeRow({
      collection_preview_inc_tax: restricted(),
      expense_cost_inc_tax: restricted(),
    }));
    const text = container.textContent ?? "";
    expect(text).toContain("回款：无权限");
    // 只剩成本率一条进度条（回款条不画）
    expect(container.querySelectorAll(".ant-progress").length).toBe(1);
    expect(text).toContain("报销成本 无权限");
  });

  it("三态标签随成本率变化", () => {
    renderCard(makeRow({ card_status: "warning" }));
    expect(screen.getByText("提醒")).toBeInTheDocument();
    cleanup();
    renderCard(makeRow({ card_status: "alert" }));
    expect(screen.getByText("报警")).toBeInTheDocument();
  });

  it("成本率算不出来时不画进度条，也不显示 0", () => {
    const { container } = renderCard(makeRow({
      card_status: null,
      cost_ratio_pct: { state: "ready", value: null, as_of: null },
      contract_amount_inc_tax: ready(null as unknown as string),
    }));
    expect(screen.getByTestId("ratio-unknown")).toHaveTextContent("数据不足");
    expect(container.querySelector(".ant-progress")).toBeNull();
  });

  it("无金额权限时显示「无权限」，绝不显示 0（铁律 5）", () => {
    const { container } = renderCard(makeRow({
      card_status: null,
      contract_amount_inc_tax: restricted(),
      known_apply_cost_ex_tax: restricted(),
      collection_preview_inc_tax: restricted(),
      cost_ratio_pct: restricted(),
      known_apply_cost_inc_tax: {
        state: "restricted", value: null, as_of: null,
      } as BoardProjectRow["known_apply_cost_inc_tax"],
    }));
    const text = container.textContent ?? "";
    expect(text).toContain("无权限");
    expect(text).not.toMatch(/(^|[^\d])0([^\d.]|$)/);
  });

  it("三源事实未导入时显示「尚未导入」而不是 0", () => {
    const { container } = renderCard(makeRow({ procured_qty: notImported() }));
    expect(container.textContent).toContain("尚未导入");
  });

  it("归档与预交付徽标按需出现", () => {
    renderCard(makeRow({ is_archived: true, pre_delivery_order_count: 3 }));
    expect(screen.getByText("已归档")).toBeInTheDocument();
    expect(screen.getByText("预交付 3 单")).toBeInTheDocument();
  });

  it("「进入面板」链到项目面板；未归属桶没有这个按钮", () => {
    renderCard(makeRow());
    expect(screen.getByRole("link", { name: /进入面板/ }))
      .toHaveAttribute("href", "/maintenance/projects/p1");
    cleanup();
    renderCard(makeRow({ project_id: "unassigned" }));
    expect(screen.queryByRole("link", { name: /进入面板/ })).toBeNull();
    expect(screen.getByText(/需在项目面板确认挂靠/)).toBeInTheDocument();
  });
});
