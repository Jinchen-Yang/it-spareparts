import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { MaintenanceCollectionProgress } from "../../../api/maintenanceBatchTransfer";
import CollectionProgressPreview from "../CollectionProgressPreview";

type State = MaintenanceCollectionProgress["before"];
type Milestone = State["milestones"][number];

function milestone(sequence: number, received: string | null, remaining: string | null, paymentState: Milestone["payment_state"]): Milestone {
  return {
    milestone_id: `period-${sequence}`, sequence,
    planned_date: `2026-0${sequence}-01`, planned_amount: "100.00",
    payment_state: paymentState, received_for_period: received, remaining_amount: remaining,
  };
}

const before: State = {
  report_month: "2026-01-01", cumulative_received: "40.00",
  milestones: [milestone(1, "40.00", "60.00", "partial"), milestone(2, "0.00", "100.00", "unpaid")],
};
const later: State = {
  report_month: "2026-02-01", cumulative_received: "150.00",
  milestones: [milestone(1, "100.00", "0.00", "paid"), milestone(2, "50.00", "50.00", "partial")],
};
const contract: MaintenanceCollectionProgress = {
  contract_id: "contract-1", project_id: "project-1", contract_no: "XSDD-20260101-0001", project_name: "项目一",
  before,
  candidates: [{ row_key: "newer", report_month: "2026-02-01", after: later }],
};

afterEach(cleanup);

function show(value = contract, selectedRowKeys = ["newer"], selectionValid = true) {
  render(<CollectionProgressPreview progress={[value]} selectedRowKeys={selectedRowKeys} selectionValid={selectionValid} />);
  fireEvent.click(screen.getByRole("button", { name: /项目一/ }));
}

describe("CollectionProgressPreview", () => {
  it("defaults to collapsed contracts and explains one payment covering several periods", () => {
    render(<CollectionProgressPreview progress={[contract]} selectedRowKeys={["newer"]} selectionValid />);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText(/一笔收款可以覆盖多期/)).toBeInTheDocument();
    expect(screen.getByText("→ 勾选后累计 ¥150.00")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /项目一/ }));
    const first = screen.getByRole("row", { name: /第 1 期/ });
    expect(within(first).getByText("部分到账")).toBeInTheDocument();
    expect(within(first).getByText("已到账")).toBeInTheDocument();
    const second = screen.getByRole("row", { name: /第 2 期/ });
    expect(within(second).getByText("部分到账")).toBeInTheDocument();
    expect(within(second).getAllByText("¥50.00")).toHaveLength(2);
  });

  it("uses only the latest selected cumulative candidate without adding amounts", () => {
    show({ ...contract, candidates: [
      { row_key: "newer", report_month: "2026-02-01", after: later },
      { row_key: "older", report_month: "2026-01-01", after: before },
    ] }, ["older", "newer"]);
    expect(screen.getByText("→ 勾选后累计 ¥150.00")).toBeInTheDocument();
    expect(screen.queryByText(/¥190\.00/)).not.toBeInTheDocument();
  });

  it("does not count an unselected overwrite", () => {
    show({ ...contract, candidates: [{ row_key: "overwrite", report_month: "2026-01-01", after: later }] }, ["unrelated"]);
    expect(screen.getByText("→ 勾选后累计 ¥40.00")).toBeInTheDocument();
    expect(screen.getByText(/未勾选本合同的收款/)).toBeInTheDocument();
    expect(screen.queryByText("已到账")).not.toBeInTheDocument();
  });

  it("keeps the latest snapshot when only an older month is selected", () => {
    show({ ...contract, before: later, candidates: [{ row_key: "older", report_month: "2026-01-01", after: before }] }, ["older"]);
    expect(screen.getByText("→ 勾选后累计 ¥150.00")).toBeInTheDocument();
    expect(screen.getByText(/补录较早月份/)).toBeInTheDocument();
  });

  it("leaves before values unchanged when nothing is selected", () => {
    show(contract, []);
    expect(screen.getByText("导入前累计 ¥40.00")).toBeInTheDocument();
    expect(screen.getByText("→ 勾选后累计 ¥40.00")).toBeInTheDocument();
  });

  it("does not present after progress while required selections are missing", () => {
    show(contract, ["newer"], false);
    expect(screen.getByText(/补齐依赖行后才计算/)).toBeInTheDocument();
    expect(screen.queryByText(/勾选后累计/)).not.toBeInTheDocument();
    expect(screen.queryByText("已到账")).not.toBeInTheDocument();
    expect(screen.getAllByText("待完成勾选").length).toBeGreaterThan(1);
  });

  it("shows incomplete plans and unknown amounts without turning them into zero", () => {
    const incomplete: State = {
      report_month: null, cumulative_received: null,
      milestones: [{ ...milestone(1, null, null, "incomplete"), planned_amount: null }],
    };
    show({ ...contract, before: incomplete, candidates: [] }, []);
    expect(screen.getByText(/计划不完整，后续期次/)).toBeInTheDocument();
    expect(screen.getAllByText("无法判断")).toHaveLength(4);
    expect(screen.getByText("未填写")).toBeInTheDocument();
    expect(screen.queryByText("¥0.00")).not.toBeInTheDocument();
  });

  it("shows unreported receipts separately from a zero receipt", () => {
    const unreported: State = {
      report_month: null, cumulative_received: null,
      milestones: [milestone(1, null, null, "not_reported")],
    };
    show({ ...contract, before: unreported, candidates: [] }, []);
    expect(screen.getByText("导入前累计 未报")).toBeInTheDocument();
    expect(screen.getAllByText("未报").length).toBeGreaterThan(1);
    expect(screen.queryByText("¥0.00")).not.toBeInTheDocument();
  });

  it("permits receipts with no plan and formats large decimals without arithmetic", () => {
    const noPlan = { report_month: null, cumulative_received: null, milestones: [] };
    show({ ...contract, before: noPlan, candidates: [{ row_key: "newer", report_month: "2026-02-01", after: { ...noPlan, report_month: "2026-02-01", cumulative_received: "999999999999.99" } }] });
    expect(screen.getByText(/未配置回款计划；收款仍可正常导入/)).toBeInTheDocument();
    expect(screen.getByText("→ 勾选后累计 ¥999,999,999,999.99")).toBeInTheDocument();
  });

  it("renders no empty section when the preview has no contracts", () => {
    const { container } = render(<CollectionProgressPreview progress={[]} selectedRowKeys={[]} selectionValid />);
    expect(container).toBeEmptyDOMElement();
  });
});
