import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanupAntd, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import type { Offer } from "../../../api/presales";
import QuoteEditor, { offerInput } from "../QuoteEditor";

const mocks = vi.hoisted(() => ({ suppliers: vi.fn() }));
vi.mock("../../../api/presales", () => ({ presales: mocks }));
vi.mock("../../../api/audience", () => ({ listAudiencePeople: vi.fn(async () => []), listAudienceGroups: vi.fn(async () => []) }));
const offer: Offer = { id: 8, inquiry_id: 1, line_id: 2, part_id: 3, version: 4, revision_no: 1,
  status: "published", valid_from: null, valid_until: null, unit_price: "15", supplier_id: 9, notes: "原备注",
  reporter: { id: 7, name: "采购甲" },
  supplier_audience: { mode: "include", users: [{ id: "sale-a", name: "销售甲" }], groups: [{ id: 99, name: "原分组" }] },
  reporter_audience: { mode: "private", users: [], groups: [] },
  notes_audience: { mode: "exclude", users: [{ id: "sale-b", name: "销售乙" }], groups: [] } };

function Harness({ existing, onSave = () => {} }: { existing?: Offer; onSave?: (value: unknown) => void }) {
  const [value, setValue] = useState(() => offerInput(existing));
  return <><QuoteEditor label="PN" value={value} onChange={setValue} existing={existing} />
    <button onClick={() => onSave(value)}>保存</button></>;
}
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
  localStorage.setItem("role", "purchaser");
  localStorage.setItem("permissions", JSON.stringify({ action_presales_quote: true, data_supplier: true }));
  mocks.suppliers.mockResolvedValue([{ id: 9, name: "供应商甲" }]);
});
afterEach(cleanupAntd);

describe("供应商档案与受众规则", () => {
  it("带搜索选择供应商后提交档案 ID，候选使用专用接口", async () => {
    const save = vi.fn();
    render(<Harness onSave={save} />);
    const select = screen.getByRole("combobox", { name: "PN供应商" });
    await waitFor(() => expect(mocks.suppliers).toHaveBeenCalledWith("", expect.any(AbortSignal)));
    fireEvent.change(select, { target: { value: "供应商甲" } });
    await waitFor(() => expect(mocks.suppliers).toHaveBeenLastCalledWith("供应商甲", expect.any(AbortSignal)));
    fireEvent.mouseDown(select);
    fireEvent.click(await screen.findByText("供应商甲", { selector: ".ant-select-item-option-content" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ supplier_id: 9 }));
  });

  it.each([
    { action_presales_quote: true, data_supplier: false },
    { action_presales_quote: false, action_presales_offer: false, data_supplier: true },
  ])("字段不可见或没有报价动作时隐藏供应商并且不查候选：%j", (permissions) => {
    localStorage.setItem("permissions", JSON.stringify(permissions));
    render(<Harness />);
    expect(screen.queryByRole("combobox", { name: "PN供应商" })).not.toBeInTheDocument();
    expect(mocks.suppliers).not.toHaveBeenCalled();
  });

  it("只有主动报价动作也可以选择供应商", async () => {
    localStorage.setItem("permissions", JSON.stringify({ action_presales_offer: true, data_supplier: true }));
    render(<Harness />);
    expect(screen.getByRole("combobox", { name: "PN供应商" })).toBeInTheDocument();
    await waitFor(() => expect(mocks.suppliers).toHaveBeenCalled());
  });

  it("未回读规则的已有报价不伪装成公开，也不展示规则设置", () => {
    const other = { ...offer };
    delete other.supplier_audience; delete other.reporter_audience; delete other.notes_audience;
    render(<Harness existing={other} />);
    expect(screen.queryByRole("button", { name: /可见性：/ })).not.toBeInTheDocument();
  });

  it("回显三个真实锁规则和成员名称，仅改价格不提交受众规则", async () => {
    const save = vi.fn();
    render(<Harness existing={offer} onSave={save} />);
    expect(screen.getByRole("button", { name: "供应商可见性：部分可见" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "报价人可见性：私密" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "采购备注可见性：不给谁看" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "供应商可见性：部分可见" }));
    const select = await screen.findByRole("combobox", { name: "谁可以看" });
    expect(select.closest(".ant-select")).toHaveTextContent("销售甲");
    expect(select.closest(".ant-select")).toHaveTextContent("原分组");
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN单价" }), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    const value = save.mock.calls[0][0];
    expect(value.unit_price).toBe(20);
    for (const field of ["supplier_audience", "reporter_audience", "notes_audience"]) expect(value).not.toHaveProperty(field);
    fireEvent.click(screen.getByRole("switch", { name: "私密" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ supplier_audience: { mode: "private", user_ids: [], group_ids: [] } }));
  });
});
