import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanupAntd, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import WorkbookExportButton from "../WorkbookExportButton";
import type { WorkbookSchema } from "../../../api/workbooks";

const schema: WorkbookSchema = {
  name: "demo", version: 1, instructions: [], sheets: [{ key: "items", title: "示例明细",
    allow_insert: true, allow_delete: true, custom_columns: false, columns: [
      { key: "entity_id", header: "身份", type: "text", edit: "locked", hint: "系统身份" },
      { key: "pn", header: "PN", type: "code", edit: "locked", hint: "文本编码" },
      { key: "qty", header: "数量", type: "int", edit: "required", hint: "整数" },
      { key: "note", header: "备注", type: "text", edit: "optional", hint: "自由填写" },
    ] }],
};

afterEach(async () => { await cleanupAntd(); vi.restoreAllMocks(); });

describe("WorkbookExportButton", () => {
  it("按 schema 显示列，身份与锁定 code 始终必带，选择后才导出", async () => {
    const fetchSchema = vi.fn().mockResolvedValue(schema);
    const onExport = vi.fn().mockResolvedValue(undefined);
    render(<WorkbookExportButton schemaName="demo" fetchSchema={fetchSchema} onExport={onExport} />);
    fireEvent.click(screen.getByRole("button", { name: "导出工作簿" }));
    expect(await screen.findByRole("checkbox", { name: "身份（必带）" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "PN（必带）" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "PN（必带）" })).toBeChecked();
    expect(fetchSchema).toHaveBeenCalledWith("demo");
    expect(onExport).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox", { name: "备注" }));
    fireEvent.click(screen.getByRole("button", { name: "下载 Excel" }));
    await waitFor(() => expect(onExport).toHaveBeenCalledWith({ items: ["entity_id", "pn", "qty"] }));
  });

  it("每次打开重新加载当前权限下的列；加载失败保留重试入口", async () => {
    const fetchSchema = vi.fn().mockRejectedValueOnce(new Error("目录暂不可用")).mockResolvedValue(schema);
    render(<WorkbookExportButton schemaName="demo" fetchSchema={fetchSchema} onExport={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "导出工作簿" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("目录暂不可用");
    expect(screen.getByRole("button", { name: "下载 Excel" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByRole("checkbox", { name: "备注" })).toBeChecked();
    expect(fetchSchema).toHaveBeenCalledTimes(2);
  });

  it("导出失败保留列选择，模板类型切换后忽略旧 schema 响应", async () => {
    let resolveOld!: (value: WorkbookSchema) => void;
    const oldSchema = new Promise<WorkbookSchema>((resolve) => { resolveOld = resolve; });
    const fetchSchema = vi.fn().mockReturnValueOnce(oldSchema).mockResolvedValue({ ...schema, name: "other" });
    const onExport = vi.fn().mockRejectedValue(new Error("导出失败"));
    const { rerender } = render(<WorkbookExportButton schemaName="demo" fetchSchema={fetchSchema} onExport={onExport} />);
    fireEvent.click(screen.getByRole("button", { name: "导出工作簿" }));
    rerender(<WorkbookExportButton schemaName="other" fetchSchema={fetchSchema} onExport={onExport} />);
    resolveOld(schema);
    fireEvent.click(screen.getByRole("button", { name: "导出工作簿" }));
    await screen.findByRole("checkbox", { name: "备注" });
    fireEvent.click(screen.getByRole("button", { name: "下载 Excel" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("导出失败");
    expect(screen.getByRole("checkbox", { name: "备注" })).toBeChecked();
  });
});
