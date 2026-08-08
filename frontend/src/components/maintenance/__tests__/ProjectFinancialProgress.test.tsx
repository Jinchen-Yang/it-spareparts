import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";

import ProjectFinancialProgress, {
  classifyCostWaterline,
} from "../ProjectFinancialProgress";

afterEach(cleanup);

describe("项目双进度", () => {
  it.each([
    [799, "normal"],
    [799.99, "yellow"],
    [800, "yellow"],
    [1000, "yellow"],
    [1000.01, "yellow"],
    [1000.04, "yellow"],
    [1000.1, "red"],
  ] as const)("合同额 1000、已知项目成本 %s 时为 %s", (actualCost, expected) => {
    expect(classifyCostWaterline({
      totalContractAmount: 1000,
      actualProjectCostKnown: actualCost,
      costComplete: true,
    }).status).toBe(expected);
  });

  it("缺成本且已知下限不足 80% 时保持未知，不伪装为绿色", () => {
    expect(classifyCostWaterline({
      totalContractAmount: 1000,
      actualProjectCostKnown: 500,
      costComplete: false,
    })).toMatchObject({ status: "unknown", percent: 50 });
  });

  it("直接使用服务端 Decimal HALF_UP 的成本率与红黄状态", () => {
    render(<ProjectFinancialProgress metrics={{
      total_contract_amount: 200,
      contract_amount_complete: true,
      received_amount: 200.01,
      collection_progress_pct: 100.01,
      site_requisition_known_cost: 200.01,
      approved_expense: 0,
      actual_project_cost_known: 200.01,
      cost_rate_lower_bound_pct: 100.01,
      cost_status: "red",
      cost_complete: true,
      missing_cost_lines: 0,
    }} />);

    expect(screen.getByTestId("collection-progress")).toHaveTextContent("100.01%");
    const cost = screen.getByTestId("project-cost-progress");
    expect(cost).toHaveTextContent("100.01%");
    expect(within(cost).getByText("超过 100%")).toBeInTheDocument();
  });

  it("合同额证据不完整时两条进度都不计算百分比", () => {
    render(<ProjectFinancialProgress metrics={{
      total_contract_amount: 1000,
      contract_amount_complete: false,
      received_amount: 300,
      site_requisition_known_cost: 450,
      approved_expense: 50,
      actual_project_cost_known: 500,
      cost_complete: true,
      missing_cost_lines: 0,
    }} />);

    const collection = screen.getByTestId("collection-progress");
    const cost = screen.getByTestId("project-cost-progress");
    expect(collection).not.toHaveTextContent("30%");
    expect(cost).not.toHaveTextContent("50%");
    expect(screen.getAllByText("合同额证据不完整，暂不计算比例。")).toHaveLength(2);
  });

  it("成本字段被脱敏时只说明不可见，不推断缺价或成本比例", () => {
    render(<ProjectFinancialProgress metrics={{
      total_contract_amount: 1000,
      contract_amount_complete: true,
      received_amount: 300,
      site_requisition_known_cost: null,
      approved_expense: null,
      actual_project_cost_known: null,
      cost_complete: null,
      missing_cost_lines: null,
    }} />);

    const cost = screen.getByTestId("project-cost-progress");
    expect(within(cost).getByText("成本不可见/无权限")).toBeInTheDocument();
    expect(cost).not.toHaveTextContent("缺 null 行成本");
    expect(cost).not.toHaveTextContent("成本待补");
    expect(cost).not.toHaveTextContent("已知下限");
  });

  it("合同额权限受限时不冒充证据不完整，也不展示百分比", () => {
    render(<ProjectFinancialProgress metrics={{
      total_contract_amount: null,
      contract_amount_complete: null,
      received_amount: null,
      site_requisition_known_cost: 30,
      approved_expense: 20,
      actual_project_cost_known: 50,
      cost_complete: true,
      missing_cost_lines: 0,
    }} />);

    expect(screen.getByText("合同额不可见，暂不计算比例。")).toBeInTheDocument();
    expect(screen.getByText("合同额不可见/无权限")).toBeInTheDocument();
    expect(screen.queryByText("合同额证据不完整，暂不计算比例。")).toBeNull();
  });

  it("没有可计算合同额时仍展示缺价事实，但不拼接空的下限文案", () => {
    render(<ProjectFinancialProgress metrics={{
      total_contract_amount: null,
      contract_amount_complete: true,
      received_amount: 0,
      site_requisition_known_cost: 30,
      approved_expense: 20,
      actual_project_cost_known: 50,
      cost_complete: false,
      missing_cost_lines: 2,
    }} />);

    const cost = screen.getByTestId("project-cost-progress");
    expect(within(cost).getByText("缺 2 行成本；当前无可计算合同额，暂不显示下限。")).toBeInTheDocument();
    expect(cost).not.toHaveTextContent("；，");
  });

  it("展示回款、项目实际成本两条进度，并拆分现场领用与审批通过报销", () => {
    render(<ProjectFinancialProgress metrics={{
      total_contract_amount: 1000,
      contract_amount_complete: true,
      received_amount: 600,
      site_requisition_known_cost: 450,
      approved_expense: 350,
      actual_project_cost_known: 800,
      cost_complete: false,
      missing_cost_lines: 3,
    }} />);

    expect(screen.getByText("回款 / 全部合同额")).toBeInTheDocument();
    const cost = screen.getByTestId("project-cost-progress");
    expect(within(cost).getByText("项目实际成本 / 全部合同额")).toBeInTheDocument();
    expect(within(cost).getByText(/现场领用已知成本 ¥450/)).toBeInTheDocument();
    expect(within(cost).getByText(/审批通过报销 ¥350/)).toBeInTheDocument();
    expect(within(cost).getByText(/缺 3 行成本/)).toBeInTheDocument();
    expect(within(cost).getAllByText("已知下限 ≥80%").length).toBeGreaterThanOrEqual(1);
    expect(within(cost).getByText(/现场领用占合同额 45%/)).toBeInTheDocument();
    expect(within(cost).queryByText("低于 80%")).toBeNull();
  });
});
