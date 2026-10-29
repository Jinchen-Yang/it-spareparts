import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanupAntd, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import type { Offer } from "../../../api/presales";
import QuoteEditor, { offerInput } from "../QuoteEditor";

const mocks = vi.hoisted(() => ({ suppliers: vi.fn() }));
vi.mock("../../../api/presales", () => ({ presales: mocks }));
vi.mock("../../../api/audience", () => ({ listAudiencePeople: vi.fn(async () => []), listAudienceGroups: vi.fn(async () => []) }));
const offer: Offer = { id: 8, inquiry_id: 1, line_id: 2, part_id: 3, version: 4, revision_no: 1,
  status: "published", valid_from: null, unit_price: "15", supplier_id: 9, notes: "原备注",
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
  it("旧入口可选择成本价，已有成本报价只改单价也保留性质", async () => {
    const save = vi.fn();
    const view = render(<Harness onSave={save} />);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "PN价格性质" }));
    fireEvent.click(screen.getByText("成本价", { selector: ".ant-select-item-option-content" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN单价" }), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ unit_price: 20, price_basis: "cost" }));
    expect(save.mock.calls[0][0]).not.toHaveProperty("is_tax_inclusive");
    view.unmount();
    render(<Harness existing={{ ...offer, price_basis: "cost", is_tax_inclusive: null }} onSave={save} />);
    expect(screen.getByRole("combobox", { name: "PN价格性质" }).closest(".ant-select")).toHaveTextContent("成本价");
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN单价" }), { target: { value: "25" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ unit_price: 25, price_basis: "cost", is_tax_inclusive: null }));
    await waitFor(() => expect(mocks.suppliers).toHaveBeenCalled());
  });

  it("无成本权限的只读查看也显示报价性质，未声明的存量性质按售价显示", () => {
    localStorage.setItem("permissions", JSON.stringify({ data_cost: false }));
    const view = render(<QuoteEditor value={{ price_basis: "cost", unit_price: 20 }} onChange={() => {}} disabled />);
    expect(screen.getByText("成本价")).toBeVisible();
    view.rerender(<QuoteEditor value={{ unit_price: 20 }} onChange={() => {}} disabled />);
    expect(screen.getByText("售价")).toBeVisible();
  });

  it("旧入口新报价默认含税且不提供未声明选项", async () => {
    const save = vi.fn();
    render(<Harness onSave={save} />);
    const tax = screen.getByRole("combobox", { name: "PN含税口径" });
    expect(tax.closest(".ant-select")).toHaveTextContent("含税");
    fireEvent.mouseDown(tax);
    expect(screen.queryByText("未声明")).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN单价" }), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ unit_price: 20 }));
    expect(save.mock.calls[0][0]).not.toHaveProperty("is_tax_inclusive");
    expect(save.mock.calls[0][0]).not.toHaveProperty("price_basis");
    await waitFor(() => expect(mocks.suppliers).toHaveBeenCalled());
  });

  it("旧入口编辑存量未声明报价只改价格仍保留 null", async () => {
    const save = vi.fn();
    render(<Harness existing={{ ...offer, is_tax_inclusive: null }} onSave={save} />);
    expect(screen.getByRole("combobox", { name: "PN含税口径" }).closest(".ant-select")).toHaveTextContent("未声明");
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN单价" }), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ unit_price: 20, is_tax_inclusive: null }));
    await waitFor(() => expect(mocks.suppliers).toHaveBeenCalled());
  });

  it.each([true, null])("草稿税口径 %s 按现值回显，只改价格不把回显当作明确选择", async (tax) => {
    const save = vi.fn();
    render(<Harness existing={{ ...offer, status: "draft", is_tax_inclusive: tax, price_basis: "cost" }} onSave={save} />);
    expect(screen.getByRole("combobox", { name: "PN含税口径" }).closest(".ant-select")).toHaveTextContent(tax === null ? "未声明" : "含税");
    expect(screen.getByRole("combobox", { name: "PN价格性质" }).closest(".ant-select")).toHaveTextContent("成本价");
    fireEvent.change(screen.getByRole("spinbutton", { name: "PN单价" }), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(save.mock.calls[0][0]).not.toHaveProperty("is_tax_inclusive");
    expect(save.mock.calls[0][0]).not.toHaveProperty("price_basis");
    await waitFor(() => expect(mocks.suppliers).toHaveBeenCalled());
  });

  it.each(["new", "draft", "published"] as const)("%s 报价明确选择含税和售价，即使等于默认显示值也提交", async (status) => {
    const save = vi.fn();
    render(<Harness existing={status === "new" ? undefined : { ...offer, status,
      is_tax_inclusive: status === "draft" ? true : null, price_basis: status === "draft" ? "sale" : "cost" }} onSave={save} />);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "PN含税口径" }));
    fireEvent.click(screen.getByText("含税", { selector: ".ant-select-item-option-content" }));
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "PN价格性质" }));
    fireEvent.click(screen.getByText("售价", { selector: ".ant-select-item-option-content" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(save).toHaveBeenLastCalledWith(expect.objectContaining({ is_tax_inclusive: true, price_basis: "sale" }));
    await waitFor(() => expect(mocks.suppliers).toHaveBeenCalled());
  });

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
