import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MaintenanceRowField } from "../../../api/maintenanceRowFilters";
import { cleanupAntd, renderWithoutMotion } from "../../../test/antdRender";
import MaintenanceRowFilterBuilder from "../MaintenanceRowFilterBuilder";

const fields: MaintenanceRowField[] = [
  { field: "pn_std", label: "PN", type: "text", operators: ["contains", "eq", "prefix", "is_empty"] },
  { field: "qty", label: "需求数量", type: "number", operators: ["eq", "range", "is_empty"] },
  { field: "order_date", label: "业务日期", type: "date", operators: ["eq", "range", "is_empty"] },
  { field: "cost_source", label: "成本来源", type: "enum", operators: ["in", "is_empty"],
    options: [{ value: "direct", label: "系统关联" }, { value: "manual", label: "人工回填" }] },
];
async function choose(label: string, text: string) {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: label }));
  const option = (await screen.findAllByText(text))
    .map((node) => node.closest(".ant-select-item-option"))
    .find((node) => node && !node.closest(".ant-select-dropdown-hidden"));
  fireEvent.click(option!);
}
afterEach(cleanupAntd);

describe("行级类型化筛选构建器", () => {
  it("组合数值闭区间、日期单边范围及枚举多值，清空同步提交空集", async () => {
    const onApply = vi.fn();
    renderWithoutMotion(<MaintenanceRowFilterBuilder fields={fields} onApply={onApply} />);
    fireEvent.click(screen.getByRole("button", { name: "添加筛选条件" }));
    await choose("条件 1 的字段", "需求数量");
    await choose("条件 1 的运算", "范围（含边界）");
    fireEvent.change(screen.getByRole("spinbutton", { name: "条件 1 的最小值" }), { target: { value: "0" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "条件 1 的最大值" }), { target: { value: "10.5" } });

    fireEvent.click(screen.getByRole("button", { name: "添加筛选条件" }));
    await choose("条件 2 的字段", "业务日期");
    await choose("条件 2 的运算", "范围（含边界）");
    fireEvent.change(screen.getByLabelText("条件 2 的最小值"), { target: { value: "2026-10-01" } });

    fireEvent.click(screen.getByRole("button", { name: "添加筛选条件" }));
    await choose("条件 3 的字段", "成本来源");
    await choose("条件 3 的值", "系统关联");
    await choose("条件 3 的值", "人工回填");
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(onApply).toHaveBeenLastCalledWith([
      { field: "qty", op: "range", min: "0", max: "10.5" },
      { field: "order_date", op: "range", min: "2026-10-01" },
      { field: "cost_source", op: "in", value: ["direct", "manual"] },
    ]);
    fireEvent.click(screen.getByRole("button", { name: "清空筛选" }));
    expect(onApply).toHaveBeenLastCalledWith([]);
    expect(screen.queryByLabelText("条件 1 的字段")).not.toBeInTheDocument();
  });

  it("空范围阻止提交；为空运算不残留上一个值；仅显示服务端授权字段", async () => {
    const onApply = vi.fn();
    renderWithoutMotion(<MaintenanceRowFilterBuilder fields={fields.slice(0, 2)} onApply={onApply} />);
    fireEvent.click(screen.getByRole("button", { name: "添加筛选条件" }));
    await choose("条件 1 的字段", "需求数量");
    await choose("条件 1 的运算", "范围（含边界）");
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(screen.getByText("范围至少填写一个边界")).toBeInTheDocument();
    expect(onApply).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole("spinbutton", { name: "条件 1 的最小值" }), { target: { value: "3" } });
    await choose("条件 1 的运算", "为空");
    fireEvent.click(screen.getByRole("button", { name: "应用筛选" }));
    expect(onApply).toHaveBeenLastCalledWith([{ field: "qty", op: "is_empty" }]);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "条件 1 的字段" }));
    expect(screen.queryByText("成本来源")).not.toBeInTheDocument();
  });
});
