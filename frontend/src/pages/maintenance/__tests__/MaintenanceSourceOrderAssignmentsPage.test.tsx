import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const listMaintenanceSourceOrders = vi.fn();
const assignMaintenanceSourceOrders = vi.fn();
const unassignMaintenanceSourceOrders = vi.fn();
const listMaintenanceProjects = vi.fn();

vi.mock("../../../api/maintenanceSourceAssignments", () => ({
  listMaintenanceSourceOrders: (...args: unknown[]) => listMaintenanceSourceOrders(...args),
  assignMaintenanceSourceOrders: (...args: unknown[]) => assignMaintenanceSourceOrders(...args),
  unassignMaintenanceSourceOrders: (...args: unknown[]) => unassignMaintenanceSourceOrders(...args),
}));

vi.mock("../../../api/maintenanceProjects", () => ({
  listMaintenanceProjects: (...args: unknown[]) => listMaintenanceProjects(...args),
}));

import MaintenanceSourceOrderAssignmentsPage, {
  reconcileSourceOrderSelection,
} from "../MaintenanceSourceOrderAssignmentsPage";

const UNASSIGNED_ROW = {
  raw_order_id: "WBDD-SYNTH-RAW-001",
  order_no: "WBDD-SYNTH-001",
  order_date: "2026-01-15",
  project_raw: "原始项目文字甲",
  project_std: "标准化展示文字甲",
  assignment_id: null,
  assignment_version: null,
  assigned_project: null,
};
const ACTIVE_PROJECT = {
  project_id: "project-1",
  project_code: "XM-001",
  display_name: "稳定项目甲",
  project_manager_id: "manager-1",
  lifecycle_status: "missing",
  is_active: true,
  version: 3,
};
const SECOND_PROJECT = {
  ...ACTIVE_PROJECT,
  project_id: "project-2",
  project_code: "XM-002",
  display_name: "稳定项目乙",
};
const THIRD_PROJECT = {
  ...ACTIVE_PROJECT,
  project_id: "project-3",
  project_code: "XM-003",
  display_name: "稳定项目丙",
};
const ASSIGNED_ROW = {
  ...UNASSIGNED_ROW,
  raw_order_id: "WBDD-SYNTH-RAW-002",
  order_no: "WBDD-SYNTH-002",
  assignment_id: "assignment-1",
  assignment_version: 4,
  assigned_project: {
    project_id: ACTIVE_PROJECT.project_id,
    project_code: ACTIVE_PROJECT.project_code,
    display_name: ACTIVE_PROJECT.display_name,
    is_active: true,
  },
};

function directory(rows: object[] = [UNASSIGNED_ROW]) {
  return Promise.resolve({
    data: { rows, total: rows.length, page: 1, page_size: 50 },
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "readonly");
  localStorage.setItem("permissions", JSON.stringify({ page_maintenance: true }));
  listMaintenanceSourceOrders.mockReturnValue(directory());
  listMaintenanceProjects.mockResolvedValue({
    data: { rows: [], total: 0, page: 1, page_size: 100 },
  });
  window.history.replaceState({}, "", "/maintenance/project-master/source-orders");
});

afterEach(() => cleanup());

describe("MaintenanceSourceOrderAssignmentsPage", () => {
  it("项目工作台深链默认展示该项目已归属的来源维保单", async () => {
    window.history.replaceState(
      {},
      "",
      `/maintenance/project-master/source-orders?project_id=${ACTIVE_PROJECT.project_id}`,
    );
    listMaintenanceSourceOrders.mockReturnValue(directory([ASSIGNED_ROW]));

    render(<MaintenanceSourceOrderAssignmentsPage />);

    expect(await screen.findByText("WBDD-SYNTH-002")).toBeInTheDocument();
    await waitFor(() => expect(listMaintenanceSourceOrders).toHaveBeenCalledWith({
      assignment_status: "assigned",
      project_id: ACTIVE_PROJECT.project_id,
      page: 1,
      page_size: 50,
    }));
  });

  it("跨页选择最多保留 100 张且第 101 张不会替换既有草稿", () => {
    const rows = Array.from({ length: 101 }, (_, index) => ({
      ...UNASSIGNED_ROW,
      raw_order_id: `WBDD-SYNTH-SELECTION-${index.toString().padStart(3, "0")}`,
    }));
    const firstHundred = reconcileSourceOrderSelection(
      [],
      rows,
      rows.slice(0, 100).map((row) => row.raw_order_id),
    );
    const rejected = reconcileSourceOrderSelection(
      firstHundred,
      rows,
      rows.map((row) => row.raw_order_id),
    );

    expect(firstHundred).toHaveLength(100);
    expect(rejected).toEqual(firstHundred);
  });

  it("默认逐张展示未归属来源维保单且无写权限时隐藏全部写入口", async () => {
    render(<MaintenanceSourceOrderAssignmentsPage />);

    expect(await screen.findByText("WBDD-SYNTH-001")).toBeInTheDocument();
    expect(screen.getByText("2026-01-15")).toBeInTheDocument();
    expect(screen.getByText("原始项目文字甲")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "未归属" })).toBeInTheDocument();
    await waitFor(() => expect(listMaintenanceSourceOrders).toHaveBeenCalledWith({
      assignment_status: "unassigned",
      page: 1,
      page_size: 50,
    }));
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "批量归属" })).toBeNull();
    expect(screen.queryByRole("button", { name: "改派" })).toBeNull();
    expect(screen.queryByRole("button", { name: "撤销归属" })).toBeNull();
  });

  it("实名写权限用户只能把明确勾选项批量归到所选稳定项目", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_profit: true,
      action_maintenance_project_manage: true,
    }));
    listMaintenanceProjects.mockResolvedValue({
      data: { rows: [ACTIVE_PROJECT], total: 1, page: 1, page_size: 100 },
    });
    assignMaintenanceSourceOrders.mockResolvedValue({ data: { assignments: [] } });
    render(<MaintenanceSourceOrderAssignmentsPage />);
    await screen.findByText("WBDD-SYNTH-001");

    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[checkboxes.length - 1]);
    fireEvent.click(screen.getByRole("button", { name: "批量归属" }));

    const dialog = await screen.findByRole("dialog", { name: "批量归属来源维保单" });
    expect(within(dialog).getByTestId("source-assignment-selection-review"))
      .toHaveTextContent("WBDD-SYNTH-001 · 原始项目文字甲 · WBDD-SYNTH-RAW-001");
    fireEvent.mouseDown(within(dialog).getByRole("combobox"));
    fireEvent.click(await screen.findByText("XM-001 · 稳定项目甲"));
    fireEvent.change(within(dialog).getByLabelText("归属原因"), {
      target: { value: "逐张核对业务单据后确认" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认归属" }));

    await waitFor(() => expect(assignMaintenanceSourceOrders).toHaveBeenCalledWith({
      project_id: "project-1",
      items: [{
        source_order_id: "WBDD-SYNTH-RAW-001",
        expected_assignment_id: null,
        expected_version: null,
      }],
      reason: "逐张核对业务单据后确认",
    }));
  });

  it("已归属单据可显式改派或撤销且都要求二次确认和原因", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_profit: true,
      action_maintenance_project_manage: true,
    }));
    listMaintenanceSourceOrders.mockReturnValue(directory([ASSIGNED_ROW]));
    listMaintenanceProjects.mockResolvedValue({
      data: { rows: [ACTIVE_PROJECT, SECOND_PROJECT], total: 2, page: 1, page_size: 100 },
    });
    assignMaintenanceSourceOrders.mockResolvedValue({ data: { assignments: [] } });
    unassignMaintenanceSourceOrders.mockResolvedValue({ data: { assignments: [] } });
    render(<MaintenanceSourceOrderAssignmentsPage />);
    await screen.findByText("WBDD-SYNTH-002");

    fireEvent.click(screen.getByRole("button", { name: /改\s*派/ }));
    const reassign = await screen.findByRole("dialog", { name: "改派来源维保单" });
    expect(within(reassign).getByText(/将替换当前归属/)).toBeInTheDocument();
    fireEvent.mouseDown(within(reassign).getByRole("combobox"));
    expect(await screen.findByText("XM-002 · 稳定项目乙")).toBeInTheDocument();
    expect(screen.queryByText("XM-001 · 稳定项目甲")).toBeNull();
    fireEvent.click(screen.getByText("XM-002 · 稳定项目乙"));
    fireEvent.change(within(reassign).getByLabelText("改派原因"), {
      target: { value: "业务复核后确认改派" },
    });
    fireEvent.click(within(reassign).getByRole("button", { name: "确认改派" }));
    await waitFor(() => expect(assignMaintenanceSourceOrders).toHaveBeenCalledWith({
      project_id: "project-2",
      items: [{
        source_order_id: ASSIGNED_ROW.raw_order_id,
        expected_assignment_id: "assignment-1",
        expected_version: 4,
      }],
      reason: "业务复核后确认改派",
    }));
    cleanup();
    render(<MaintenanceSourceOrderAssignmentsPage />);
    await screen.findByText("WBDD-SYNTH-002");
    fireEvent.click(screen.getByRole("button", { name: "撤销归属" }));
    const unassign = await screen.findByRole("dialog", { name: "撤销来源维保单归属" });
    expect(within(unassign).getByText(/不会删除来源维保单/)).toBeInTheDocument();
    fireEvent.change(within(unassign).getByLabelText("撤销原因"), {
      target: { value: "复核后暂时无法确定稳定项目" },
    });
    fireEvent.click(within(unassign).getByRole("button", { name: "确认撤销" }));
    await waitFor(() => expect(unassignMaintenanceSourceOrders).toHaveBeenCalledWith({
      items: [{ assignment_id: "assignment-1", expected_version: 4 }],
      reason: "复核后暂时无法确定稳定项目",
    }));
  });

  it("409 冲突保留目标项目与原因并允许刷新版本后人工再次确认", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_profit: true,
      action_maintenance_project_manage: true,
    }));
    listMaintenanceProjects.mockResolvedValue({
      data: { rows: [ACTIVE_PROJECT], total: 1, page: 1, page_size: 100 },
    });
    assignMaintenanceSourceOrders.mockRejectedValue({
      response: { status: 409, data: { detail: "项目归属已变化，请刷新后重试" } },
    });
    render(<MaintenanceSourceOrderAssignmentsPage />);
    await screen.findByText("WBDD-SYNTH-001");
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[checkboxes.length - 1]);
    fireEvent.click(screen.getByRole("button", { name: "批量归属" }));
    const dialog = await screen.findByRole("dialog", { name: "批量归属来源维保单" });
    fireEvent.mouseDown(within(dialog).getByRole("combobox"));
    fireEvent.click(await screen.findByText("XM-001 · 稳定项目甲"));
    fireEvent.change(within(dialog).getByLabelText("归属原因"), {
      target: { value: "保留的业务核对说明" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认归属" }));

    expect(await within(dialog).findByText("项目归属已变化，请刷新后重试")).toBeInTheDocument();
    expect(within(dialog).getByDisplayValue("保留的业务核对说明")).toBeInTheDocument();
    expect(within(dialog).getByText("XM-001 · 稳定项目甲")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "刷新目录并保留草稿" }))
      .toBeInTheDocument();
    expect(assignMaintenanceSourceOrders).toHaveBeenCalledTimes(1);
    fireEvent.click(within(dialog).getByRole("button", { name: "刷新目录并保留草稿" }));
    await waitFor(() => expect(listMaintenanceSourceOrders).toHaveBeenLastCalledWith({
      source_order_id: [UNASSIGNED_ROW.raw_order_id],
      assignment_status: "all",
      page: 1,
      page_size: 100,
    }));
    expect(within(dialog).getByDisplayValue("保留的业务核对说明")).toBeInTheDocument();
    expect(within(dialog).getByText("XM-001 · 稳定项目甲")).toBeInTheDocument();
    expect(within(dialog).getByText(/已明确选择 1 张来源维保单/)).toBeInTheDocument();
    expect(assignMaintenanceSourceOrders).toHaveBeenCalledTimes(1);
  });

  it("批量冲突刷新后排除每张已选单据的当前项目", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_profit: true,
      action_maintenance_project_manage: true,
    }));
    const pendingRows = [
      UNASSIGNED_ROW,
      {
        ...UNASSIGNED_ROW,
        raw_order_id: "WBDD-SYNTH-RAW-003",
        order_no: "WBDD-SYNTH-003",
      },
    ];
    const refreshedRows = [
      {
        ...pendingRows[0],
        assignment_id: "assignment-current-1",
        assignment_version: 2,
        assigned_project: {
          project_id: ACTIVE_PROJECT.project_id,
          project_code: ACTIVE_PROJECT.project_code,
          display_name: ACTIVE_PROJECT.display_name,
          is_active: true,
        },
      },
      {
        ...pendingRows[1],
        assignment_id: "assignment-current-2",
        assignment_version: 5,
        assigned_project: {
          project_id: SECOND_PROJECT.project_id,
          project_code: SECOND_PROJECT.project_code,
          display_name: SECOND_PROJECT.display_name,
          is_active: true,
        },
      },
    ];
    listMaintenanceProjects.mockResolvedValue({
      data: {
        rows: [ACTIVE_PROJECT, SECOND_PROJECT, THIRD_PROJECT],
        total: 3,
        page: 1,
        page_size: 100,
      },
    });
    listMaintenanceSourceOrders
      .mockReturnValueOnce(directory(pendingRows))
      .mockReturnValueOnce(directory(refreshedRows));
    assignMaintenanceSourceOrders.mockRejectedValue({
      response: { status: 409, data: { detail: "项目归属已变化，请刷新后重试" } },
    });
    render(<MaintenanceSourceOrderAssignmentsPage />);
    await screen.findByText("WBDD-SYNTH-001");
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[1]);
    fireEvent.click(checkboxes[2]);
    fireEvent.click(screen.getByRole("button", { name: "批量归属" }));
    const dialog = await screen.findByRole("dialog", { name: "批量归属来源维保单" });
    fireEvent.mouseDown(within(dialog).getByRole("combobox"));
    fireEvent.click(await screen.findByText("XM-003 · 稳定项目丙"));
    fireEvent.change(within(dialog).getByLabelText("归属原因"), {
      target: { value: "保留批量冲突草稿" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认归属" }));
    await within(dialog).findByText("项目归属已变化，请刷新后重试");

    fireEvent.click(within(dialog).getByRole("button", { name: "刷新目录并保留草稿" }));

    await waitFor(() => expect(listMaintenanceSourceOrders).toHaveBeenCalledTimes(2));
    expect(within(dialog).getByDisplayValue("保留批量冲突草稿")).toBeInTheDocument();
    expect(within(dialog).getByText("XM-003 · 稳定项目丙")).toBeInTheDocument();
    fireEvent.mouseDown(within(dialog).getByRole("combobox"));
    expect(screen.queryByText("XM-001 · 稳定项目甲")).toBeNull();
    expect(screen.queryByText("XM-002 · 稳定项目乙")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "确认归属" })).not.toBeDisabled();
  });

  it("冲突刷新未返回所选单号时不会静默丢弃草稿", async () => {
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_profit: true,
      action_maintenance_project_manage: true,
    }));
    listMaintenanceProjects.mockResolvedValue({
      data: { rows: [ACTIVE_PROJECT], total: 1, page: 1, page_size: 100 },
    });
    assignMaintenanceSourceOrders.mockRejectedValue({
      response: { status: 409, data: { detail: "项目归属已变化，请刷新后重试" } },
    });
    listMaintenanceSourceOrders
      .mockReturnValueOnce(directory())
      .mockReturnValueOnce(directory([]));
    render(<MaintenanceSourceOrderAssignmentsPage />);
    await screen.findByText("WBDD-SYNTH-001");
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[checkboxes.length - 1]);
    fireEvent.click(screen.getByRole("button", { name: "批量归属" }));
    const dialog = await screen.findByRole("dialog", { name: "批量归属来源维保单" });
    fireEvent.mouseDown(within(dialog).getByRole("combobox"));
    fireEvent.click(await screen.findByText("XM-001 · 稳定项目甲"));
    fireEvent.change(within(dialog).getByLabelText("归属原因"), {
      target: { value: "保留无法刷新的草稿" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认归属" }));
    await within(dialog).findByText("项目归属已变化，请刷新后重试");

    fireEvent.click(within(dialog).getByRole("button", { name: "刷新目录并保留草稿" }));

    await waitFor(() => expect(listMaintenanceSourceOrders).toHaveBeenCalledTimes(2));
    expect(within(dialog).getByText(/已明确选择 1 张来源维保单/)).toBeInTheDocument();
    expect(within(dialog).getByDisplayValue("保留无法刷新的草稿")).toBeInTheDocument();
    expect(within(dialog).getByText("XM-001 · 稳定项目甲")).toBeInTheDocument();
  });
});
