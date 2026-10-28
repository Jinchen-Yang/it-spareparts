import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanupAntd, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import PartRegistrationForm from "../PartRegistrationForm";
import CreatePartModal from "../CreatePartModal";
import { buildSpecUpdates } from "../partCreate";
import type { MasterSpecField } from "../../../api";

const mocks = vi.hoisted(() => ({ categories: vi.fn(), check: vi.fn(), create: vi.fn(), suggest: vi.fn() }));
vi.mock("../../../api", () => ({ masterCategories: mocks.categories, masterCheck: mocks.check, masterCreate: mocks.create }));
vi.mock("../../../api/search", () => ({ suggestParts: mocks.suggest }));
const fields: MasterSpecField[] = [{ key: "capacity", label: "容量", unit: "GB" }, { key: "interface", label: "接口", unit: null }];
const categories = [{ code: "05", name: "CPU", children: [{ code: "0501", name: "Intel至强" }] },
  { code: "04", name: "板卡", children: [{ code: "0404", name: "显卡GPU" }] }];
beforeEach(() => {
  vi.clearAllMocks();
  mocks.categories.mockResolvedValue({ data: { categories, spec_fields: fields } });
  mocks.check.mockResolvedValue({ data: { near_duplicates: [] } });
  mocks.create.mockResolvedValue({ data: { created: true, id: 125, pn_std: "NEW-GPU" } });
  mocks.suggest.mockResolvedValue([]);
});
afterEach(cleanupAntd);

async function select(label: string, option: string) {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: label }));
  fireEvent.click(await screen.findByText(option, { selector: ".ant-select-item-option-content" }));
}
async function checked() {
  fireEvent.click(screen.getByRole("button", { name: /查\s*重/ }));
  const button = await screen.findByRole("button", { name: "新建并加入当前单据" });
  await waitFor(() => expect(button).toBeEnabled());
  return button;
}

describe("售前 PN 就地建档", () => {
  it("复用主数据类别、保存完整资料和自由参数，带真实ID加入当前单据", async () => {
    const selected = vi.fn(); const close = vi.fn();
    render(<CreatePartModal pn="new-gpu" onSelected={selected} onClose={close} />);
    expect(mocks.create).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("new-gpu建档描述"), { target: { value: "GPU 完整描述" } });
    fireEvent.change(screen.getByLabelText("new-gpu建档简介"), { target: { value: "运算加速卡" } });
    fireEvent.change(screen.getByLabelText("new-gpu建档品牌"), { target: { value: "品牌甲" } });
    fireEvent.change(screen.getByLabelText("new-gpu建档制造商"), { target: { value: "制造商乙" } });
    await waitFor(() => expect(screen.getByRole("combobox", { name: "new-gpu建档一级类别" })).toBeEnabled());
    await select("new-gpu建档一级类别", "板卡");
    await select("new-gpu建档二级类别", "显卡GPU");
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    fireEvent.change(screen.getByRole("combobox", { name: "参数 1 名称" }), { target: { value: "显存类型" } });
    fireEvent.change(screen.getByLabelText("参数 1 值"), { target: { value: "HBM3" } });
    fireEvent.click(await checked());
    await waitFor(() => expect(selected).toHaveBeenCalledWith(expect.objectContaining({ part_id: 125, pn_std: "NEW-GPU", category_minor: "显卡GPU", key_specs: [{ label: "显存类型", value: "HBM3" }] })));
    expect(mocks.create).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ pn_std: "new-gpu", description: "GPU 完整描述", summary: "运算加速卡", brand: "品牌甲", manufacturer: "制造商乙", category_major: "板卡", category_minor: "显卡GPU", spec_updates: { "custom:显存类型": { value: "HBM3", unit: null, numeric_value: null } } }));
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("参数不完整或重名不发请求，删除后可以保存", async () => {
    render(<CreatePartModal pn="NEW" onSelected={vi.fn()} onClose={vi.fn()} />);
    const save = await checked();
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    fireEvent.change(screen.getByRole("combobox", { name: "参数 1 名称" }), { target: { value: "核心数" } });
    fireEvent.click(save);
    expect(await screen.findByText(/每个参数都需要填写名称和值/)).toBeVisible();
    expect(mocks.create).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "删除参数 1" }));
    fireEvent.click(save);
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
  });

  it("失败保留填写内容和诊断ID；重试时用同一份资料", async () => {
    mocks.create.mockRejectedValueOnce({ config: { method: "post", url: "/parts/master", baseURL: "/api" }, response: { status: 503, headers: { "x-request-id": "test-request-123" }, data: { detail: "private db detail" } } });
    render(<CreatePartModal pn="NEW" onSelected={vi.fn()} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("NEW建档简介"), { target: { value: "保留这段简介" } });
    fireEvent.click(await checked());
    expect(await screen.findByText("服务暂时不可用")).toBeVisible();
    expect(screen.getByLabelText("NEW建档简介")).toHaveValue("保留这段简介");
    expect(screen.getByText(/请求标识: test-request-123/)).toBeInTheDocument();
    expect(screen.queryByText("private db detail")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "新建并加入当前单据" }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(2));
    expect(mocks.create.mock.calls[0][0]).toEqual(mocks.create.mock.calls[1][0]);
  });

  it("改动PN后必须重新查重，类别变化清空原二级类别", async () => {
    render(<CreatePartModal pn="NEW" onSelected={vi.fn()} onClose={vi.fn()} />);
    await checked();
    await select("NEW建档一级类别", "板卡"); await select("NEW建档二级类别", "显卡GPU");
    await select("NEW建档一级类别", "CPU");
    expect(screen.getByRole("combobox", { name: "NEW建档二级类别" })).toHaveValue("");
    fireEvent.change(screen.getByLabelText("NEW建档 PN"), { target: { value: "CHANGED" } });
    expect(screen.queryByRole("button", { name: "新建并加入当前单据" })).toBeNull();
    fireEvent.click(await checked());
    await waitFor(() => expect(mocks.create).toHaveBeenCalledWith(expect.objectContaining({ pn_std: "CHANGED", category_major: "CPU", category_minor: null })));
  });

  it("连续点按只提交一次，类别加载失败可重试且不丢输入", async () => {
    mocks.categories.mockRejectedValueOnce(new Error("offline"));
    let done!: () => void;
    const onCreate = vi.fn(() => new Promise<void>((resolve) => { done = resolve; }));
    render(<PartRegistrationForm initialPn="NEW" submitLabel="新建并加入当前单据" onCheck={async () => []} onCreate={onCreate} onSelect={vi.fn()} />);
    await screen.findByText("主数据类别与参数选项加载失败，填写内容已保留。");
    fireEvent.change(screen.getByLabelText("NEW建档描述"), { target: { value: "保留描述" } });
    fireEvent.click(screen.getByRole("button", { name: "重试加载" }));
    const save = await checked();
    fireEvent.click(save); fireEvent.click(save);
    expect(onCreate).toHaveBeenCalledTimes(1);
    expect(onCreate.mock.calls[0]).toEqual(expect.arrayContaining([expect.objectContaining({ description: "保留描述" })]));
    await act(async () => done());
  });
});

describe("参数键值契约", () => {
  it("标准单位用十进制精确转换，自由参数不伪造单位数值", () => {
    expect(buildSpecUpdates([{ id: 1, name: "容量", value: "1.2TB" }, { id: 2, name: "核心数", value: "96" }], fields)).toEqual({ capacity: { value: "1.2TB", unit: "GB", numeric_value: "1200" }, "custom:核心数": { value: "96", unit: null, numeric_value: null } });
  });
  it("同标准键的中英文名称不能重复", () => {
    expect(() => buildSpecUpdates([{ id: 1, name: "容量", value: "1TB" }, { id: 2, name: "capacity", value: "2TB" }], fields)).toThrow(/重复/);
  });
});
