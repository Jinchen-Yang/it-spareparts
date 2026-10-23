import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import CheckboxMultiSelect from "../CheckboxMultiSelect";

const options = [
  { value: "a", label: "张三", description: "10 单" },
  { value: "b", label: "李四", description: "3 单" },
  { value: "c", label: "王五", disabled: true },
];

afterEach(cleanup);

function openPanel() {
  fireEvent.click(screen.getByRole("button", { name: "销售人员" }));
  return within(screen.getByRole("dialog", { name: "销售人员筛选" }));
}

describe("CheckboxMultiSelect", () => {
  it("keeps selection local until Confirm, commits once and restores trigger focus", () => {
    const onChange = vi.fn();
    render(<CheckboxMultiSelect label="销售人员" options={options} value={["a"]} onChange={onChange} selectionUnit="人" />);
    const trigger = screen.getByRole("button", { name: "销售人员" });
    expect(trigger).toHaveTextContent("张三");
    expect(trigger).not.toHaveTextContent("10 单");
    const panel = openPanel();
    fireEvent.click(panel.getByRole("checkbox", { name: "李四" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(trigger).toHaveTextContent("张三");
    fireEvent.click(panel.getByRole("button", { name: "确定" }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith(["a", "b"]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("retains selections across searches and shows complete selected names", () => {
    const onChange = vi.fn();
    render(<CheckboxMultiSelect label="销售人员" options={options} value={["a"]} onChange={onChange} />);
    const panel = openPanel();
    const search = panel.getByRole("textbox", { name: "搜索销售人员" });
    fireEvent.change(search, { target: { value: "李" } });
    expect(panel.queryByRole("checkbox", { name: "张三" })).not.toBeInTheDocument();
    expect(panel.getByRole("button", { name: "移除张三" })).toBeInTheDocument();
    fireEvent.click(panel.getByRole("checkbox", { name: "李四" }));
    fireEvent.change(search, { target: { value: "张" } });
    expect(panel.getByRole("checkbox", { name: "张三" })).toBeChecked();
    expect(panel.getByRole("button", { name: "移除李四" })).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(panel.getByRole("button", { name: "确定" }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith(["a", "b"]);
  });

  it("Reset clears draft and search; Escape cancels it, while Confirm commits empty selection", () => {
    const onChange = vi.fn();
    render(<CheckboxMultiSelect label="销售人员" options={options} value={["a"]} onChange={onChange} />);
    let panel = openPanel();
    const search = panel.getByRole("textbox", { name: "搜索销售人员" });
    fireEvent.change(search, { target: { value: "李" } });
    fireEvent.click(panel.getByRole("button", { name: "重置" }));
    expect(search).toHaveValue("");
    expect(panel.getByRole("checkbox", { name: "张三" })).not.toBeChecked();
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.keyDown(search, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    panel = openPanel();
    expect(panel.getByRole("checkbox", { name: "张三" })).toBeChecked();
    fireEvent.click(panel.getByRole("button", { name: "重置" }));
    fireEvent.click(panel.getByRole("button", { name: "确定" }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith([]);
  });

  it("outside click abandons unconfirmed choices and query", () => {
    const onChange = vi.fn();
    render(<><button>外部</button><CheckboxMultiSelect label="销售人员" options={options} value={["a"]} onChange={onChange} /></>);
    const panel = openPanel();
    fireEvent.click(panel.getByRole("checkbox", { name: "李四" }));
    fireEvent.change(panel.getByRole("textbox"), { target: { value: "李" } });
    const outside = screen.getByRole("button", { name: "外部" });
    fireEvent.mouseDown(outside);
    fireEvent.click(outside);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const reopened = openPanel();
    expect(reopened.getByRole("checkbox", { name: "张三" })).toBeChecked();
    expect(reopened.getByRole("checkbox", { name: "李四" })).not.toBeChecked();
    expect(reopened.getByRole("textbox")).toHaveValue("");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("external value changes cancel stale draft, including a parent reset", () => {
    const onChange = vi.fn();
    const { rerender } = render(<CheckboxMultiSelect label="销售人员" options={options} value={["a"]} onChange={onChange} />);
    let panel = openPanel();
    fireEvent.click(panel.getByRole("checkbox", { name: "李四" }));
    rerender(<CheckboxMultiSelect label="销售人员" options={options} value={[]} onChange={onChange} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    panel = openPanel();
    expect(panel.getByRole("checkbox", { name: "张三" })).not.toBeChecked();
    expect(panel.getByRole("checkbox", { name: "李四" })).not.toBeChecked();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("option refresh and equivalent value arrays do not discard an open draft", () => {
    const onChange = vi.fn();
    const { rerender } = render(<CheckboxMultiSelect label="销售人员" options={options} value={["a"]} onChange={onChange} />);
    const panel = openPanel();
    fireEvent.click(panel.getByRole("checkbox", { name: "李四" }));
    rerender(<CheckboxMultiSelect label="销售人员" options={[{ value: "a", label: "张三" }]} value={["a"]} onChange={onChange} />);
    const updated = within(screen.getByRole("dialog"));
    expect(updated.getByRole("checkbox", { name: "b" })).toBeChecked();
    fireEvent.click(updated.getByRole("button", { name: "确定" }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith(["a", "b"]);
  });

  it("preserves missing selected values and allows removing them", () => {
    const onChange = vi.fn();
    render(<CheckboxMultiSelect label="销售人员" options={options} value={["历史销售"]} onChange={onChange} />);
    expect(screen.getByRole("button", { name: "销售人员" })).toHaveTextContent("历史销售");
    const panel = openPanel();
    expect(panel.getByRole("checkbox", { name: "历史销售" })).toBeChecked();
    fireEvent.click(panel.getByRole("button", { name: "移除历史销售" }));
    expect(panel.getByRole("checkbox", { name: "历史销售" })).not.toBeChecked();
    fireEvent.click(panel.getByRole("checkbox", { name: "历史销售" }));
    expect(panel.getByRole("checkbox", { name: "历史销售" })).toBeChecked();
    fireEvent.click(panel.getByRole("checkbox", { name: "历史销售" }));
    fireEvent.click(panel.getByRole("button", { name: "确定" }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith([]);
  });

  it("keeps numeric and string values distinct", () => {
    const onChange = vi.fn();
    render(<CheckboxMultiSelect<string | number> label="销售人员" options={[
      { value: 1, label: "数值一" }, { value: "1", label: "文本一" },
    ]} value={[1]} onChange={onChange} />);
    const panel = openPanel();
    expect(panel.getByRole("checkbox", { name: "数值一" })).toBeChecked();
    expect(panel.getByRole("checkbox", { name: "文本一" })).not.toBeChecked();
    fireEvent.click(panel.getByRole("checkbox", { name: "文本一" }));
    fireEvent.click(panel.getByRole("checkbox", { name: "数值一" }));
    fireEvent.click(panel.getByRole("button", { name: "确定" }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith(["1"]);
  });

  it("disables unavailable choices and cancels a draft when the entire control is disabled", () => {
    const onChange = vi.fn();
    const { rerender } = render(<CheckboxMultiSelect label="销售人员" options={options} value={["a"]} onChange={onChange} />);
    const panel = openPanel();
    expect(panel.getByRole("checkbox", { name: "王五" })).toBeDisabled();
    fireEvent.click(panel.getByRole("checkbox", { name: "李四" }));
    rerender(<CheckboxMultiSelect label="销售人员" options={options} value={["a"]} onChange={onChange} disabled />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "销售人员" })).toBeDisabled();
    rerender(<CheckboxMultiSelect label="销售人员" options={options} value={["a"]} onChange={onChange} />);
    expect(openPanel().getByRole("checkbox", { name: "李四" })).not.toBeChecked();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("opens from keyboard, focuses search, cycles Tab, and restores focus on Escape", async () => {
    render(<CheckboxMultiSelect label="销售人员" options={options} value={[]} onChange={() => {}} />);
    const trigger = screen.getByRole("button", { name: "销售人员" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    const panel = within(screen.getByRole("dialog"));
    const search = panel.getByRole("textbox");
    await waitFor(() => expect(search).toHaveFocus());
    fireEvent.keyDown(search, { key: "Tab", shiftKey: true });
    expect(panel.getByRole("button", { name: "确定" })).toHaveFocus();
    fireEvent.keyDown(panel.getByRole("button", { name: "确定" }), { key: "Tab" });
    expect(search).toHaveFocus();
    fireEvent.keyDown(search, { key: "Escape" });
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("shows full single label, multi selection count, and no-match state without losing selection", () => {
    const longLabel = "姓名很长也需要完整显示的销售人员";
    const { rerender } = render(<CheckboxMultiSelect label="销售人员" options={[{ value: "a", label: longLabel }]} value={["a"]} onChange={() => {}} selectionUnit="人" />);
    const trigger = screen.getByRole("button", { name: "销售人员" });
    expect(trigger).toHaveTextContent(longLabel);
    rerender(<CheckboxMultiSelect label="销售人员" options={options} value={["a", "b"]} onChange={() => {}} selectionUnit="人" />);
    expect(trigger).toHaveTextContent("已选 2 人");
    expect(trigger).toHaveAttribute("title", "张三、李四");
    const panel = openPanel();
    fireEvent.change(panel.getByRole("textbox"), { target: { value: "不存在" } });
    expect(panel.getByText("无匹配结果")).toBeInTheDocument();
    expect(panel.getByRole("button", { name: "移除张三" })).toBeInTheDocument();
    expect(panel.getByRole("button", { name: "移除李四" })).toBeInTheDocument();
  });
});

it("remote candidate pages retain known selected labels without applying a second local search filter", () => {
  const onSearch = vi.fn(); const onChange = vi.fn();
  const props = { label: "客户", value: [53], onChange, onSearch, knownOptions: [{ value: 53, label: "不在候选页的客户" }] };
  const view = render(<CheckboxMultiSelect {...props} options={[{ value: 1, label: "首屏客户" }]} />);
  fireEvent.click(screen.getByRole("button", { name: "客户" }));
  let panel = within(screen.getByRole("dialog", { name: "客户筛选" }));
  expect(onSearch).toHaveBeenLastCalledWith("");
  expect(panel.getByRole("button", { name: "移除不在候选页的客户" })).toBeInTheDocument();
  fireEvent.change(panel.getByRole("textbox"), { target: { value: "服务端别名" } });
  expect(onSearch).toHaveBeenLastCalledWith("服务端别名");
  view.rerender(<CheckboxMultiSelect {...props} options={[{ value: 2, label: "别名匹配的规范客户名" }]} />);
  panel = within(screen.getByRole("dialog", { name: "客户筛选" }));
  fireEvent.click(panel.getByRole("checkbox", { name: "别名匹配的规范客户名" }));
  fireEvent.click(panel.getByRole("button", { name: "确定" }));
  expect(onChange).toHaveBeenCalledWith([53, 2]);
});
