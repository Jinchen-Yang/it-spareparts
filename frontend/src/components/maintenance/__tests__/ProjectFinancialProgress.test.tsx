import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ProjectFinancialProgress, {
  classifyCostWaterline,
} from "../ProjectFinancialProgress";

describe("项目双进度", () => {
  it.each([
    [799, "normal"],
    [800, "yellow"],
    [1000, "yellow"],
    [1000.01, "red"],
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
    expect(within(cost).getByText(/现场领用占合同额 45%/)).toBeInTheDocument();
  });
});
