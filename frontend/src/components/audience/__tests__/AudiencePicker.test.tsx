import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AudiencePicker, { PUBLIC_AUDIENCE } from "../AudiencePicker";
import AudienceLock from "../AudienceLock";
import type { AudienceGroup, AudienceMode } from "../../../api/audience";

const mocks = vi.hoisted(() => ({ createAudienceGroup: vi.fn(), listAudienceGroups: vi.fn(), listAudiencePeople: vi.fn() }));
vi.mock("../../../api/audience", () => mocks);

const people = [{ id: "alice", display_name: "小杨" }, { id: "bob", display_name: "小李" }];
const groups: AudienceGroup[] = [
  { id: 1, name: "采购", scope: "public", owner_id: "boss", user_ids: ["alice"], archived: false },
  { id: 2, name: "销售", scope: "personal", owner_id: "alice", user_ids: ["bob"], archived: false },
  { id: 3, name: "旧分组", scope: "personal", owner_id: "alice", user_ids: ["bob"], archived: true },
];

function choose(panel: string, label: string) {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: panel }));
  const options = Array.from(document.querySelectorAll<HTMLElement>(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content"));
  fireEvent.click(options.find((option) => option.textContent === label)!);
}

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  mocks.listAudiencePeople.mockResolvedValue(people);
  mocks.listAudienceGroups.mockResolvedValue(groups);
});
afterEach(cleanup);

describe("AudiencePicker", () => {
  it("默认公开；私密开关和公开按钮输出无成员规则", () => {
    const onChange = vi.fn();
    render(<AudiencePicker people={people} groups={groups} onChange={onChange} />);
    fireEvent.click(screen.getByRole("switch", { name: "私密" }));
    expect(onChange).toHaveBeenLastCalledWith({ mode: "private", user_ids: [], group_ids: [] });
    fireEvent.click(screen.getByRole("switch", { name: "私密" }));
    expect(onChange).toHaveBeenLastCalledWith(PUBLIC_AUDIENCE);
    fireEvent.click(screen.getByRole("button", { name: "公开" }));
    expect(onChange).toHaveBeenLastCalledWith(PUBLIC_AUDIENCE);
  });

  it("部分可见多选人员和分组，切换不给谁看会清空另一面板", () => {
    const onChange = vi.fn();
    render(<AudiencePicker people={people} groups={groups} onChange={onChange} />);
    choose("谁可以看", "小杨");
    choose("谁可以看", "采购（公共分组）");
    expect(onChange).toHaveBeenLastCalledWith({ mode: "include", user_ids: ["alice"], group_ids: [1] });
    choose("不给谁看", "小杨");
    expect(onChange).toHaveBeenLastCalledWith({ mode: "exclude", user_ids: ["alice"], group_ids: [] });
    const includeSelect = screen.getByRole("combobox", { name: "谁可以看" }).closest(".ant-select")!;
    expect(includeSelect.querySelectorAll(".ant-select-selection-item")).toHaveLength(0);
    choose("不给谁看", "销售（个人分组）");
    expect(onChange).toHaveBeenLastCalledWith({ mode: "exclude", user_ids: ["alice"], group_ids: [2] });
    fireEvent.click(screen.getByRole("switch", { name: "私密" }));
    expect(onChange).toHaveBeenLastCalledWith({ mode: "private", user_ids: [], group_ids: [] });
  });

  it("取消最后一个成员回到公开，不能输出没有成员的include规则", () => {
    const onChange = vi.fn();
    render(<AudiencePicker people={people} groups={groups} onChange={onChange} />);
    choose("谁可以看", "小杨"); choose("谁可以看", "小杨");
    expect(onChange).toHaveBeenLastCalledWith(PUBLIC_AUDIENCE);
  });

  it("当场保存个人分组并用新分组替换选中的人，可以继续复用", async () => {
    const onChange = vi.fn();
    mocks.createAudienceGroup.mockResolvedValue({ ...groups[1], id: 8, name: "对接人", user_ids: ["alice", "bob"] });
    render(<AudiencePicker people={people} groups={groups} onChange={onChange} />);
    choose("谁可以看", "小杨"); choose("谁可以看", "小李");
    fireEvent.change(screen.getByRole("textbox", { name: "新分组名称" }), { target: { value: " 对接人 " } });
    fireEvent.click(screen.getByRole("button", { name: "存为分组" }));
    await waitFor(() => expect(mocks.createAudienceGroup).toHaveBeenCalledWith({ name: "对接人", scope: "personal", user_ids: ["alice", "bob"] }));
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith({ mode: "include", user_ids: [], group_ids: [8] }));
    expect(screen.getByRole("textbox", { name: "新分组名称" })).toHaveValue("");
    fireEvent.click(screen.getByRole("button", { name: "公开" }));
    choose("不给谁看", "常用 · 对接人（个人分组）");
    expect(onChange).toHaveBeenLastCalledWith({ mode: "exclude", user_ids: [], group_ids: [8] });
  });

  it("保存失败保留所选人员并显示错误", async () => {
    const onChange = vi.fn();
    mocks.createAudienceGroup.mockRejectedValue(new Error("重复名称"));
    render(<AudiencePicker people={people} groups={groups} onChange={onChange} />);
    choose("谁可以看", "小杨");
    fireEvent.change(screen.getByRole("textbox", { name: "新分组名称" }), { target: { value: "重复" } });
    fireEvent.click(screen.getByRole("button", { name: "存为分组" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("分组保存失败");
    expect(onChange).toHaveBeenLastCalledWith({ mode: "include", user_ids: ["alice"], group_ids: [] });
  });

  it("常用分组按本机频率置顶，归档分组不能重新选择", () => {
    localStorage.setItem("audience:test-account", JSON.stringify({ 2: 5, 1: 1 }));
    render(<AudiencePicker people={people} groups={groups} preferenceKey="audience:test-account" />);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "谁可以看" }));
    const options = Array.from(document.querySelectorAll(".ant-select-item-option-content")).map((option) => option.textContent);
    expect(options.slice(0, 2)).toEqual(["常用 · 销售（个人分组）", "常用 · 采购（公共分组）"]);
    expect(options.join()).not.toContain("旧分组");
  });

  it("未传候选数据时从通用接口读取账号和分组", async () => {
    render(<AudiencePicker />);
    await waitFor(() => expect(mocks.listAudiencePeople).toHaveBeenCalledWith("", expect.any(AbortSignal)));
    await waitFor(() => expect(mocks.listAudienceGroups).toHaveBeenCalledWith(expect.any(AbortSignal)));
    choose("谁可以看", "小李");
    expect(screen.getByRole("combobox", { name: "谁可以看" }).closest(".ant-select")).toHaveTextContent("小李");
  });

  it("可按账号用户名搜索，不会因中文显示名不匹配而丢掉候选", () => {
    const onChange = vi.fn();
    render(<AudiencePicker people={people} groups={groups} onChange={onChange} />);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "谁可以看" }));
    fireEvent.change(screen.getByRole("combobox", { name: "谁可以看" }), { target: { value: "alice" } });
    fireEvent.click(screen.getByText("小杨", { selector: ".ant-select-item-option-content" }));
    expect(onChange).toHaveBeenLastCalledWith({ mode: "include", user_ids: ["alice"], group_ids: [] });
  });
});

describe("AudienceLock", () => {
  it("回读规则展示真实模式、人员与分组名称，打开设置不触发改写", async () => {
    const onChange = vi.fn();
    render(<AudienceLock label="备注" savedRule={{ mode: "exclude", users: [{ id: "not-in-results", name: "原销售" }],
      groups: [{ id: 99, name: "原分组" }] }} onChange={onChange} people={[]} groups={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "备注可见性：不给谁看" }));
    const select = await screen.findByRole("combobox", { name: "不给谁看" });
    expect(select.closest(".ant-select")).toHaveTextContent("原销售");
    expect(select.closest(".ant-select")).toHaveTextContent("原分组");
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "公开" }));
    expect(onChange).toHaveBeenLastCalledWith(PUBLIC_AUDIENCE);
    expect(screen.getByRole("button", { name: "备注可见性：公开" })).toBeInTheDocument();
  });

  it.each<[AudienceMode, string, string]>([
    ["public", "公开", "global"], ["private", "私密", "lock"],
    ["include", "部分可见", "team"], ["exclude", "不给谁看", "user-delete"],
  ])("%s 模式显示对应图标和可访问名称", (mode, label, icon) => {
    render(<AudienceLock label="备注" value={{ mode, user_ids: [], group_ids: [] }} people={people} groups={groups} />);
    const button = screen.getByRole("button", { name: `备注可见性：${label}` });
    expect(button).toHaveAttribute("data-mode", mode);
    expect(button.querySelector(`[aria-label="${icon}"]`)).toBeInTheDocument();
  });

  it("点击字段旁图标打开Picker，修改后图标同步", async () => {
    const onChange = vi.fn();
    render(<AudienceLock label="备注" onChange={onChange} people={people} groups={groups} />);
    fireEvent.click(screen.getByRole("button", { name: "备注可见性：公开" }));
    fireEvent.click(await screen.findByRole("switch", { name: "私密" }));
    expect(onChange).toHaveBeenLastCalledWith({ mode: "private", user_ids: [], group_ids: [] });
    expect(screen.getByRole("button", { name: "备注可见性：私密" })).toBeInTheDocument();
  });
});
