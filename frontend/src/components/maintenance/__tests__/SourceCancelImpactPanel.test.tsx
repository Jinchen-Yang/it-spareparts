import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn(), confirm: vi.fn() }));
vi.mock("../../../api/maintenanceSourceCancelCascade", () => ({
  listSourceCancellations: api.list,
  getSourceCancellation: api.detail,
  confirmSourceCancellation: api.confirm,
}));
import SourceCancelImpactPanel from "../SourceCancelImpactPanel";

const candidate = {
  batch_id: 8, source_order_id: "private-order-id", order_no: "WBDD-20261020-0001",
  data_status: "已取消", executable: true, warnings: [],
};
const detail = {
  ...candidate, expected_digest: "digest-8",
  demands: [{ line_id: 12, raw_line_id: "private-raw-line", pn: "PN-需求", qty: "3", is_active: true }],
  exact_site_lines: [{ issue_line_id: "exact-1", issue_id: "issue-1", issue_no: "LY-精确-1", issue_date: "2026-10-18", link_reason: "原始需求明细ID", pn: "PN-精确关联", quantity: "2", is_active: true }],
  unlinked_site_lines: [{ issue_line_id: "unlinked-1", issue_id: "issue-2", issue_no: "LY-待核对-1", issue_date: "2026-10-19", link_reason: "仅需求单头或单号关联", pn: "PN-无法关联", quantity: "1", is_active: true }],
};
const result = {
  batch_id: 8, source_order_id: "private-order-id", voided_orders: 1, voided_lines: 1,
  voided_site_lines: 1, voided_site_issues: 1, replayed: false,
  confirmed_at: "2026-10-20T08:00:00Z", warnings: [],
};
const props = () => ({ projectId: "project-1", canConfirm: true,
  onChanged: vi.fn().mockResolvedValue(true), registerRefresh: vi.fn() });

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  api.list.mockResolvedValue({ data: { items: [candidate], total: 1, page: 1, page_size: 10 } });
  api.detail.mockResolvedValue({ data: detail });
  api.confirm.mockResolvedValue({ data: result });
});
afterEach(cleanup);

describe("已取消影响人工确认", () => {
  it("先查看具体候选及保留项，填写原因后携带摘要确认并刷新项目", async () => {
    const p = props();
    render(<SourceCancelImpactPanel {...p} />);
    await screen.findByText(candidate.order_no);
    expect(api.confirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("PN-精确关联");
    expect(within(dialog).getByText("PN-需求")).toBeInTheDocument();
    expect(within(dialog).getByText("PN-无法关联")).toBeInTheDocument();
    expect(within(dialog).getByText("LY-精确-1")).toBeInTheDocument();
    expect(within(dialog).getByText("原始需求明细ID")).toBeInTheDocument();
    expect(within(dialog).getByText(/无法精确关联的领用保留/)).toBeInTheDocument();
    expect(within(dialog).queryByText("private-raw-line")).toBeNull();
    expect(within(dialog).getByRole("button", { name: "确认连带作废" })).toBeDisabled();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "作废原因" }), { target: { value: "已核对源单取消" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认连带作废" }));
    await waitFor(() => expect(api.confirm).toHaveBeenCalledWith("project-1", 8, "private-order-id", {
      expected_digest: "digest-8", reason: "已核对源单取消", idempotency_key: expect.stringMatching(/^source-cancel-/),
    }, ""));
    expect(await screen.findByText(/已确认作废：1 张需求单、1 条需求明细、1 条精确关联领用/)).toBeInTheDocument();
    expect(p.onChanged).toHaveBeenCalledTimes(1);
  });

  it("409 整次未执行后关闭确认并要求刷新，旧摘要不能继续提交", async () => {
    api.confirm.mockRejectedValueOnce({ response: { status: 409, data: { detail: "摘要已变化" } } });
    const p = props();
    render(<SourceCancelImpactPanel {...p} />);
    await screen.findByText(candidate.order_no);
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(await within(dialog).findByRole("textbox", { name: "作废原因" }), { target: { value: "核对取消" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认连带作废" }));
    expect(await screen.findByText(/本次未执行，请刷新影响清单后重新核对/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: "查看影响" })).toBeDisabled();
    expect(p.onChanged).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "刷新影响清单" }));
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole("button", { name: "查看影响" })).toBeEnabled());
  });

  it("网络结果未知时冻结原请求重试，重放结果明确属于历史确认", async () => {
    api.confirm.mockRejectedValueOnce(new Error("connection lost"));
    api.confirm.mockResolvedValueOnce({ data: { ...result, replayed: true } });
    const p = props();
    render(<SourceCancelImpactPanel {...p} />);
    await screen.findByText(candidate.order_no);
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(await within(dialog).findByRole("textbox", { name: "作废原因" }), { target: { value: "核对取消" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认连带作废" }));
    expect(await within(dialog).findByText(/本次结果尚未确认/)).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: "作废原因" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "刷新影响清单" })).toBeDisabled();
    const original = api.confirm.mock.calls[0];
    fireEvent.click(within(dialog).getByRole("button", { name: "重试同一次确认" }));
    await waitFor(() => expect(api.confirm).toHaveBeenCalledTimes(2));
    expect(api.confirm.mock.calls[1]).toEqual(original);
    expect(await screen.findByText(/已返回原确认结果.*不代表当前状态/)).toBeInTheDocument();
  });

  it("双击只发一次，写入成功但刷新失败时保留成功及警告，不重发确认", async () => {
    let finish!: (value: unknown) => void;
    api.confirm.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const p = props(); p.onChanged.mockRejectedValueOnce(new Error("refresh failed"));
    render(<SourceCancelImpactPanel {...p} />);
    await screen.findByText(candidate.order_no);
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(await within(dialog).findByRole("textbox", { name: "作废原因" }), { target: { value: "核对取消" } });
    const submit = within(dialog).getByRole("button", { name: "确认连带作废" });
    fireEvent.click(submit); fireEvent.click(submit);
    expect(api.confirm).toHaveBeenCalledTimes(1);
    await act(async () => finish({ data: { ...result, warnings: ["未精确关联的领用已保留"] } }));
    expect(await screen.findByText(/已确认作废.*页面刷新失败/)).toBeInTheDocument();
    expect(screen.getByText("未精确关联的领用已保留")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试同一次确认" })).toBeNull();
    expect(api.confirm).toHaveBeenCalledTimes(1);
  });

  it("没有实名作废权限的 admin 也只有查看入口", async () => {
    localStorage.setItem("role", "admin");
    render(<SourceCancelImpactPanel {...props()} canConfirm={false} />);
    expect(screen.getByText(/当前账号没有连带作废权限/)).toBeInTheDocument();
    await screen.findByText(candidate.order_no);
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("PN-需求");
    expect(within(dialog).queryByRole("textbox", { name: "作废原因" })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "确认连带作废" })).toBeNull();
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it("未知非生效状态显示人工告警且不提供确认动作", async () => {
    const warning = "未知非生效状态，需人工";
    const unknown = { ...candidate, data_status: "已作废", executable: false, warnings: [warning] };
    api.list.mockResolvedValueOnce({ data: { items: [unknown], total: 1, page: 1, page_size: 10 } });
    api.detail.mockResolvedValueOnce({ data: { ...detail, ...unknown } });
    render(<SourceCancelImpactPanel {...props()} />);
    await screen.findByText(warning);
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText(warning);
    expect(within(dialog).queryByRole("button", { name: "确认连带作废" })).toBeNull();
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it("清单失败可重试，详情失败不会沿用旧数据，403 显示权限原因", async () => {
    api.list.mockRejectedValueOnce(new Error("offline"));
    api.detail.mockRejectedValueOnce({ response: { status: 403, data: { detail: "当前账号无此项目范围权限" } } });
    render(<SourceCancelImpactPanel {...props()} />);
    expect(await screen.findByText(/已取消影响加载失败/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查看影响" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "刷新影响清单" }));
    await screen.findByText(candidate.order_no);
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("当前账号无此项目范围权限")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "确认连带作废" })).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "重试详情" }));
    expect(await within(dialog).findByText("PN-需求")).toBeInTheDocument();
  });

  it("无候选显示空态，分页由服务端限定项目和页码", async () => {
    api.list.mockResolvedValueOnce({ data: { items: [candidate], total: 11, page: 1, page_size: 10 } });
    api.list.mockResolvedValueOnce({ data: { items: [], total: 0, page: 2, page_size: 10 } });
    render(<SourceCancelImpactPanel {...props()} />);
    await screen.findByText(candidate.order_no);
    fireEvent.click(screen.getByTitle("Next Page"));
    expect(await screen.findByText("当前没有已取消候选或未知非生效状态告警")).toBeInTheDocument();
    expect(api.list).toHaveBeenLastCalledWith("project-1", { page: 2, page_size: 10 });
  });

  it("切换项目后旧详情的迟到响应不能进入新项目确认上下文", async () => {
    let finishOld!: (value: unknown) => void;
    api.detail.mockReturnValueOnce(new Promise(resolve => { finishOld = resolve; }));
    const p = props(); const view = render(<SourceCancelImpactPanel {...p} />);
    await screen.findByText(candidate.order_no);
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const next = { ...candidate, source_order_id: "order-2", order_no: "WBDD-项目乙" };
    api.list.mockResolvedValue({ data: { items: [next], total: 1, page: 1, page_size: 10 } });
    api.detail.mockResolvedValueOnce({ data: { ...detail, ...next, demands: [], exact_site_lines: [], unlinked_site_lines: [] } });
    view.rerender(<SourceCancelImpactPanel {...p} projectId="project-2" />);
    await screen.findByText(next.order_no);
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("无需求明细");
    await act(async () => finishOld({ data: detail }));
    expect(within(dialog).queryByText("PN-需求")).toBeNull();
    expect(api.detail).toHaveBeenLastCalledWith("project-2", 8, "order-2");
  });

  it("账号 token 已改变时旧对话框不能向新账号提交", async () => {
    localStorage.setItem("token", "account-a");
    render(<SourceCancelImpactPanel {...props()} />);
    await screen.findByText(candidate.order_no);
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(await within(dialog).findByRole("textbox", { name: "作废原因" }), { target: { value: "核对取消" } });
    localStorage.setItem("token", "account-b");
    fireEvent.click(within(dialog).getByRole("button", { name: "确认连带作废" }));
    expect(api.confirm).not.toHaveBeenCalled();
  });

  it("确认返回 403 后清除旧请求并显示服务端实名权限说明", async () => {
    api.confirm.mockRejectedValueOnce({ response: { status: 403, data: { detail: { message: "必须使用实名系统账号" } } } });
    const p = props(); render(<SourceCancelImpactPanel {...p} />);
    await screen.findByText(candidate.order_no);
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(await within(dialog).findByRole("textbox", { name: "作废原因" }), { target: { value: "核对取消" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认连带作废" }));
    expect(await screen.findByText("必须使用实名系统账号")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试同一次确认" })).toBeNull();
    expect(p.onChanged).not.toHaveBeenCalled();
  });

  it("409 前发出的列表迟到响应不能解除必须刷新门禁", async () => {
    const p = props(); render(<SourceCancelImpactPanel {...p} />);
    await screen.findByText(candidate.order_no);
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(await within(dialog).findByRole("textbox", { name: "作废原因" }), { target: { value: "核对取消" } });
    let finishList!: (value: unknown) => void;
    api.list.mockReturnValueOnce(new Promise(resolve => { finishList = resolve; }));
    const refresh = p.registerRefresh.mock.calls.find(([key, callback]) => key === "source-cancel" && callback)![1] as () => Promise<boolean>;
    let background!: Promise<boolean>;
    act(() => { background = refresh(); });
    api.confirm.mockRejectedValueOnce({ response: { status: 409 } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认连带作废" }));
    await screen.findByText(/本次未执行，请刷新影响清单后重新核对/);
    await act(async () => {
      finishList({ data: { items: [candidate], total: 1, page: 1, page_size: 10 } });
      await background;
    });
    expect(screen.getByRole("button", { name: "查看影响" })).toBeDisabled();
    expect(screen.getByText(/本次未执行，请刷新影响清单后重新核对/)).toBeInTheDocument();
  });

  it("第 2 页候选处理后数量收缩，刷新回有效页而不会隐藏导航困在空页", async () => {
    const first = { ...candidate, order_no: "WBDD-第一页仍待处理" };
    let shrunk = false;
    api.list.mockImplementation((_project, params) => Promise.resolve({ data: {
      items: shrunk ? (params.page === 1 ? [first] : []) : [candidate],
      total: shrunk ? 1 : 11, page: params.page, page_size: 10,
    } }));
    const p = props();
    p.onChanged.mockImplementation(async () => {
      shrunk = true;
      const callbacks = p.registerRefresh.mock.calls.filter(([key, callback]) => key === "source-cancel" && callback);
      return (callbacks[callbacks.length - 1][1] as () => Promise<boolean>)();
    });
    render(<SourceCancelImpactPanel {...p} />);
    await screen.findByText(candidate.order_no);
    fireEvent.click(screen.getByTitle("Next Page"));
    await waitFor(() => expect(api.list).toHaveBeenLastCalledWith("project-1", { page: 2, page_size: 10 }));
    fireEvent.click(screen.getByRole("button", { name: "查看影响" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(await within(dialog).findByRole("textbox", { name: "作废原因" }), { target: { value: "核对取消" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认连带作废" }));
    expect(await screen.findByText(first.order_no)).toBeInTheDocument();
    expect(api.list).toHaveBeenLastCalledWith("project-1", { page: 1, page_size: 10 });
    expect(screen.queryByText("当前没有已取消候选或未知非生效状态告警")).toBeNull();
  });
});
