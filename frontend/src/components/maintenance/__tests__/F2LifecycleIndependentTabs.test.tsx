import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanupAntd, fireEvent, render, screen, waitFor } from "../../../test/antdRender";

const api = vi.hoisted(() => ({
  project: vi.fn(), board: vi.fn(), workspace: vi.fn(), expenses: vi.fn(),
  download: vi.fn(), validate: vi.fn(), apply: vi.fn(), saveBlob: vi.fn(),
}));
vi.mock("../../../api/maintenanceWorkbooks", async () => ({
  ...await vi.importActual("../../../api/maintenanceWorkbooks"),
  listProjectExpenseRows: api.expenses, downloadProjectMaster: api.download,
  validateProjectMaster: api.validate, applyProjectMaster: api.apply, saveBlob: api.saveBlob,
}));
vi.mock("../../../api/maintenanceProjects", async () => ({
  ...await vi.importActual("../../../api/maintenanceProjects"), getMaintenanceProject: api.project,
}));
vi.mock("../../../api/maintenanceBossBoard", async () => ({
  ...await vi.importActual("../../../api/maintenanceBossBoard"), getBoardProject: api.board,
}));
vi.mock("../../../api/maintenanceOperations", async () => ({
  ...await vi.importActual("../../../api/maintenanceOperations"), getMaintenanceProjectWorkspace: api.workspace,
}));
// ExpenseTab, its own state, and both workbook consumers are real.
vi.mock("../../../pages/maintenance/panel/OverviewTab", () => ({ default: () => null }));
vi.mock("../../../pages/maintenance/panel/PartsOrdersTab", () => ({ default: () => null }));
vi.mock("../../../pages/maintenance/panel/CollectionTab", () => ({ default: () => null }));
vi.mock("../../../pages/maintenance/panel/SiteReturnTab", () => ({ default: () => null }));
vi.mock("../../../pages/maintenance/panel/AcceptanceTab", () => ({ default: () => null }));
vi.mock("../../../pages/maintenance/panel/MaintenanceProjectAnalyticsContext", () => ({ default: () => null }));

import MaintenanceProjectPanelPage from "../../../pages/maintenance/MaintenanceProjectPanelPage";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function permissions(own = false) {
  localStorage.setItem("permissions", JSON.stringify({
    page_maintenance: true, data_purchase_cost: true, data_profit: true,
    action_maintenance_expense_collection_upload: true, own_customers_only: own,
  }));
}
beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear(); localStorage.setItem("token", "account-A"); localStorage.setItem("role", "staff");
  permissions();
  api.project.mockResolvedValue({ data: { project: {
    project_id: "A", project_code: "XSDD-A", display_name: "真实报销页签项目",
    is_active: true, lifecycle_status: "ongoing", version: 1, can_edit_master_workbook: true,
  } } });
  api.board.mockResolvedValue({ data: {
    project_id: "A", display_name: "真实报销页签项目", contract_nos: ["XSDD-A"],
    can_edit_master_workbook: true, known_apply_cost_inc_tax: { state: "unknown", value: null },
  } });
  api.workspace.mockResolvedValue({ data: {
    project: { project_id: "A", metrics: {} }, collection_snapshots: { rows: [], total: 0 },
  } });
  api.download.mockResolvedValue(new Blob(["synthetic workbook"]));
  api.validate.mockResolvedValue({ will_void_rows: [] });
});
afterEach(async () => {
  await cleanupAntd();
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
});

describe("F2 independent nested tab ownership", () => {
  it.each([
    ["success", "token"], ["error", "token"],
    ["success", "permissions"], ["error", "permissions"],
  ] as const)("late expense %s cannot renew a %s owner from a stale capability prop", async (outcome, identity) => {
    const old = deferred<{ rows: []; total: number }>();
    api.expenses.mockReturnValueOnce(old.promise);
    const view = render(<MemoryRouter initialEntries={["/maintenance/projects/A?tab=expense"]}>
      <Routes><Route path="/maintenance/projects/:projectId" element={<MaintenanceProjectPanelPage />} /></Routes>
    </MemoryRouter>);
    await screen.findByRole("heading", { name: "真实报销页签项目" });
    await screen.findByRole("button", { name: /下载报销/ });
    await waitFor(() => expect(api.expenses).toHaveBeenCalledTimes(1));
    await act(async () => {}); // Let parent loaders finish before changing identity.
    expect(api.project).toHaveBeenCalledTimes(1);
    expect(api.board).toHaveBeenCalledTimes(1);
    expect(api.workspace).toHaveBeenCalledTimes(1);
    if (identity === "token") localStorage.setItem("token", "account-B");
    else permissions(true);
    await act(async () => {
      if (outcome === "success") old.resolve({ rows: [], total: 0 });
      else old.reject(new Error("synthetic old expense read failure"));
    });
    expect(api.project).toHaveBeenCalledTimes(1);
    const button = screen.queryByRole("button", { name: /下载报销/ });
    if (button) fireEvent.click(button);
    const inputs = view.container.querySelectorAll<HTMLInputElement>('input[type="file"]');
    if (inputs.length > 1) fireEvent.change(inputs[1], { target: {
      files: [new File(["independent"], "scope.xlsx", { type: "application/vnd.ms-excel" })],
    } });
    await act(async () => {});
    expect(api.download).not.toHaveBeenCalled();
    expect(api.validate).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /下载报销/ })).toBeNull();
  });
});
