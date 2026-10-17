// 金额口径多选（D-39 / Q7）：可选一项或多项并排显示，但至少保留一项（全清 = 无效操作，
// 不回调）；hidden 账号不渲染；值集外的野值被过滤展示。
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import AmountBasisSelect from "../AmountBasisSelect";

const OPTIONS = [
  { value: "inc", label: "含税" },
  { value: "ex", label: "不含税" },
  { value: "paid", label: "实付" },
];

afterEach(cleanup);

function openAndDeselect(label: string) {
  // antd multiple Select：下拉里点已选项 = 取消选择（jsdom 下比点标签 × 稳）
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "金额口径" }));
  fireEvent.click(screen.getByText(label, { selector: ".ant-select-item-option-content" }));
}

describe("AmountBasisSelect（口径多选，Q7）", () => {
  it("hidden=true 直接不渲染（金额不可见账号）", () => {
    const { container } = render(
      <AmountBasisSelect options={OPTIONS} value={["inc"]} onChange={() => {}} hidden />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("默认标签为「金额口径」；自定义标签用于销售分析等复用场景", () => {
    render(<AmountBasisSelect options={OPTIONS} value={["inc"]} onChange={() => {}} />);
    expect(screen.getByRole("combobox", { name: "金额口径" })).toBeInTheDocument();
    cleanup();
    render(
      <AmountBasisSelect
        options={OPTIONS} value={["inc"]} onChange={() => {}} label="金额列"
      />,
    );
    expect(screen.getByRole("combobox", { name: "金额列" })).toBeInTheDocument();
  });

  it("值集外的野值不展示（脏 URL 不产生幽灵选项）", () => {
    render(
      <AmountBasisSelect options={OPTIONS} value={["inc", "gold"]} onChange={() => {}} />,
    );
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "金额口径" }));
    // 下拉只列合法选项；已选标记只落在合法值上
    const selected = Array.from(document.querySelectorAll(".ant-select-item-option-selected"))
      .map((el) => el.textContent);
    expect(selected).toEqual(["含税"]);
    expect(
      screen.queryByText("gold", { selector: ".ant-select-item-option-content" }),
    ).toBeNull();
  });

  it("多选到单项：保留剩余项并回调", () => {
    const onChange = vi.fn();
    render(
      <AmountBasisSelect options={OPTIONS} value={["inc", "ex"]} onChange={onChange} />,
    );
    openAndDeselect("含税");
    expect(onChange).toHaveBeenCalledWith(["ex"]);
  });

  it("至少保留一项：清空最后一项是无操作（不回调，不进入「没有金额列」状态）", () => {
    const onChange = vi.fn();
    render(
      <AmountBasisSelect options={OPTIONS} value={["paid"]} onChange={onChange} />,
    );
    openAndDeselect("实付");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("从下拉追加选项：回调携带完整新集合", () => {
    const onChange = vi.fn();
    render(
      <AmountBasisSelect options={OPTIONS} value={["inc"]} onChange={onChange} />,
    );
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "金额口径" }));
    fireEvent.click(
      screen.getByText("实付", { selector: ".ant-select-item-option-content" }),
    );
    expect(onChange).toHaveBeenCalledWith(["inc", "paid"]);
  });
});
