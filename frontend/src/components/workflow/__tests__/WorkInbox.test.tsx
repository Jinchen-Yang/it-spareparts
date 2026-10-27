import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  act, cleanupAntd, fireEvent, render, screen, waitFor, within,
} from "../../../test/antdRender";
import {
  claimWorkItem, getWorkBadge, listWorkCandidates, listWorkItems,
  releaseWorkItem, startWorkItem, transferWorkItem,
} from "../../../api/workItems";
import type { WorkItem } from "../../../api/workItems";
import WorkInbox from "../WorkInbox";

vi.mock("../../../api/workItems", () => ({
  listWorkItems: vi.fn(),
  getWorkBadge: vi.fn(),
  claimWorkItem: vi.fn(),
  startWorkItem: vi.fn(),
  releaseWorkItem: vi.fn(),
  transferWorkItem: vi.fn(),
  reassignWorkItem: vi.fn(),
  completeWorkItem: vi.fn(),
  declineWorkItem: vi.fn(),
  listWorkCandidates: vi.fn(),
}));

const poolItem: WorkItem = {
  id: 11, subject_type: "demo", subject_id: 101, pool_key: "demo_pool",
  assignee_id: null, claimed_by: null, status: "pending" as const,
  version: 3, paused: false, summary: { title: "演示待办" },
  allowed_actions: ["claim"],
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};
const claimedItem: WorkItem = {
  ...poolItem, assignee_id: 7, claimed_by: 7, status: "in_progress" as const,
  allowed_actions: ["release", "transfer", "complete", "decline"],
};

function page(items = [poolItem], canSupervise = false) {
  return { items, total: items.length, page: 1, page_size: 20, can_supervise: canSupervise };
}

function inbox(onOpen = vi.fn()) {
  return render(<WorkInbox subjectType="demo"
    renderRow={(item) => <span>{String(item.summary.title)}</span>}
    onOpen={onOpen} />);
}

async function poolInbox() {
  inbox();
  await waitFor(() => expect(listWorkItems).toHaveBeenCalled());
  fireEvent.click(screen.getByRole("tab", { name: /公共池/ }));
  await screen.findByRole("button", { name: /领\s*取/ });
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getWorkBadge).mockResolvedValue({ assigned: 1, pool: 2, claimed: 3 });
  vi.mocked(listWorkItems).mockResolvedValue(page());
  vi.mocked(listWorkCandidates).mockResolvedValue({ items: [{ id: 9, display_name: "李工" }] });
  vi.mocked(claimWorkItem).mockResolvedValue(claimedItem);
  vi.mocked(releaseWorkItem).mockResolvedValue(poolItem);
  vi.mocked(transferWorkItem).mockResolvedValue({ ...claimedItem, assignee_id: 9 });
  vi.mocked(startWorkItem).mockResolvedValue(claimedItem);
});

afterEach(cleanupAntd);

it("切换四个页签时保持业务类型过滤，同级账号没有全部页签", async () => {
  inbox();
  await screen.findByText("演示待办");
  expect(screen.queryByRole("tab", { name: "全部" })).toBeNull();
  for (const [label, tab] of [
    ["公共池", "pool"], ["我已领取", "claimed"], ["已处理", "done"], ["指派给我", "assigned"],
  ]) {
    fireEvent.click(screen.getByRole("tab", { name: new RegExp(label) }));
    await waitFor(() => expect(listWorkItems).toHaveBeenLastCalledWith(
      expect.objectContaining({ tab, subject_type: "demo", page: 1 }), expect.any(AbortSignal),
    ));
  }
});

it("主管通过服务端能力看到全部页签", async () => {
  vi.mocked(listWorkItems).mockResolvedValue(page([poolItem], true));
  inbox();
  fireEvent.click(await screen.findByRole("tab", { name: "全部" }));
  await waitFor(() => expect(listWorkItems).toHaveBeenLastCalledWith(
    expect.objectContaining({ tab: "all", subject_type: "demo" }), expect.any(AbortSignal),
  ));
});

it("领取时携带版本号，成功后刷新列表和角标", async () => {
  await poolInbox();
  const listCalls = vi.mocked(listWorkItems).mock.calls.length;
  const badgeCalls = vi.mocked(getWorkBadge).mock.calls.length;
  fireEvent.click(screen.getByRole("button", { name: /领\s*取/ }));
  await waitFor(() => expect(claimWorkItem).toHaveBeenCalledWith(11, { expected_version: 3 }));
  await waitFor(() => expect(vi.mocked(listWorkItems).mock.calls.length).toBeGreaterThan(listCalls));
  await waitFor(() => expect(vi.mocked(getWorkBadge).mock.calls.length).toBeGreaterThan(badgeCalls));
});

it("领取冲突显示当前领取人，并重新获取待办", async () => {
  vi.mocked(claimWorkItem).mockRejectedValue({ response: { status: 409, data: {
    detail: { message: "待办已被领取", claimed_by_name: "王工" },
  } } });
  await poolInbox();
  const listCalls = vi.mocked(listWorkItems).mock.calls.length;
  fireEvent.click(screen.getByRole("button", { name: /领\s*取/ }));
  expect(await screen.findByText(/已被.*王工.*领取/)).toBeInTheDocument();
  await waitFor(() => expect(vi.mocked(listWorkItems).mock.calls.length).toBeGreaterThan(listCalls));
});

it("售前可指定统一冲突文案并保留自动刷新", async () => {
  vi.mocked(claimWorkItem).mockRejectedValue({ response: { status: 409, data: {
    detail: { message: "记录已变化", claimed_by_name: "王工" },
  } } });
  render(<WorkInbox subjectType="presales_inquiry" conflictMessage="已被他人修改/领取"
    renderRow={() => <span>售前待办</span>} onOpen={vi.fn()} />);
  await screen.findByText("售前待办");
  const listCalls = vi.mocked(listWorkItems).mock.calls.length;
  fireEvent.click(screen.getByRole("button", { name: /领\s*取/ }));
  expect(await screen.findByText("已被他人修改/领取")).toBeInTheDocument();
  await waitFor(() => expect(vi.mocked(listWorkItems).mock.calls.length).toBeGreaterThan(listCalls));
});

it("退回公共池必须填写原因，提交当前版本与原因", async () => {
  vi.mocked(listWorkItems).mockResolvedValue({ ...page(), items: [claimedItem] });
  inbox();
  fireEvent.click(await screen.findByRole("button", { name: /退\s*回/ }));
  const dialog = await screen.findByRole("dialog");
  const confirm = within(dialog).getByRole("button", { name: /确认退回/ });
  expect(confirm).toBeDisabled();
  fireEvent.change(within(dialog).getByLabelText(/原因/), { target: { value: "   " } });
  expect(confirm).toBeDisabled();
  expect(releaseWorkItem).not.toHaveBeenCalled();
  fireEvent.change(within(dialog).getByLabelText(/原因/), { target: { value: "暂时无法处理" } });
  fireEvent.click(confirm);
  await waitFor(() => expect(releaseWorkItem).toHaveBeenCalledWith(11, {
    expected_version: 3, reason: "暂时无法处理",
  }));
});

it("转交必须选择候选人并填写原因", async () => {
  vi.mocked(listWorkItems).mockResolvedValue({ ...page(), items: [claimedItem] });
  inbox();
  fireEvent.click(await screen.findByRole("button", { name: /转\s*交/ }));
  const dialog = await screen.findByRole("dialog");
  await waitFor(() => expect(listWorkCandidates).toHaveBeenCalledWith("demo_pool", expect.any(AbortSignal)));
  const confirm = within(dialog).getByRole("button", { name: /确认转交/ });
  expect(confirm).toBeDisabled();
  expect(transferWorkItem).not.toHaveBeenCalled();
  fireEvent.change(within(dialog).getByLabelText(/原因/), { target: { value: "请协助处理" } });
  expect(confirm).toBeDisabled();
  expect(transferWorkItem).not.toHaveBeenCalled();
  fireEvent.mouseDown(within(dialog).getByRole("combobox"));
  await screen.findByRole("option", { name: "李工" });
  const option = screen.getAllByText("李工").find((node) => node.closest(".ant-select-item-option"));
  expect(option).toBeDefined();
  fireEvent.click(option!.closest(".ant-select-item-option")!);
  fireEvent.click(confirm);
  await waitFor(() => expect(transferWorkItem).toHaveBeenCalledWith(11, {
    expected_version: 3, reason: "请协助处理", to_user_id: 9,
  }));
});

it("打开被指派的待办时先开始处理，再把新版本交给业务层", async () => {
  vi.mocked(listWorkItems).mockResolvedValue({ ...page(), items: [{
    ...poolItem, assignee_id: 7, allowed_actions: ["start", "transfer"],
  }] });
  const onOpen = vi.fn();
  inbox(onOpen);
  fireEvent.click(await screen.findByRole("button", { name: /打\s*开/ }));
  await waitFor(() => expect(startWorkItem).toHaveBeenCalledWith(11, { expected_version: 3 }));
  await waitFor(() => expect(onOpen).toHaveBeenCalledWith(claimedItem));
});

it("写操作只显示服务端允许的动作，暂停状态仍可打开详情", async () => {
  vi.mocked(listWorkItems).mockResolvedValue({ ...page(), items: [{
    ...claimedItem, paused: true, allowed_actions: [],
  }] });
  const onOpen = vi.fn();
  inbox(onOpen);
  await screen.findByText("已暂停");
  expect(screen.queryByRole("button", { name: /完\s*成/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /转\s*交/ })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /打\s*开/ }));
  expect(startWorkItem).not.toHaveBeenCalled();
  expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ paused: true }));
});

it("开始处理的响应晚到时，切换业务类型后不会打开旧业务详情", async () => {
  vi.mocked(listWorkItems).mockResolvedValue({ ...page(), items: [{
    ...poolItem, assignee_id: 7, allowed_actions: ["start"],
  }] });
  let resolve!: (item: WorkItem) => void;
  vi.mocked(startWorkItem).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
  const onOpen = vi.fn();
  const view = inbox(onOpen);
  fireEvent.click(await screen.findByRole("button", { name: /打\s*开/ }));
  view.rerender(<WorkInbox subjectType="other_demo"
    renderRow={(item) => <span>{String(item.summary.title)}</span>} onOpen={onOpen} />);
  await act(async () => { resolve(claimedItem); });
  expect(onOpen).not.toHaveBeenCalled();
  expect(listWorkItems).toHaveBeenLastCalledWith(
    expect.objectContaining({ subject_type: "other_demo" }), expect.any(AbortSignal),
  );
});

it("原生表格分页筛选与按行数排序，领取沿用版本检查", async () => {
  const rows = [
    { ...poolItem, summary: { created_by_name: "销售甲", line_count: 2 } },
    { ...poolItem, id: 12, summary: { created_by_name: "销售乙", line_count: 8 } },
  ];
  vi.mocked(listWorkItems).mockResolvedValue(page(rows));
  render(<WorkInbox subjectType="demo" renderRow={(item) => <span>{String(item.summary.created_by_name)}</span>} onOpen={vi.fn()} />);
  await screen.findByText("销售乙");
  expect(listWorkItems).toHaveBeenCalledWith(expect.objectContaining({ page: 1, page_size: 20 }), expect.any(AbortSignal));
  const grid = screen.getByRole("table", { name: "报价工作台队列" });
  fireEvent.click(within(grid).getByRole("columnheader", { name: /行数/ }));
  fireEvent.click(within(grid).getByRole("columnheader", { name: /行数/ }));
  expect(within(grid).getAllByRole("row")[1]).toHaveTextContent("销售乙");
  fireEvent.change(screen.getByLabelText("筛选待办"), { target: { value: "销售甲" } });
  expect(within(grid).queryByText("销售乙")).not.toBeInTheDocument();
  fireEvent.click(within(grid).getByRole("button", { name: /领\s*取/ }));
  await waitFor(() => expect(claimWorkItem).toHaveBeenCalledWith(11, { expected_version: 3 }));
});

it("待办打开守卫拒绝后不会提前开始新待办", async () => {
  vi.mocked(listWorkItems).mockResolvedValue(page([{ ...poolItem, assignee_id: 7, allowed_actions: ["start"] }]));
  const onOpen = vi.fn();
  const beforeOpen = vi.fn(async () => false);
  render(<WorkInbox subjectType="demo" beforeOpen={beforeOpen} renderRow={() => <span>待开始询价</span>} onOpen={onOpen} />);
  fireEvent.click(await screen.findByRole("button", { name: /打\s*开/ }));
  await waitFor(() => expect(beforeOpen).toHaveBeenCalled());
  expect(startWorkItem).not.toHaveBeenCalled();
  expect(onOpen).not.toHaveBeenCalled();
});

it("开始处理失败不进入报价页，重试成功才传递最新待办身份", async () => {
  vi.mocked(listWorkItems).mockResolvedValue(page([{ ...poolItem, assignee_id: 7, allowed_actions: ["start"] }]));
  vi.mocked(startWorkItem).mockRejectedValueOnce(new Error("offline"));
  const onOpen = vi.fn();
  inbox(onOpen);
  fireEvent.click(await screen.findByRole("button", { name: /打\s*开/ }));
  await screen.findByText("打开失败，请重试");
  expect(onOpen).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: /打\s*开/ }));
  await waitFor(() => expect(onOpen).toHaveBeenCalledWith(claimedItem));
  expect(startWorkItem).toHaveBeenCalledTimes(2);
});
