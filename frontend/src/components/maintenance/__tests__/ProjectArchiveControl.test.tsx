import { cleanup, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
const mocks = vi.hoisted(() => ({ archive: vi.fn(), restore: vi.fn(), list: vi.fn(), search: vi.fn() }));
vi.mock("../../../api/maintenanceProjects", () => ({ archiveMaintenanceProject: mocks.archive,
  restoreMaintenanceProject: mocks.restore, listMaintenanceProjects: mocks.list, searchMaintenanceProjects: mocks.search }));
import ProjectArchiveControl from "../ProjectArchiveControl";
import MaintenanceProjectDirectoryButton from "../MaintenanceProjectDirectoryButton";
import type { MaintenanceProject } from "../../../api/maintenanceProjects";
const project: MaintenanceProject = { project_id: "p1", project_code: "XSDD-1", display_name: "测试项目",
  salesperson: null, salesperson_override_active: false, project_manager_id: null,
  period_from: null, period_to: null, lifecycle_status: "ongoing", is_active: true, version: 4 };
beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); localStorage.setItem("token", "owner"); });
afterEach(cleanup);

describe("可恢复项目归档", () => {
  it("必填原因并提交当前版本，保存后刷新失败仍允许正确版本恢复", async () => {
    mocks.archive.mockResolvedValue({ data: { ...project, is_active: false, version: 5 } });
    mocks.restore.mockResolvedValue({ data: { ...project, version: 6 } });
    render(<ProjectArchiveControl project={project} onChanged={async () => false} />);
    fireEvent.click(screen.getByRole("button", { name: "归档项目" }));
    expect(screen.getByText(/有有效需求的项目仍在统计中显示/)).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "确认归档项目" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText("归档或恢复原因"), { target: { value: "重复容器已核对" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(mocks.archive).toHaveBeenCalledWith("p1", { version: 4, reason: "重复容器已核对" }));
    expect(await screen.findByText(/操作已成功，但页面刷新失败/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "恢复项目" }));
    fireEvent.change(screen.getByLabelText("归档或恢复原因"), { target: { value: "重新启用" } });
    fireEvent.click(screen.getByRole("button", { name: "确认恢复项目" }));
    await waitFor(() => expect(mocks.restore).toHaveBeenCalledWith("p1", { version: 5, reason: "重新启用" }));
    expect(mocks.archive).toHaveBeenCalledTimes(1);
  });
  it("冲突保持弹窗并要求刷新，不自动改用新版本重试", async () => {
    mocks.archive.mockRejectedValue({ response: { status: 409 } });
    render(<ProjectArchiveControl project={project} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "归档项目" }));
    fireEvent.change(screen.getByLabelText("归档或恢复原因"), { target: { value: "核对" } });
    fireEvent.click(screen.getByRole("button", { name: "确认归档项目" }));
    expect(await screen.findByText("项目已被其他操作修改，请刷新后重新确认。")).toBeInTheDocument();
    expect(mocks.archive).toHaveBeenCalledTimes(1);
  });
  it("新账号不执行旧页面的归档命令", async () => {
    render(<ProjectArchiveControl project={project} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "归档项目" }));
    fireEvent.change(screen.getByLabelText("归档或恢复原因"), { target: { value: "核对" } });
    localStorage.setItem("token", "other");
    fireEvent.click(screen.getByRole("button", { name: "确认归档项目" }));
    expect(mocks.archive).not.toHaveBeenCalled();
    expect(screen.getByText("登录账号已变化，请刷新后继续")).toBeInTheDocument();
  });
  it("归档空项目可在目录中找回，按权限后的服务端全集分页", async () => {
    mocks.list.mockResolvedValue({ data: { rows: [{ ...project, is_active: false }], total: 24 } });
    mocks.search.mockResolvedValue({ data: { rows: [{ ...project, is_active: false }], total: 1 } });
    render(<MemoryRouter><MaintenanceProjectDirectoryButton /></MemoryRouter>);
    expect(mocks.list).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "项目目录（含归档）" }));
    expect(await screen.findByRole("link", { name: "测试项目" })).toHaveAttribute("href", "/maintenance/projects/p1");
    expect(screen.getByText("已归档")).toBeInTheDocument();
    expect(mocks.list).toHaveBeenCalledWith({ page: 1, page_size: 20, include_inactive: true });
    fireEvent.click(screen.getByTitle("Next Page"));
    await waitFor(() => expect(mocks.list).toHaveBeenLastCalledWith({ page: 2, page_size: 20, include_inactive: true }));
  });
});
