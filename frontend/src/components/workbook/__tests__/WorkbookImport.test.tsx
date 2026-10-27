import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import WorkbookImport from "../WorkbookImport";
import WorkbookPreview, { ROW_COLORS } from "../WorkbookPreview";
import type { WorkbookPlan, WorkbookPlanRow, WorkbookPreviewResult, WorkbookRowStatus } from "../../../api/workbooks";
import { tokens } from "../../../tokens";

const mocks = vi.hoisted(() => ({ previewWorkbook: vi.fn(), applyWorkbook: vi.fn() }));
vi.mock("../../../api/workbooks", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../../api/workbooks")>(), ...mocks,
}));

function row(key: string, status: WorkbookRowStatus, patch: Partial<WorkbookPlanRow> = {}): WorkbookPlanRow {
  return { key, sheet: "示例明细", row_number: 2, entity_id: key, status,
    changes: [{ key: "qty", header: "数量", before: 2, after: 4 }], errors: [], warnings: [], ...patch };
}

function plan(rows: WorkbookPlanRow[]): WorkbookPlan {
  return { rows, counts: { added: 0, modified: 0, unchanged: 0, deleted: 0, conflict: 0,
    ...Object.fromEntries(["added", "modified", "unchanged", "deleted", "conflict"].map((status) => [status, rows.filter((item) => item.status === status).length])) },
  errors: [], warnings: [] };
}

function preview(rows: WorkbookPlanRow[]): WorkbookPreviewResult {
  return { operation_id: "op-1", plan_digest: "digest-1", replayed: false, plan: plan(rows) };
}

function upload(container: HTMLElement, name = "demo.xlsx") {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
  fireEvent.change(input, { target: { files: [new File(["xlsx"], name)] } });
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.previewWorkbook.mockResolvedValue(preview([row("first", "modified")]));
  mocks.applyWorkbook.mockResolvedValue({ operation_id: "op-1", replayed: false, response: { updated: 1 } });
});
afterEach(cleanupAntd);

describe("WorkbookPreview", () => {
  it("五类行按约定着色，逐格显示前后值，错误格显示红框", () => {
    const rows = (["added", "modified", "unchanged", "deleted", "conflict"] as const)
      .map((status) => row(status, status, status === "modified" ? {
        errors: [{ column: "qty", message: "数量必须为整数", row_number: 2, sheet: "示例明细" }],
      } : {}));
    const { container } = render(<WorkbookPreview plan={plan(rows)} confirmDeletes={[]} onConfirmDeletes={vi.fn()} />);
    for (const status of ["added", "modified", "unchanged", "deleted", "conflict"] as const) {
      expect(container.querySelector(`tr[data-status="${status}"]`)).toHaveStyle({ backgroundColor: ROW_COLORS[status] });
    }
    const modified = container.querySelector('tr[data-status="modified"]') as HTMLElement;
    expect(within(modified).getByLabelText("修改前")).toHaveTextContent("2");
    expect(within(modified).getByLabelText("修改后")).toHaveTextContent("4");
    expect(modified.querySelector('[data-field="qty"]')).toHaveAttribute("data-invalid", "true");
    expect(modified.querySelector('[data-field="qty"]')).toHaveStyle({ border: `2px solid ${tokens.color.danger}` });
  });

  it("只有删除候选时也显示导出基线和自定义值，便于确认目标", () => {
    const deleted = row("old", "deleted", { changes: [], delete_key: "items:old",
      values: { pn: "0231", qty: 0 }, custom_fields: { 包装: "原装" } });
    render(<WorkbookPreview plan={plan([deleted])} confirmDeletes={[]} onConfirmDeletes={vi.fn()} />);
    expect(screen.getByText("0231")).toBeInTheDocument();
    expect(screen.getByText("原装")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "确认删除 示例明细 old" })).not.toBeChecked();
  });

  it("不同工作表同名 key 的表头、变更值和错误按工作表隔离", () => {
    const qty = row("qty-row", "modified", { sheet: "数量表",
      changes: [{ key: "value", header: "数量", before: 2, after: 4 }] });
    const money = row("money-row", "modified", { sheet: "金额表",
      changes: [{ key: "value", header: "金额", before: "10.00", after: "12.00" }],
      errors: [{ column: "value", message: "金额无效" }] });
    const { container } = render(<WorkbookPreview plan={plan([qty, money])} confirmDeletes={[]} onConfirmDeletes={vi.fn()} />);
    const headers = screen.getAllByRole("columnheader");
    const qtyIndex = headers.findIndex((header) => header.textContent === "数量表 · 数量");
    const moneyIndex = headers.findIndex((header) => header.textContent === "金额表 · 金额");
    expect(qtyIndex).toBeGreaterThan(-1);
    expect(moneyIndex).toBeGreaterThan(-1);
    const qtyCells = within(container.querySelector('[data-row-key="qty-row"]') as HTMLElement).getAllByRole("cell");
    const moneyCells = within(container.querySelector('[data-row-key="money-row"]') as HTMLElement).getAllByRole("cell");
    expect(qtyCells[qtyIndex]).toHaveTextContent("2 → 4");
    expect(qtyCells[moneyIndex]).toHaveTextContent("—");
    expect(moneyCells[qtyIndex]).toHaveTextContent("—");
    expect(moneyCells[moneyIndex]).toHaveTextContent("10.00 → 12.00");
    expect(qtyCells[qtyIndex].querySelector('[data-invalid="true"]')).toBeNull();
    expect(moneyCells[moneyIndex].querySelector('[data-invalid="true"]')).not.toBeNull();
  });
});

describe("WorkbookImport", () => {
  it("先预览，再仅提交逐条勾选的删除；回执成功后通知业务刷新", async () => {
    mocks.previewWorkbook.mockResolvedValue(preview([
      row("first", "modified"), row("delete-a", "deleted", { delete_key: "items:a" }), row("delete-b", "deleted", { delete_key: "items:b" }),
    ]));
    const onApplied = vi.fn();
    const { container } = render(<WorkbookImport schemaName="demo" onApplied={onApplied} />);
    upload(container);
    await screen.findByText("变更预览");
    expect(mocks.previewWorkbook).toHaveBeenCalledWith("demo", expect.any(File));
    expect(mocks.applyWorkbook).not.toHaveBeenCalled();
    expect(screen.getByRole("checkbox", { name: "确认删除 示例明细 delete-a" })).not.toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: "确认删除 示例明细 delete-a" }));
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));
    await screen.findByText("工作簿已应用");
    expect(mocks.applyWorkbook).toHaveBeenCalledWith("op-1", "digest-1", ["items:a"]);
    expect(onApplied).toHaveBeenCalledWith({ updated: 1 });
    expect(screen.getByLabelText("工作簿回执")).toHaveTextContent('"updated": 1');
  });

  it("取消某行删除确认后不会发送该行；未确认删除可以保留", async () => {
    mocks.previewWorkbook.mockResolvedValue(preview([row("a", "deleted", { delete_key: "items:a" })]));
    const { container } = render(<WorkbookImport schemaName="demo" />);
    upload(container);
    const checkbox = await screen.findByRole("checkbox", { name: "确认删除 示例明细 a" });
    fireEvent.click(checkbox); fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));
    await waitFor(() => expect(mocks.applyWorkbook).toHaveBeenCalledWith("op-1", "digest-1", []));
  });

  it("有错误或冲突时禁止应用并显示定位原因", async () => {
    const invalid = row("bad", "modified", { errors: [{ sheet: "明细", row_number: 7, column: "PN", message: "请将该列设为文本后重新粘贴" }] });
    mocks.previewWorkbook.mockResolvedValue(preview([invalid, row("conflict", "conflict")]));
    const { container } = render(<WorkbookImport schemaName="demo" />);
    upload(container);
    expect(await screen.findByRole("button", { name: "确认应用" })).toBeDisabled();
    expect(screen.getByText("明细 · 第 7 行 · PN · 请将该列设为文本后重新粘贴")).toBeInTheDocument();
    expect(screen.getByText("存在版本冲突，请重新导出最新工作簿后修改。")).toBeInTheDocument();
    expect(mocks.applyWorkbook).not.toHaveBeenCalled();
  });

  it("重复上传直接显示首次回执，不再次 apply", async () => {
    mocks.previewWorkbook.mockResolvedValue({ ...preview([]), replayed: true, response: { receipt: "首次结果" } });
    const { container } = render(<WorkbookImport schemaName="demo" />);
    upload(container);
    expect(await screen.findByText("该文件已应用过，显示首次结果")).toBeInTheDocument();
    expect(screen.getByLabelText("工作簿回执")).toHaveTextContent("首次结果");
    expect(screen.queryByRole("button", { name: "确认应用" })).toBeNull();
    expect(mocks.applyWorkbook).not.toHaveBeenCalled();
  });

  it("已失败的操作重新上传时保留预览并禁止再次应用", async () => {
    mocks.previewWorkbook.mockResolvedValue({ ...preview([row("bad", "modified")]), status: "failed",
      response: { error: "应用失败，整份业务更改已回滚" } });
    const { container } = render(<WorkbookImport schemaName="demo" />);
    upload(container);
    expect(await screen.findByRole("alert")).toHaveTextContent("该次应用已失败，请修正文件后重新上传");
    expect(screen.getByRole("button", { name: "确认应用" })).toBeDisabled();
    expect(mocks.applyWorkbook).not.toHaveBeenCalled();
  });

  it("应用接口重放也显示首次结果，刷新失败仍保留成功回执", async () => {
    mocks.applyWorkbook.mockResolvedValue({ operation_id: "op-1", replayed: true, response: { original: true } });
    const onApplied = vi.fn().mockRejectedValue(new Error("刷新失败"));
    const { container } = render(<WorkbookImport schemaName="demo" onApplied={onApplied} />);
    upload(container);
    fireEvent.click(await screen.findByRole("button", { name: "确认应用" }));
    await screen.findByText("工作簿已应用，但页面刷新失败，请手动刷新。");
    expect(screen.getByText("该文件已应用过，显示首次结果")).toBeInTheDocument();
    expect(screen.getByLabelText("工作簿回执")).toHaveTextContent('"original": true');
  });

  it("应用失败保留预览与删除选择并显示服务端错误", async () => {
    mocks.previewWorkbook.mockResolvedValue(preview([row("a", "deleted", { delete_key: "items:a" })]));
    mocks.applyWorkbook.mockRejectedValue({ response: { data: { detail: "版本已变化，请重新预览" } } });
    const { container } = render(<WorkbookImport schemaName="demo" />);
    upload(container);
    fireEvent.click(await screen.findByRole("checkbox", { name: "确认删除 示例明细 a" }));
    fireEvent.click(screen.getByRole("button", { name: "确认应用" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("版本已变化，请重新预览");
    expect(screen.getByRole("checkbox", { name: "确认删除 示例明细 a" })).toBeChecked();
  });

  it("模板类型切换后丢弃旧预览响应，不能误用旧 operation", async () => {
    let resolvePreview!: (value: WorkbookPreviewResult) => void;
    mocks.previewWorkbook.mockReturnValue(new Promise<WorkbookPreviewResult>((resolve) => { resolvePreview = resolve; }));
    const { container, rerender } = render(<WorkbookImport schemaName="demo" />);
    upload(container);
    await waitFor(() => expect(mocks.previewWorkbook).toHaveBeenCalledOnce());
    rerender(<WorkbookImport schemaName="other" />);
    resolvePreview(preview([row("old", "modified")]));
    await waitFor(() => expect(screen.queryByText("变更预览")).toBeNull());
    expect(mocks.applyWorkbook).not.toHaveBeenCalled();
  });

  it("限制只上传 xlsx，预览请求失败显示错误", async () => {
    const { container } = render(<WorkbookImport schemaName="demo" />);
    upload(container, "demo.xls");
    expect(await screen.findByRole("alert")).toHaveTextContent("请上传 .xlsx 工作簿");
    expect(mocks.previewWorkbook).not.toHaveBeenCalled();
    mocks.previewWorkbook.mockRejectedValue({ response: { data: { detail: { message: "请使用系统导出的模板" } } } });
    upload(container);
    expect(await screen.findByRole("alert")).toHaveTextContent("请使用系统导出的模板");
  });

  it("超过 10 MB 不发送预览，换会话后的迟到响应不展示", async () => {
    const { container, rerender } = render(<WorkbookImport schemaName="demo" />);
    const file = new File(["xlsx"], "large.xlsx");
    Object.defineProperty(file, "size", { value: 10 * 1024 * 1024 + 1 });
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } });
    expect(await screen.findByRole("alert")).toHaveTextContent("工作簿不能超过 10 MB");
    expect(mocks.previewWorkbook).not.toHaveBeenCalled();
    let resolvePreview!: (value: WorkbookPreviewResult) => void;
    mocks.previewWorkbook.mockReturnValue(new Promise<WorkbookPreviewResult>((resolve) => { resolvePreview = resolve; }));
    upload(container);
    await waitFor(() => expect(mocks.previewWorkbook).toHaveBeenCalledOnce());
    const oldToken = localStorage.getItem("token");
    localStorage.setItem("token", "new-workbook-session");
    rerender(<WorkbookImport schemaName="demo" />);
    resolvePreview(preview([row("old-session", "modified")]));
    await waitFor(() => expect(screen.queryByText("变更预览")).toBeNull());
    if (oldToken == null) localStorage.removeItem("token"); else localStorage.setItem("token", oldToken);
  });
});


it("支持业务预览回调和草稿提示，重放成功同样通知业务跳转", async () => {
  const previewFile = vi.fn().mockResolvedValue(preview([row("a", "modified")]));
  const onApplied = vi.fn();
  const { container } = render(<WorkbookImport schemaName="demo" previewFile={previewFile}
    buttonLabel="上传报价表" successMessage="已保存为草稿，请在页面确认后回复" onApplied={onApplied} />);
  expect(screen.getByRole("button", { name: "上传报价表" })).toBeInTheDocument();
  upload(container);
  fireEvent.click(await screen.findByRole("button", { name: "确认应用" }));
  expect(await screen.findByText("已保存为草稿，请在页面确认后回复")).toBeInTheDocument();
  expect(previewFile).toHaveBeenCalledWith(expect.any(File));
  expect(mocks.previewWorkbook).not.toHaveBeenCalled();
  expect(onApplied).toHaveBeenCalledWith({ updated: 1 });
  previewFile.mockResolvedValue({ ...preview([]), replayed: true, response: { inquiry_id: 7 } });
  upload(container);
  await waitFor(() => expect(onApplied).toHaveBeenCalledWith({ inquiry_id: 7 }));
  expect(screen.getByText("该文件已应用过，显示首次结果")).toBeInTheDocument();
});

it("应用后业务变更失败返回的新计划逐格显示错误并禁止重试应用", async () => {
  const bad = row("first", "modified", { errors: [{ sheet: "示例明细", row_number: 2, row_key: "first", column: "qty", message: "数量已被其他人修改" }] });
  mocks.applyWorkbook.mockRejectedValue({ response: { data: { detail: {
    message: "业务数据已变化，请修正后重新上传", operation_id: "op-1", plan_digest: "digest-2", status: "failed", plan: plan([bad]),
    response: { row_errors: bad.errors },
  } } } });
  const { container } = render(<WorkbookImport schemaName="demo" />);
  upload(container);
  fireEvent.click(await screen.findByRole("button", { name: "确认应用" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("业务数据已变化，请修正后重新上传");
  expect(screen.getByRole("button", { name: "确认应用" })).toBeDisabled();
  expect(container.querySelector('[data-field="qty"]')).toHaveAttribute("data-invalid", "true");
  expect(screen.getByText("示例明细 · 第 2 行 · qty · 数量已被其他人修改")).toBeInTheDocument();
});
