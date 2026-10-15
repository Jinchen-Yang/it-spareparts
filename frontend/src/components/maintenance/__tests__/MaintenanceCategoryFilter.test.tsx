import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MaintenanceCategoryFilterInput } from "../../../api/maintenanceOperations";
const mocks = vi.hoisted(() => ({ categories: vi.fn() }));
vi.mock("../../../api/maintenanceOperations", () => ({ listMaintenancePartCategories: mocks.categories }));
import MaintenanceCategoryFilter from "../MaintenanceCategoryFilter";

function Harness() {
  const [value, setValue] = useState<MaintenanceCategoryFilterInput>({});
  return <><MaintenanceCategoryFilter value={value} onChange={setValue} labelPrefix="领用" /><output>{JSON.stringify(value)}</output></>;
}
beforeEach(() => {
  mocks.categories.mockReset();
  mocks.categories.mockResolvedValue({ data: { categories: [{ major: " 硬盘 ", minors: ["SAS", "NVMe"] }, { major: "内存", minors: ["DDR4"] }] } });
});
afterEach(cleanup);

describe("当前主数据库品类筛选", () => {
  it("未分类可叠加大类，未关联主数据清除品类且不依赖字典加载成功", async () => {
    mocks.categories.mockRejectedValueOnce(new Error("offline"));
    render(<Harness />);
    await screen.findByText(/品类选项加载失败/);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "领用主数据分类状态" }));
    fireEvent.click(await screen.findByText("未分类", { selector: ".ant-select-item-option-content" }));
    expect(screen.getByRole("status")).toHaveTextContent('"category_state":"unclassified"');
    fireEvent.click(screen.getByRole("button", { name: "重试加载品类" }));
    await waitFor(() => expect(screen.queryByText(/品类选项加载失败/)).not.toBeInTheDocument());
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "领用一级品类" }));
    fireEvent.click(await screen.findByText("硬盘", { selector: ".ant-select-item-option-content" }));
    expect(screen.getByRole("status")).toHaveTextContent('"category_major":" 硬盘 "');
    expect(screen.getByRole("combobox", { name: "领用二级品类" })).toBeDisabled();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "领用主数据分类状态" }));
    fireEvent.click(await screen.findByText("未关联主数据", { selector: ".ant-select-item-option-content" }));
    expect(screen.getByRole("status")).toHaveTextContent('{"category_state":"unlinked"}');
    expect(screen.getByRole("combobox", { name: "领用一级品类" })).toBeDisabled();
  });
  it("两级联动保留字典原文本，切换大类清除小类", async () => {
    render(<Harness />);
    await waitFor(() => expect(mocks.categories).toHaveBeenCalledTimes(1));
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "领用一级品类" }));
    fireEvent.click(await screen.findByText("硬盘", { exact: true, selector: ".ant-select-item-option-content" }));
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "领用二级品类" }));
    fireEvent.click(await screen.findByText("SAS", { exact: true, selector: ".ant-select-item-option-content" }));
    expect(screen.getByRole("status")).toHaveTextContent('"category_major":" 硬盘 "');
    expect(screen.getByRole("status")).toHaveTextContent('"category_minor":"SAS"');
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "领用一级品类" }));
    fireEvent.click(await screen.findByText("内存", { exact: true, selector: ".ant-select-item-option-content" }));
    expect(screen.getByRole("status")).toHaveTextContent('{"category_major":"内存"}');
  });
});
