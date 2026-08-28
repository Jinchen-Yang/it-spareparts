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
    expect(screen.getByText(/合同总额（含税）/)).toBeInTheDocument();
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

  it("合同额 partial 只展示已知小计，不作为回款率分母", () => {
    const { container } = renderCard(makeRow({
      contract_amount_inc_tax: {
        state: "partial",
        value: "80000.00",
        as_of: null,
      },
    }));
    const text = container.textContent ?? "";
    expect(text).toContain("已知小计 ¥80,000（合同事实不完整）");
    expect(text).not.toContain("（38%）");
    // 只保留成本率进度条；不完整合同额不得生成回款进度条。
    expect(container.querySelectorAll(".ant-progress")).toHaveLength(1);
  });

  it("完整合同额为 0 时明确展示真实零，不伪装成缺失", () => {
    const { container } = renderCard(makeRow({
      contract_amount_inc_tax: ready("0.00"),
    }));
    const text = container.textContent ?? "";
    expect(text).toContain("合同额 ¥0（真实零，无法计算比例）");
    expect(text).not.toContain("合同额缺失");
    // 真实零不能作为比例分母，因此仍只保留成本率进度条。
    expect(container.querySelectorAll(".ant-progress")).toHaveLength(1);
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

  it("全部缺价时显示待补价而不是备件成本 0", () => {
    const { container } = renderCard(makeRow({
      card_status: null,
      cost_ratio_pct: ready(null as unknown as string),
      known_apply_cost_inc_tax: {
        state: "partial",
        value: {
          actual_amount: "0.00",
          estimated_amount: "0.00",
          known_amount: "0.00",
          missing_lines: 2,
          coverage_pct: 0,
          quality: "incomplete",
        },
        as_of: null,
      },
    }));
    expect(container.textContent).toContain("暂无可计算成本");
    expect(container.textContent).toContain("缺失 2 行无参照价");
    expect(container.textContent).not.toContain("备件成本 0.00 元");
  });

  it("只有需求单头没有有效明细时不显示备件成本 0 或绿色正常", () => {
    const { container } = renderCard(makeRow({
      card_status: null,
      cost_ratio_pct: ready(null as unknown as string),
      known_apply_cost_inc_tax: {
        state: "partial",
        value: {
          actual_amount: "0.00",
          estimated_amount: "0.00",
          known_amount: null,
          missing_lines: 0,
          coverage_pct: null,
          quality: "incomplete",
        },
        as_of: null,
      },
    }));
    expect(container.textContent).toContain("暂无可计算成本");
    expect(container.textContent).toContain("暂无有效需求明细");
    expect(container.textContent).not.toContain("备件成本 0.00 元");
    expect(container.textContent).not.toContain("正常");
  });

  it("部分缺价时把金额和成本率明确标成已知下限", () => {
    const { container } = renderCard(makeRow({
      card_status: "warning",
      cost_ratio_pct: ready("85.0"),
      known_apply_cost_inc_tax: {
        state: "partial",
        value: {
          actual_amount: "85000.00",
          estimated_amount: "0.00",
          known_amount: "85000.00",
          missing_lines: 1,
          coverage_pct: 80,
          quality: "incomplete",
        },
        as_of: null,
      },
    }));
    expect(container.textContent).toContain("85000.00 元（已知下限）");
    expect(container.textContent).toContain("85%（已知下限）");
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

  it("始终显示维保起止期限，缺失哪端就明确提示待补", () => {
    renderCard(makeRow({ period_from: "2026-01-01", period_to: "2026-12-31" }));
    expect(screen.getByTestId("maintenance-period"))
      .toHaveTextContent("维保期限：2026-01-01 ～ 2026-12-31");
    cleanup();

    renderCard(makeRow({ period_from: null, period_to: "2026-12-31", lifecycle: "missing" }));
    expect(screen.getByTestId("maintenance-period"))
      .toHaveTextContent("维保期限：起始待补 ～ 2026-12-31");
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
