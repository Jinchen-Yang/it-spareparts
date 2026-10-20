import { act, cleanup, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Modal } from "antd";

const { saveBlob } = vi.hoisted(() => ({ saveBlob: vi.fn() }));

vi.mock("../../../api/maintenanceWorkbooks", async () => {
  const actual = await vi.importActual<typeof import("../../../api/maintenanceWorkbooks")>(
    "../../../api/maintenanceWorkbooks",
  );
  return { ...actual, saveBlob };
});

import WorkbookRoundTrip from "../WorkbookRoundTrip";

const makeFile = () => new File(["xlsx"], "回填.xlsx", { type: "application/vnd.ms-excel" });

function uploadFile(container: HTMLElement, file: File) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
  return input;
}

describe("WorkbookRoundTrip", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });
  afterEach(async () => {
    cleanup();
    Modal.destroyAll();

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("hides the upload button without the upload permission", () => {
    render(
      <WorkbookRoundTrip
        title="总表"
        filename="a.xlsx"
        canUpload={false}
        onDownload={vi.fn()}
        onApply={vi.fn()}
      />,
    );
    expect(screen.getByText("下载总表")).toBeInTheDocument();
    expect(screen.queryByText("上传覆盖")).not.toBeInTheDocument();
  });

  it("applies directly when no onValidate is given (legacy single-phase)", async () => {
    const onApply = vi.fn().mockResolvedValue({ cost_refills: 2 });
    const { container } = render(
      <WorkbookRoundTrip
        title="备件表"
        filename="a.xlsx"
        canUpload
        onDownload={vi.fn()}
        onApply={onApply}
      />,
    );
    const file = makeFile();
    const firstInput = uploadFile(container, file);
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/已覆盖：补价 2 行/)).toBeInTheDocument();
    await waitFor(() =>
      expect(container.querySelector('input[type="file"]')).not.toBe(firstInput));
    uploadFile(container, file);
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(2));
  });

  it("does not claim the upload failed or the stale UI refreshed after commit succeeds", async () => {
    const onApply = vi.fn().mockResolvedValue({ contract_updates: 1 });
    const onAfterApply = vi.fn().mockResolvedValue(false);
    const { container } = render(
      <WorkbookRoundTrip
        title="总表"
        filename="a.xlsx"
        canUpload
        onDownload={vi.fn()}
        onApply={onApply}
        onAfterApply={onAfterApply}
      />,
    );

    uploadFile(container, makeFile());

    await waitFor(() => expect(onAfterApply).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/数据已写入，但页面刷新失败/)).toBeInTheDocument();
    expect(screen.queryByText(/已覆盖并刷新/)).toBeNull();
    expect(screen.queryByText("上传失败")).toBeNull();
  });

  it("项目备注更新回执显示具体数量", async () => {
    const { container } = render(
      <WorkbookRoundTrip
        title="总表" filename="a.xlsx" canUpload onDownload={vi.fn()}
        onApply={vi.fn().mockResolvedValue({ project_note_updates: 1 })}
      />,
    );
    uploadFile(container, makeFile());
    expect(await screen.findByText("已覆盖：项目备注更新 1 项")).toBeVisible();
  });

  it("two-phase: shows the void preview and applies only after confirm", async () => {
    const onValidate = vi.fn().mockResolvedValue({
      expense_updates: 1,
      will_void_rows: [{ sheet: "04_报销订单", order_no: "BXD-1", reason: "上传文件缺行" }],
      will_reassign_orders: [{
        source_order_id: "raw-wbdd-1",
        order_no: "WBDD-1",
        from_project_name: "旧项目",
        to_project_id: "new-project",
      }],
    });
    const onApply = vi.fn().mockResolvedValue({ expense_updates: 1 });
    const { container } = render(
      <WorkbookRoundTrip
        title="总表"
        filename="a.xlsx"
        canUpload
        onDownload={vi.fn()}
        onValidate={onValidate}
        onApply={onApply}
      />,
    );
    uploadFile(container, makeFile());

    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(1));
    expect(onApply).not.toHaveBeenCalled();
    expect(await screen.findByText(/以下 1 行将被作废/)).toBeInTheDocument();
    expect(screen.getByText(/BXD-1/)).toBeInTheDocument();
    expect(screen.getByText(/以下 1 张 WBDD 将按本次项目内人工认证更正归属/)).toBeInTheDocument();
    expect(screen.getByText(/WBDD-1 · 旧项目 → 当前项目/)).toBeInTheDocument();

    fireEvent.click(screen.getByText("确认回传"));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
  });

  it("合并预览列明保留单、来源明细及单头取舍，取消后只提交新预检的凭据", async () => {
    const merge = {
      target_issue_no: "ISS-FINAL",
      survivor_issue_id: "issue-older",
      sources: [
        { issue_id: "issue-older", issue_no: "ISS-A", line_count: 2, rows: [12] },
        { issue_id: "issue-newer", issue_no: "ISS-B", line_count: 3, rows: [30, 31] },
      ],
      total_line_count: 5,
      rows: [12, 30, 31],
      header_conflicts: [{
        field: "issue_date", survivor_value: "2026-08-01",
        discarded: [{ issue_id: "issue-newer", issue_no: "ISS-B", value: "2026-08-02" }],
        final_value: "2026-08-03",
      }],
    };
    const onValidate = vi.fn()
      .mockResolvedValueOnce({ site_issue_merges: [merge], site_issue_merge_token: "first-preview" })
      .mockResolvedValueOnce({ site_issue_merges: [merge], site_issue_merge_token: "second-preview" });
    const onApply = vi.fn().mockResolvedValue({ site_issue_merge_count: 1 });
    const { container } = render(
      <WorkbookRoundTrip title="总表" filename="a.xlsx" canUpload onDownload={vi.fn()}
        onValidate={onValidate} onApply={onApply} />,
    );
    const file = makeFile();
    const initialInput = uploadFile(container, file);
    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("领用单合并 1 组")).toBeVisible());
    expect(dialog.getByText("合并目标：ISS-FINAL")).toBeVisible();
    expect(dialog.getByText("保留单：ISS-A（issue-older）")).toBeVisible();
    expect(dialog.getByText("来源单：ISS-A（issue-older）· 明细 2 行 · Excel 第 12 行")).toBeVisible();
    expect(dialog.getByText("来源单：ISS-B（issue-newer）· 明细 3 行 · Excel 第 30、31 行")).toBeVisible();
    expect(dialog.getByText("合并后现有明细共 5 行 · Excel 第 12、30、31 行")).toBeVisible();
    expect(dialog.getByText("领用日期：保留单原值 2026-08-01；最终值 2026-08-03")).toBeVisible();
    expect(dialog.getByText("舍弃来源值：ISS-B（issue-newer）· 2026-08-02")).toBeVisible();
    expect(onApply).not.toHaveBeenCalled();
    fireEvent.click(dialog.getByRole("button", { name: /取\s*消/ }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBe(initialInput));
    uploadFile(container, file);
    fireEvent.click(await screen.findByRole("button", { name: "确认回传" }));
    await waitFor(() => expect(onApply).toHaveBeenCalledWith(file, { siteIssueMergeToken: "second-preview" }));
    expect(onApply).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("已覆盖：领用单合并 1 组")).toBeVisible();
  });

  it("合并计划过期时保留草稿并重新预检，不提供强制接管", async () => {
    const onValidate = vi.fn()
      .mockResolvedValueOnce({ site_issue_merge_count: 1, site_issue_merge_token: "stale-preview" })
      .mockResolvedValueOnce({ site_issue_merge_count: 1, site_issue_merge_token: "fresh-preview" });
    const onApply = vi.fn()
      .mockRejectedValueOnce({ response: { status: 409, data: { detail: {
        code: "stale_site_issue_merge", message: "领用单合并计划已变化，请重新校验后确认",
      } } } })
      .mockResolvedValueOnce({ site_issue_merge_count: 1 });
    const { container } = render(
      <WorkbookRoundTrip title="总表" filename="a.xlsx" canUpload onDownload={vi.fn()}
        onValidate={onValidate} onApply={onApply} />,
    );
    const file = makeFile();
    uploadFile(container, file);
    fireEvent.click(await screen.findByRole("button", { name: "确认回传" }));
    expect(await screen.findByText("领用单合并计划已变化，请重新校验后确认")).toBeVisible();
    expect(screen.queryByRole("button", { name: /强制接管/ })).toBeNull();
    const retry = await screen.findByRole("button", { name: "重试原文件" });
    await waitFor(() => expect(retry).toBeEnabled());
    fireEvent.click(retry);
    fireEvent.click(await screen.findByRole("button", { name: "确认回传" }));
    await waitFor(() => expect(onApply).toHaveBeenLastCalledWith(file, { siteIssueMergeToken: "fresh-preview" }));
    expect(onValidate).toHaveBeenCalledTimes(2);
  });

  it("已确认合并后强制接管仍携带同一次预览凭据", async () => {
    const onValidate = vi.fn().mockResolvedValue({ site_issue_merge_token: "merge-preview" });
    const onApply = vi.fn()
      .mockRejectedValueOnce({ response: { status: 409, data: { detail: {
        code: "row_conflicts", message: "部分行已被更新",
        conflicts: [{ sheet: "06_领用返还", row: "ISS-A", entity_id: "line-a", field: "领用数量", base: "1", old: "2", new: "3" }],
      } } } })
      .mockResolvedValueOnce({ site_issue_merge_count: 1 });
    const { container } = render(
      <WorkbookRoundTrip title="总表" filename="a.xlsx" canUpload onDownload={vi.fn()}
        onValidate={onValidate} onApply={onApply} />,
    );
    const file = makeFile();
    uploadFile(container, file);
    fireEvent.click(await screen.findByRole("button", { name: "确认回传" }));
    fireEvent.click(await screen.findByRole("button", { name: "强制接管并上传" }));
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(2));
    expect(onApply).toHaveBeenNthCalledWith(1, file, { siteIssueMergeToken: "merge-preview" });
    expect(onApply).toHaveBeenLastCalledWith(file, { forceTakeover: true, siteIssueMergeToken: "merge-preview" });
  });

  it("two-phase: cancel leaves the file unapplied", async () => {
    const onValidate = vi.fn().mockResolvedValue({ will_void_rows: [] });
    const onApply = vi.fn();
    const { container } = render(
      <WorkbookRoundTrip
        title="总表"
        filename="a.xlsx"
        canUpload
        onDownload={vi.fn()}
        onValidate={onValidate}
        onApply={onApply}
      />,
    );
    const file = makeFile();
    const firstInput = uploadFile(container, file);
    const cancelButtons = await screen.findAllByRole("button", { name: /取\s*消/ });
    fireEvent.click(cancelButtons[cancelButtons.length - 1]);
    // 弹窗关闭有动画，等一拍再断言——本用例的要点是「取消绝不落库」
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(onApply).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(container.querySelector('input[type="file"]')).not.toBe(firstInput));
    uploadFile(container, file);
    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(2));
    const retryCancelButtons = await screen.findAllByRole("button", { name: /取\s*消/ });
    fireEvent.click(retryCancelButtons[retryCancelButtons.length - 1]);
  });

  it("two-phase: a rejected validation never reaches apply and shows the backend line error", async () => {
    const onValidate = vi.fn().mockRejectedValue({
      response: { data: { detail: "第 12 行：上传行数不足导出行数 50%，整本拒绝" } },
    });
    const onApply = vi.fn();
    const { container } = render(
      <WorkbookRoundTrip
        title="总表"
        filename="a.xlsx"
        canUpload
        onDownload={vi.fn()}
        onValidate={onValidate}
        onApply={onApply}
      />,
    );
    const file = makeFile();
    const firstInput = uploadFile(container, file);
    expect(await screen.findByText(/第 12 行：上传行数不足/)).toBeInTheDocument();
    expect(screen.getByText(/第 12 行：上传行数不足/).closest('[role="alert"]')).not.toBeNull();
    expect(onApply).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(container.querySelector('input[type="file"]')).not.toBe(firstInput));
    uploadFile(container, file);
    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(2));
  });

  it("预检一次展示全部身份碰撞行及原值/上传值，关闭后可重新上传修改文件", async () => {
    const onValidate = vi.fn().mockRejectedValue({
      response: {
        status: 422,
        data: {
          detail: {
            code: "stale_site_replay",
            message: "共 3 行与已入库领用记录冲突，首行：第 1031 行",
            rows: [
              { row_no: 1031, entity_id: "manual-site:one", issue_no: "WBDD-1", pn: "PN-1", sn: "SN-1", differences: [{ field: "领用日期", old: "2026-08-05", new: "2026-09-01" }] },
              { row_no: 1033, entity_id: "manual-site:two", issue_no: "WBDD-2", pn: "PN-2", sn: "", differences: [{ field: "是否应返还", old: "", new: "是" }] },
              { row_no: 1040, entity_id: "manual-site:three", issue_no: "WBDD-3", pn: "PN-3", sn: "SN-3", differences: [{ field: "数量", old: "0", new: "2" }, { field: "备注", old: "原备注", new: "上传备注" }] },
            ],
            total_count: 3,
            fix_hint: "已入库记录请保留实体ID在原行修改；真正的新领用请将领用单号留空自动编号（或使用尚未占用的单号）",
          },
        },
      },
    });
    const onApply = vi.fn();
    const { container } = render(
      <WorkbookRoundTrip title="总表" filename="a.xlsx" canUpload onDownload={vi.fn()} onValidate={onValidate} onApply={onApply} />,
    );
    const file = makeFile();
    const firstInput = uploadFile(container, file);

    const dialog = within(await screen.findByRole("dialog"));
    await waitFor(() => expect(dialog.getByText("共 3 行与已入库领用记录冲突，首行：第 1031 行")).toBeVisible());
    for (const row of [1031, 1033, 1040]) expect(dialog.getByText(`行号：${row}`)).toBeVisible();
    for (const id of ["one", "two", "three"]) expect(dialog.getByText(`实体ID：manual-site:${id}`)).toBeVisible();
    expect(dialog.getByText("领用单号：WBDD-1 · PN：PN-1 · SN：SN-1")).toBeVisible();
    expect(dialog.getByText("领用日期：2026-08-05 → 2026-09-01")).toBeVisible();
    expect(dialog.getByText("是否应返还：（空） → 是")).toBeVisible();
    expect(dialog.getByText("数量：0 → 2")).toBeVisible();
    expect(dialog.getByText("备注：原备注 → 上传备注")).toBeVisible();
    expect(dialog.getByText(/已入库记录请保留实体ID在原行修改/)).toBeVisible();
    expect(screen.queryByRole("button", { name: /强制接管/ })).toBeNull();
    expect(onApply).not.toHaveBeenCalled();

    fireEvent.click(dialog.getByRole("button", { name: "返回修改文件" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBe(firstInput));
    uploadFile(container, file);
    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(2));
  });

  it("身份碰撞超过明细上限时展示返回的全部 200 行及真实总数", async () => {
    const rows = Array.from({ length: 200 }, (_, index) => ({
      row_no: index + 2,
      entity_id: `manual-site:${index}`,
      issue_no: `WBDD-${index}`,
      pn: `PN-${index}`,
      sn: "",
      differences: [{ field: "领用日期", old: "2026-08-05", new: "2026-09-01" }],
    }));
    const { container } = render(
      <WorkbookRoundTrip
        title="总表" filename="a.xlsx" canUpload onDownload={vi.fn()} onApply={vi.fn()}
        onValidate={vi.fn().mockRejectedValue({ response: { status: 422, data: { detail: {
          code: "stale_site_replay", message: "共 203 行与已入库领用记录冲突", rows,
          total_count: 203, fix_hint: "已入库记录请保留实体ID在原行修改",
        } } } })}
      />,
    );
    uploadFile(container, makeFile());

    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText("行号：2")).toBeInTheDocument();
    expect(dialog.getByText("行号：201")).toBeInTheDocument();
    expect(dialog.getByText("共 203 行冲突，当前展示前 200 行；其余 3 行请在处理后重新上传查看。")).toBeInTheDocument();
  });

  it.each(["直接上传", "强制接管重传"])("%s失败也展示身份碰撞明细且不能再次强制接管", async (path) => {
    const onApply = vi.fn();
    if (path === "强制接管重传") {
      onApply.mockRejectedValueOnce({ response: { status: 409, data: { detail: {
        code: "row_conflicts", message: "部分行已被他人更新",
        conflicts: [{ sheet: "06_领用返还", row: "WBDD-1", entity_id: "manual-site:one", field: "备注", base: "基线备注", old: "原备注", new: "上传备注" }],
      } } } });
    }
    onApply.mockRejectedValue({ response: { status: 422, data: { detail: {
      code: "stale_site_replay", message: "共 1 行与已入库领用记录冲突", total_count: 1,
      fix_hint: "已入库记录请保留实体ID在原行修改",
      rows: [{ row_no: 8, entity_id: "manual-site:one", issue_no: "WBDD-1", pn: "PN-1", sn: "", differences: [{ field: "领用日期", old: "2026-08-05", new: "2026-09-01" }] }],
    } } } });
    const { container } = render(
      <WorkbookRoundTrip title="总表" filename="a.xlsx" canUpload onDownload={vi.fn()} onApply={onApply} />,
    );
    uploadFile(container, makeFile());
    if (path === "强制接管重传") {
      fireEvent.click(await screen.findByRole("button", { name: /强制接管并上传/ }));
      await waitFor(() => expect(onApply).toHaveBeenLastCalledWith(expect.any(File), { forceTakeover: true }));
    }

    const errorDialog = await screen.findByRole("dialog", { name: "领用记录冲突，请修改文件后重新上传" });
    await waitFor(() => expect(screen.queryByRole("button", { name: /强制接管/ })).toBeNull());
    const dialog = within(errorDialog);
    expect(dialog.getByText("行号：8")).toBeInTheDocument();
    expect(dialog.getByText("领用日期：2026-08-05 → 2026-09-01")).toBeInTheDocument();
    expect(screen.queryByText("待处理文件：回填.xlsx")).toBeNull();
    fireEvent.click(dialog.getByRole("button", { name: "返回修改文件" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onApply).toHaveBeenCalledTimes(path === "强制接管重传" ? 2 : 1);
  });

  it.each([
    { label: "缺失", rows: undefined },
    { label: "空数组", rows: [] },
    { label: "字段缺失", rows: [{ row_no: 2 }] },
  ])("身份碰撞无可用结构化明细（$label）时保留原文提示", async ({ rows, label }) => {
    const onApply = vi.fn();
    const errorMessage = `第 12 行已入库，请重新下载再修改（${label}）`;
    const { container } = render(
      <WorkbookRoundTrip
        title="总表" filename="a.xlsx" canUpload onDownload={vi.fn()} onApply={onApply}
        onValidate={vi.fn().mockRejectedValue({ response: { status: 422, data: { detail: {
          code: "stale_site_replay", message: errorMessage, rows,
          total_count: 1, fix_hint: "保留实体ID",
        } } } })}
      />,
    );
    uploadFile(container, makeFile());
    expect(await screen.findByText(errorMessage)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onApply).not.toHaveBeenCalled();
  });

  it.each(["切换账号", "撤销权限", "切换项目"])("身份碰撞明细在%s后关闭", async (action) => {
    localStorage.setItem("token", "synthetic-session-a");
    const onApply = vi.fn();
    const props = {
      title: "总表", filename: "a.xlsx", canUpload: true, onDownload: vi.fn(), onApply,
      onValidate: vi.fn().mockRejectedValue({ response: { status: 422, data: { detail: {
        code: "stale_site_replay", message: "共 1 行冲突", total_count: 1, fix_hint: "保留实体ID",
        rows: [{ row_no: 2, entity_id: "manual-site:private", issue_no: "WBDD-private", pn: "PN-private", sn: "", differences: [{ field: "地址", old: "原地址", new: "上传地址" }] }],
      } } } }),
    };
    const { container, rerender, unmount } = render(<WorkbookRoundTrip {...props} />);
    uploadFile(container, makeFile());
    await screen.findByRole("dialog");
    if (action === "切换账号") {
      localStorage.setItem("token", "synthetic-session-b");
      rerender(<WorkbookRoundTrip {...props} />);
    } else if (action === "撤销权限") {
      rerender(<WorkbookRoundTrip {...props} canUpload={false} />);
    } else {
      unmount();
    }
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryByText("地址：原地址 → 上传地址")).toBeNull();
    expect(onApply).not.toHaveBeenCalled();
  });

  it("预检409保留原文件草稿，可直接重试并在成功后清除", async () => {
    const onValidate = vi.fn()
      .mockRejectedValueOnce({
        response: { status: 409, data: { detail: { message: "项目备注版本已变化，请重新核对后回传" } } },
      })
      .mockResolvedValue({ changes: [{ sheet: "01_项目概览", field: "项目备注", old: "旧备注", new: "草稿备注" }] });
    const onApply = vi.fn().mockResolvedValue({
      changes: [{ sheet: "01_项目概览", field: "项目备注", old: "旧备注", new: "草稿备注" }],
    });
    const { container } = render(
      <WorkbookRoundTrip
        title="总表" filename="a.xlsx" canUpload
        onDownload={vi.fn()} onValidate={onValidate} onApply={onApply}
      />,
    );
    const file = makeFile();
    uploadFile(container, file);
    expect(await screen.findByText("项目备注版本已变化，请重新核对后回传")).toBeVisible();
    expect(await screen.findByText("待处理文件：回填.xlsx")).toBeVisible();
    expect(onApply).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "重试原文件" }));
    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(2));
    expect(onValidate.mock.calls[1][0]).toBe(file);
    fireEvent.click(await screen.findByRole("button", { name: "确认回传" }));
    await waitFor(() => expect(onApply).toHaveBeenCalledWith(file, undefined));
    await waitFor(() => expect(screen.queryByText("待处理文件：回填.xlsx")).toBeNull());
  });

  it.each(["取消", "权限撤销", "切换账号"])("落库409保留文件，%s后清除草稿", async (action) => {
    localStorage.setItem("token", "synthetic-session-a");
    const onApply = vi.fn().mockRejectedValue({
      response: { status: 409, data: { detail: "项目备注已被修改，整本未写入" } },
    });
    const props = { title: "总表", filename: "a.xlsx", canUpload: true, onDownload: vi.fn(), onApply };
    const { container, rerender } = render(<WorkbookRoundTrip {...props} />);
    const firstInput = uploadFile(container, makeFile());
    expect(await screen.findByText("待处理文件：回填.xlsx")).toBeVisible();
    await waitFor(() => expect(container.querySelector('input[type="file"]')).not.toBe(firstInput));
    if (action === "取消") {
      fireEvent.click(screen.getByRole("button", { name: "取消文件草稿" }));
    } else if (action === "权限撤销") {
      rerender(<WorkbookRoundTrip {...props} canUpload={false} />);
    } else {
      localStorage.setItem("token", "synthetic-session-b");
      rerender(<WorkbookRoundTrip {...props} />);
    }
    await waitFor(() => expect(screen.queryByText("待处理文件：回填.xlsx")).toBeNull());
    expect(screen.queryByRole("button", { name: "重试原文件" })).toBeNull();
    expect(onApply).toHaveBeenCalledTimes(1);
    if (action === "权限撤销") {
      rerender(<WorkbookRoundTrip {...props} />);
      expect(screen.queryByText("待处理文件：回填.xlsx")).toBeNull();
    }
  });

  it("账号切换后旧上传的409晚到响应不能留下文件草稿", async () => {
    localStorage.setItem("token", "synthetic-session-a");
    let rejectOld!: (error: unknown) => void;
    const onApply = vi.fn(() => new Promise<never>((_, reject) => { rejectOld = reject; }));
    const props = { title: "总表", filename: "a.xlsx", canUpload: true, onDownload: vi.fn(), onApply };
    const { container, rerender } = render(<WorkbookRoundTrip {...props} />);
    uploadFile(container, makeFile());
    await waitFor(() => expect(onApply).toHaveBeenCalledTimes(1));
    localStorage.setItem("token", "synthetic-session-b");
    rerender(<WorkbookRoundTrip {...props} />);
    await act(async () => rejectOld({ response: { status: 409, data: { detail: "旧请求版本冲突" } } }));
    expect(screen.queryByText("旧请求版本冲突")).toBeNull();
    expect(screen.queryByText("待处理文件：回填.xlsx")).toBeNull();
  });

  it("切换账号后旧预检的晚到响应不能打开确认弹窗", async () => {
    localStorage.setItem("token", "synthetic-session-a");
    let resolvePreview!: (value: { will_void_rows: [] }) => void;
    const onValidate = vi.fn(() => new Promise<{ will_void_rows: [] }>((resolve) => { resolvePreview = resolve; }));
    const onApply = vi.fn();
    const props = { title: "总表", filename: "a.xlsx", canUpload: true, onDownload: vi.fn(), onValidate, onApply };
    const { container, rerender } = render(<WorkbookRoundTrip {...props} />);
    uploadFile(container, makeFile());
    await waitFor(() => expect(onValidate).toHaveBeenCalledTimes(1));
    localStorage.setItem("token", "synthetic-session-b");
    rerender(<WorkbookRoundTrip {...props} />);
    await act(async () => resolvePreview({ will_void_rows: [] }));
    expect(screen.queryByRole("button", { name: "确认回传" })).toBeNull();
    expect(onApply).not.toHaveBeenCalled();
  });

  it.each(["切换账号", "撤销权限", "切换项目"])("已有确认弹窗在%s后不能提交旧文件", async (action) => {
    localStorage.setItem("token", "synthetic-session-a");
    const onValidate = vi.fn().mockResolvedValue({ will_void_rows: [] });
    const onApply = vi.fn();
    const props = { title: "总表", filename: "a.xlsx", canUpload: true, onDownload: vi.fn(), onValidate, onApply };
    const { container, rerender, unmount } = render(<WorkbookRoundTrip {...props} />);
    uploadFile(container, makeFile());
    const confirm = await screen.findByRole("button", { name: "确认回传" });
    if (action === "切换账号") {
      localStorage.setItem("token", "synthetic-session-b");
      rerender(<WorkbookRoundTrip {...props} />);
    } else if (action === "撤销权限") {
      rerender(<WorkbookRoundTrip {...props} canUpload={false} />);
    } else {
      unmount();
    }
    // 已捕获的旧按钮即便在弹窗退场动画期间收到点击也不能写库。
    fireEvent.click(confirm);
    await waitFor(() => expect(screen.queryByRole("button", { name: "确认回传" })).toBeNull());
    expect(onApply).not.toHaveBeenCalled();
  });
});

describe("WorkbookRoundTrip · 2.7.0 行级冲突与强制接管", () => {
  it("409 冲突展示三值对照，确认后带 force_takeover 重传", async () => {
    const conflicts = [
      {
        sheet: "03_备件明细", row: "WBDD-1", entity_id: "7",
        field: "需求数量", old: "4", new: "6", base: "2",
        reason: "server_changed_since_export",
      },
    ];
    const onApply = vi.fn()
      .mockRejectedValueOnce({
        response: {
          status: 409,
          data: {
            detail: {
              code: "row_conflicts",
              message: "部分行已被他人更新",
              conflicts,
            },
          },
        },
      })
      .mockResolvedValueOnce({
        cost_refills: 0, site_return_flags: 0, expense_updates: 0,
        collection_creates: 0, collection_voids: 0,
        changes: conflicts.map((c) => ({ ...c, overridden: true })),
        overridden: conflicts,
        force_takeover: true,
      });
    const { container } = render(
      <WorkbookRoundTrip
        title="测试表" filename="t.xlsx"
        onDownload={vi.fn().mockResolvedValue(new Blob())}
        onApply={onApply}
        canUpload
      />,
    );
    uploadFile(container, makeFile());
    // 冲突弹窗：三值都渲染
    expect(
      (await screen.findAllByText(/有 1 处改动与他人冲突/)).length,
    ).toBeGreaterThan(0);
    expect(screen.getAllByText(/你下载时的值：2/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/当前最新值：4/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/你上传的值：6/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /强制接管并上传/ }));
    await waitFor(() => {
      expect(onApply).toHaveBeenCalledTimes(2);
      expect(onApply).toHaveBeenLastCalledWith(expect.any(File), {
        forceTakeover: true,
      });
    });
    await screen.findByText(/已覆盖/);
  });
});
