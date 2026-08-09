import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";

const listMaintenanceProjects = vi.fn();
const createMaintenanceProject = vi.fn();
const getMaintenanceProject = vi.fn();
const updateMaintenanceProject = vi.fn();
const archiveMaintenanceProject = vi.fn();
const restoreMaintenanceProject = vi.fn();

vi.mock("../../api/maintenanceProjects", () => ({
  listMaintenanceProjects: (...args: unknown[]) => listMaintenanceProjects(...args),
  createMaintenanceProject: (...args: unknown[]) => createMaintenanceProject(...args),
  getMaintenanceProject: (...args: unknown[]) => getMaintenanceProject(...args),
  updateMaintenanceProject: (...args: unknown[]) => updateMaintenanceProject(...args),
  archiveMaintenanceProject: (...args: unknown[]) => archiveMaintenanceProject(...args),
  restoreMaintenanceProject: (...args: unknown[]) => restoreMaintenanceProject(...args),
}));

import MaintenanceProjectMasterPage from "../MaintenanceProjectMasterPage";

const ACTIVE_PROJECT = {
  project_id: "project-1",
  project_code: "XM-001",
  display_name: "一号维保项目",
  project_manager_id: "manager-1",
  // Even legacy-looking values must not be interpreted until the authority is locked.
  lifecycle_status: "ongoing",
  is_active: true,
  version: 3,
};
const ARCHIVED_PROJECT = {
  ...ACTIVE_PROJECT,
  project_id: "project-2",
  project_code: "XM-ARCHIVED",
  display_name: "已归档项目",
  is_active: false,
  version: 7,
};

function directory(
  rows = [ACTIVE_PROJECT],
  options: { total?: number; page?: number } = {},
) {
  return Promise.resolve({
    data: {
      rows,
      total: options.total ?? rows.length,
      page: options.page ?? 1,
      page_size: 50,
      as_of: "2026-08-08",
      data_version: "directory-v1",
    },
  });
}

type DirectoryResponse = Awaited<ReturnType<typeof directory>>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function enableProjectManagement() {
  localStorage.setItem("role", "admin");
  localStorage.setItem("permissions", JSON.stringify({
    page_maintenance: true,
    action_maintenance_project_manage: true,
  }));
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "readonly");
  localStorage.setItem("permissions", JSON.stringify({ page_maintenance: true }));
  listMaintenanceProjects.mockReturnValue(directory());
  getMaintenanceProject.mockResolvedValue({ data: { project: ACTIVE_PROJECT } });
});

afterEach(() => {
  cleanup();
  message.destroy();
});

describe("MaintenanceProjectMasterPage", () => {
  it("没有动作权限时可查看主档但不展示写入口，包括共享管理员", async () => {
    localStorage.setItem("role", "admin");
    render(<MaintenanceProjectMasterPage />);

    expect(await screen.findByText("XM-001")).toBeInTheDocument();
    expect(screen.getByText("一号维保项目")).toBeInTheDocument();
    expect(screen.getByText("manager-1")).toBeInTheDocument();
    expect(screen.getByText("业务期限待确认")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "新建项目" })).toBeNull();
    expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();
    expect(screen.queryByRole("button", { name: "归档" })).toBeNull();
    expect(screen.queryByLabelText("生命周期状态")).toBeNull();
  });

  it("按服务端总数翻页，不会只展示默认前 50 个项目", async () => {
    listMaintenanceProjects.mockImplementation((params: { page: number }) => (
      params.page === 2
        ? directory([ARCHIVED_PROJECT], { total: 51, page: 2 })
        : directory([ACTIVE_PROJECT], { total: 51, page: 1 })
    ));
    render(<MaintenanceProjectMasterPage />);

    expect(await screen.findByText("XM-001")).toBeInTheDocument();
    expect(listMaintenanceProjects).toHaveBeenCalledWith({ page: 1, page_size: 50 });
    fireEvent.click(screen.getByTitle("2"));

    expect(await screen.findByText("XM-ARCHIVED")).toBeInTheDocument();
    expect(listMaintenanceProjects).toHaveBeenLastCalledWith({ page: 2, page_size: 50 });
  });

  it("快速翻页时迟到的旧响应不能覆盖当前页", async () => {
    const page2 = deferred<DirectoryResponse>();
    const page3 = deferred<DirectoryResponse>();
    listMaintenanceProjects
      .mockReturnValueOnce(directory([ACTIVE_PROJECT], { total: 101, page: 1 }))
      .mockReturnValueOnce(page2.promise)
      .mockReturnValueOnce(page3.promise);
    render(<MaintenanceProjectMasterPage />);
    await screen.findByText("XM-001");

    fireEvent.click(screen.getByTitle("2"));
    fireEvent.click(screen.getByTitle("3"));
    await act(async () => {
      page3.resolve(await directory([{
        ...ACTIVE_PROJECT,
        project_id: "project-page-3",
        project_code: "XM-PAGE-3",
      }], { total: 101, page: 3 }));
    });
    expect(await screen.findByText("XM-PAGE-3")).toBeInTheDocument();

    await act(async () => {
      page2.resolve(await directory([{
        ...ACTIVE_PROJECT,
        project_id: "project-page-2",
        project_code: "XM-PAGE-2",
      }], { total: 101, page: 2 }));
    });
    expect(screen.getByText("XM-PAGE-3")).toBeInTheDocument();
    expect(screen.queryByText("XM-PAGE-2")).toBeNull();
  });

  it("列表加载失败后可在原地重试", async () => {
    listMaintenanceProjects
      .mockRejectedValueOnce(new Error("network down"))
      .mockReturnValueOnce(directory());
    render(<MaintenanceProjectMasterPage />);

    expect(await screen.findByText("项目主档加载失败，请检查网络后重试。"))
      .toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));

    expect(await screen.findByText("XM-001")).toBeInTheDocument();
    expect(listMaintenanceProjects).toHaveBeenCalledTimes(2);
  });

  it.each([
    ["管理员", "admin", { action_maintenance_project_manage: true }],
    ["获授权维保人员", "readonly", { action_maintenance_project_manage: true }],
  ])("%s 可以看到主档写入口", async (_label, role, permissions) => {
    localStorage.setItem("role", role);
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      ...permissions,
    }));

    render(<MaintenanceProjectMasterPage />);

    await screen.findByText("XM-001");
    expect(screen.getByRole("button", { name: "新建项目" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "编辑" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "归档" })).toBeInTheDocument();
  });

  it("新建项目只提交稳定身份字段和必填原因", async () => {
    enableProjectManagement();
    createMaintenanceProject.mockResolvedValue({ data: ACTIVE_PROJECT });
    render(<MaintenanceProjectMasterPage />);
    await screen.findByText("XM-001");

    fireEvent.click(screen.getByRole("button", { name: "新建项目" }));
    fireEvent.change(screen.getByLabelText("稳定项目编号"), {
      target: { value: "XM-002" },
    });
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "二号维保项目" },
    });
    fireEvent.change(screen.getByLabelText("项目经理标识"), {
      target: { value: "manager-2" },
    });
    expect(screen.queryByLabelText("生命周期状态")).toBeNull();
    expect(screen.getByRole("button", { name: "保存建档" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "新合同立项" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存建档" }));

    await waitFor(() => expect(createMaintenanceProject).toHaveBeenCalledWith({
      project_code: "XM-002",
      display_name: "二号维保项目",
      project_manager_id: "manager-2",
      reason: "新合同立项",
    }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("编辑时用当前版本提交，清空负责人会显式发送 null", async () => {
    enableProjectManagement();
    updateMaintenanceProject.mockResolvedValue({
      data: { ...ACTIVE_PROJECT, project_manager_id: null, version: 4 },
    });
    render(<MaintenanceProjectMasterPage />);
    await screen.findByText("XM-001");

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    expect(screen.getByLabelText("稳定项目编号")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("项目经理标识"), {
      target: { value: "" },
    });
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "负责人离任" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => expect(updateMaintenanceProject).toHaveBeenCalledWith(
      "project-1",
      {
        version: 3,
        project_manager_id: null,
        reason: "负责人离任",
      },
    ));
  });

  it("编辑字段没有变化时即使填写原因也不能发送空 PATCH", async () => {
    enableProjectManagement();
    render(<MaintenanceProjectMasterPage />);
    await screen.findByText("XM-001");

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "没有实际变更" },
    });

    expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
    expect(updateMaintenanceProject).not.toHaveBeenCalled();
  });

  it("409 时保留抽屉草稿并明确提示刷新", async () => {
    enableProjectManagement();
    listMaintenanceProjects
      .mockReturnValueOnce(directory())
      .mockReturnValueOnce(directory([{
        ...ACTIVE_PROJECT,
        project_manager_id: "manager-2",
        version: 4,
      }]));
    updateMaintenanceProject
      .mockRejectedValueOnce({
        response: {
          status: 409,
          data: { detail: "项目主档已被他人修改（当前版本 4），请刷新后重试" },
        },
      })
      .mockResolvedValueOnce({
        data: { ...ACTIVE_PROJECT, display_name: "我的未保存新名称", version: 5 },
      });
    render(<MaintenanceProjectMasterPage />);
    await screen.findByText("XM-001");

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("项目名称"), {
      target: { value: "我的未保存新名称" },
    });
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "名称更正" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    expect(await screen.findByText(/项目主档已被他人修改/)).toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: "刷新项目列表并保留草稿",
    })).toBeInTheDocument();
    expect(screen.getByLabelText("项目名称")).toHaveValue("我的未保存新名称");
    expect(screen.getByLabelText("操作原因")).toHaveValue("名称更正");

    fireEvent.click(screen.getByRole("button", {
      name: "刷新项目列表并保留草稿",
    }));
    await waitFor(() => expect(listMaintenanceProjects).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("项目名称")).toHaveValue("我的未保存新名称");
    expect(screen.getByLabelText("项目经理标识")).toHaveValue("manager-2");
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(updateMaintenanceProject).toHaveBeenLastCalledWith(
      "project-1",
      { version: 4, display_name: "我的未保存新名称", reason: "名称更正" },
    ));
  });

  it("归档必须经过二次确认并填写原因", async () => {
    enableProjectManagement();
    archiveMaintenanceProject.mockResolvedValue({
      data: { ...ACTIVE_PROJECT, is_active: false, version: 4 },
    });
    render(<MaintenanceProjectMasterPage />);
    await screen.findByText("XM-001");

    fireEvent.click(screen.getByRole("button", { name: "归档" }));
    expect(screen.getByText("二次确认归档项目")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认归档" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("归档原因"), {
      target: { value: "项目已结束" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认归档" }));

    await waitFor(() => expect(archiveMaintenanceProject).toHaveBeenCalledWith(
      "project-1",
      { version: 3, reason: "项目已结束" },
    ));
  });

  it("归档冲突后可刷新最新版本并保留原因再提交", async () => {
    enableProjectManagement();
    archiveMaintenanceProject
      .mockRejectedValueOnce({
        response: { status: 409, data: { detail: "项目已更新，请刷新" } },
      })
      .mockResolvedValueOnce({
        data: { ...ACTIVE_PROJECT, is_active: false, version: 5 },
      });
    getMaintenanceProject.mockResolvedValue({
      data: { project: { ...ACTIVE_PROJECT, version: 4 } },
    });
    render(<MaintenanceProjectMasterPage />);
    await screen.findByText("XM-001");

    fireEvent.click(screen.getByRole("button", { name: "归档" }));
    fireEvent.change(screen.getByLabelText("归档原因"), {
      target: { value: "项目已经结束" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认归档" }));

    expect(await screen.findByText("项目已更新，请刷新")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", {
      name: "刷新最新版本并保留原因",
    }));
    await waitFor(() => expect(getMaintenanceProject).toHaveBeenCalledWith("project-1"));
    expect(screen.getByLabelText("归档原因")).toHaveValue("项目已经结束");

    const retryButton = await screen.findByRole("button", { name: "确认归档" });
    await waitFor(() => expect(retryButton).toBeEnabled());
    fireEvent.click(retryButton);
    await waitFor(() => expect(archiveMaintenanceProject).toHaveBeenLastCalledWith(
      "project-1",
      { version: 4, reason: "项目已经结束" },
    ));
  });

  it("归档冲突刷新发现已被他人归档时结束操作，不会反向恢复", async () => {
    enableProjectManagement();
    archiveMaintenanceProject.mockRejectedValueOnce({
      response: { status: 409, data: { detail: "项目已更新，请刷新" } },
    });
    getMaintenanceProject.mockResolvedValue({
      data: { project: { ...ACTIVE_PROJECT, is_active: false, version: 4 } },
    });
    render(<MaintenanceProjectMasterPage />);
    await screen.findByText("XM-001");

    fireEvent.click(screen.getByRole("button", { name: "归档" }));
    fireEvent.change(screen.getByLabelText("归档原因"), {
      target: { value: "项目已经结束" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认归档" }));
    fireEvent.click(await screen.findByRole("button", {
      name: "刷新最新版本并保留原因",
    }));

    await waitFor(() => expect(getMaintenanceProject).toHaveBeenCalledWith("project-1"));
    await waitFor(() => expect(screen.queryByRole("button", { name: "确认归档" })).toBeNull());
    expect(archiveMaintenanceProject).toHaveBeenCalledTimes(1);
    expect(restoreMaintenanceProject).not.toHaveBeenCalled();
  });

  it("恢复冲突刷新发现已被他人恢复时结束操作，不会反向归档", async () => {
    enableProjectManagement();
    listMaintenanceProjects.mockReturnValue(directory([ARCHIVED_PROJECT]));
    restoreMaintenanceProject.mockRejectedValueOnce({
      response: { status: 409, data: { detail: "项目已更新，请刷新" } },
    });
    getMaintenanceProject.mockResolvedValue({
      data: { project: { ...ARCHIVED_PROJECT, is_active: true, version: 8 } },
    });
    render(<MaintenanceProjectMasterPage />);
    await screen.findByText("XM-ARCHIVED");

    fireEvent.click(screen.getByRole("button", { name: "恢复" }));
    fireEvent.change(screen.getByLabelText("恢复原因"), {
      target: { value: "项目重新启动" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认恢复" }));
    fireEvent.click(await screen.findByRole("button", {
      name: "刷新最新版本并保留原因",
    }));

    await waitFor(() => expect(getMaintenanceProject).toHaveBeenCalledWith("project-2"));
    await waitFor(() => expect(screen.queryByRole("button", { name: "确认恢复" })).toBeNull());
    expect(restoreMaintenanceProject).toHaveBeenCalledTimes(1);
    expect(archiveMaintenanceProject).not.toHaveBeenCalled();
  });

  it("恢复也必须经过二次确认并使用归档主档的当前版本", async () => {
    enableProjectManagement();
    listMaintenanceProjects.mockReturnValue(directory([ARCHIVED_PROJECT]));
    restoreMaintenanceProject.mockResolvedValue({
      data: { ...ARCHIVED_PROJECT, is_active: true, version: 8 },
    });
    render(<MaintenanceProjectMasterPage />);
    await screen.findByText("XM-ARCHIVED");

    fireEvent.click(screen.getByRole("button", { name: "恢复" }));
    expect(screen.getByText("二次确认恢复项目")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认恢复" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("恢复原因"), {
      target: { value: "项目重新启动" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认恢复" }));

    await waitFor(() => expect(restoreMaintenanceProject).toHaveBeenCalledWith(
      "project-2",
      { version: 7, reason: "项目重新启动" },
    ));
  });
});
