import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getBoardProjectExportOptions = vi.fn();
const downloadBoardProjectsExport = vi.fn();
const saveBlob = vi.fn();

vi.mock("../../../api/maintenanceBossBoard", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceBossBoard",
  );
  return {
    ...actual,
    getBoardProjectExportOptions: (...args: unknown[]) => getBoardProjectExportOptions(...args),
    downloadBoardProjectsExport: (...args: unknown[]) => downloadBoardProjectsExport(...args),
  };
});

vi.mock("../../../api/maintenanceWorkbooks", async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    "../../../api/maintenanceWorkbooks",
  );
  return { ...actual, saveBlob: (...args: unknown[]) => saveBlob(...args) };
});

import MaintenanceProjectExportButton from "../MaintenanceProjectExportButton";

const options = {
  data: {
    fields: [
      { key: "project_name", label: "项目名称", group: "项目基础", default_selected: true },
      { key: "period_from", label: "维保起始时间", group: "维保期限", default_selected: true },
      { key: "period_to", label: "维保终止时间", group: "维保期限", default_selected: true },
      { key: "contract_nos", label: "销售单号", group: "合同与回款", default_selected: true },
      { key: "contract_amount_inc_tax", label: "合同总额", group: "合同与回款", default_selected: true },
      { key: "collection_received_inc_tax", label: "累计已回款", group: "合同与回款", default_selected: true },
    ],
    default_fields: [
      "project_name",
      "period_from",
      "period_to",
      "contract_nos",
      "contract_amount_inc_tax",
      "collection_received_inc_tax",
    ],
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  getBoardProjectExportOptions.mockResolvedValue(options);
  downloadBoardProjectsExport.mockResolvedValue({
    blob: new Blob(["xlsx"]),
    filename: "维保项目清单-20260827.xlsx",
  });
});

afterEach(cleanup);

function renderButton() {
  return render(
    <MaintenanceProjectExportButton
      filters={{
        lifecycle: "ended",
        card_status: "alert",
        sort: "cost_ratio",
        q: "XSDD-001",
      }}
    />,
  );
}

describe("维保项目清单导出", () => {
  it("只展示服务端权限白名单，并默认勾选业务要求的六项", async () => {
    renderButton();
    fireEvent.click(screen.getByRole("button", { name: /导出项目清单/ }));
    const dialog = await screen.findByRole("dialog");

    expect(within(dialog).getByText("项目基础")).toBeInTheDocument();
    expect(within(dialog).getByText("维保期限")).toBeInTheDocument();
    expect(within(dialog).getByText("合同与回款")).toBeInTheDocument();
    expect(within(dialog).queryByText("数据库内部ID")).toBeNull();
    for (const label of [
      "项目名称", "维保起始时间", "维保终止时间", "销售单号", "合同总额", "累计已回款",
    ]) {
      expect(within(dialog).getByRole("checkbox", { name: label })).toBeChecked();
    }
  });

  it("支持取消全选/多选，并把当前页面筛选用于全量导出", async () => {
    renderButton();
    fireEvent.click(screen.getByRole("button", { name: /导出项目清单/ }));
    const dialog = await screen.findByRole("dialog");

    fireEvent.click(within(dialog).getByRole("button", { name: "取消全选" }));
    expect(within(dialog).getByRole("button", { name: /下载 Excel/ })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "项目名称" }));
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "维保终止时间" }));
    fireEvent.click(within(dialog).getByRole("button", { name: /下载 Excel（2 项）/ }));

    await waitFor(() => expect(downloadBoardProjectsExport).toHaveBeenCalledWith({
      fields: ["project_name", "period_to"],
      lifecycle: "ended",
      card_status: "alert",
      sort: "cost_ratio",
      q: "XSDD-001",
    }));
    expect(saveBlob).toHaveBeenCalledWith(
      expect.any(Blob),
      "维保项目清单-20260827.xlsx",
    );
  });

  it("记住上次选择，但会剔除服务端不再允许的字段", async () => {
    localStorage.setItem(
      "maintenance_project_export_fields_v1",
      JSON.stringify(["period_to", "database_secret"]),
    );
    renderButton();
    fireEvent.click(screen.getByRole("button", { name: /导出项目清单/ }));
    const dialog = await screen.findByRole("dialog");

    expect(within(dialog).getByRole("checkbox", { name: "维保终止时间" })).toBeChecked();
    expect(within(dialog).getByRole("checkbox", { name: "项目名称" })).not.toBeChecked();
    expect(within(dialog).queryByText("database_secret")).toBeNull();
    expect(JSON.parse(localStorage.getItem("maintenance_project_export_fields_v1") || "[]"))
      .toEqual(["period_to"]);
  });

  it("可一键恢复服务端默认字段", async () => {
    localStorage.setItem(
      "maintenance_project_export_fields_v1",
      JSON.stringify(["project_name"]),
    );
    renderButton();
    fireEvent.click(screen.getByRole("button", { name: /导出项目清单/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "恢复默认" }));

    expect(within(dialog).getByText("已选 6 / 6 项")).toBeInTheDocument();
    expect(within(dialog).getByRole("checkbox", { name: "累计已回款" })).toBeChecked();
  });
});
