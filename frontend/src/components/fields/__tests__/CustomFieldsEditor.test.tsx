import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CustomFieldsEditor from "../CustomFieldsEditor";
import type { FieldDefinition } from "../../../api/fields";

const mocks = vi.hoisted(() => ({ defineField: vi.fn(), listFieldDefinitions: vi.fn(), suggestFieldNames: vi.fn() }));
vi.mock("../../../api/fields", () => mocks);
vi.mock("../../../api/audience", () => ({ listAudiencePeople: vi.fn(), listAudienceGroups: vi.fn(), createAudienceGroup: vi.fn() }));

function definition(id = 1, name = "备注1", patch: Partial<FieldDefinition> = {}): FieldDefinition {
  return { id, owner_scope: "personal", owner_id: "alice", subject_type: "demo", level: "line",
    name, value_type: "text", choices: null, default_rule_id: null, default_rule: null,
    auto_include: true, sort: 0, archived: false, ...patch };
}
const template = [definition(), definition(2, "特殊要求", {
  default_rule_id: 9, default_rule: { mode: "private", user_ids: [], group_ids: [] },
})];
const pool = [...template, definition(3, "质保年限", { value_type: "number", auto_include: false })];
const audienceProps = { people: [], groups: [] };

beforeEach(() => {
  vi.clearAllMocks();
  mocks.listFieldDefinitions.mockResolvedValue(pool);
  mocks.suggestFieldNames.mockResolvedValue([{ field_def_id: 3, name: "质保年限", uses: 8 }]);
  mocks.defineField.mockResolvedValue(definition(10, "备注2"));
});
afterEach(cleanup);

describe("CustomFieldsEditor", () => {
  it("模板生成空白输入框，字段默认可见性体现在锁图标", () => {
    const onChange = vi.fn();
    render(<CustomFieldsEditor subjectType="demo" level="line" template={template}
      definitions={pool} onChange={onChange} audienceProps={audienceProps} />);
    expect(screen.getByRole("textbox", { name: "备注1字段值" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "特殊要求字段值" })).toHaveValue("");
    expect(screen.getByRole("button", { name: "备注1可见性：公开" })).toHaveAttribute("data-mode", "public");
    expect(screen.getByRole("button", { name: "特殊要求可见性：私密" })).toHaveAttribute("data-mode", "private");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("只输出有值的字段，清空后省略，不把模板空框提交", () => {
    const onChange = vi.fn();
    render(<CustomFieldsEditor subjectType="demo" level="line" template={template}
      definitions={pool} onChange={onChange} audienceProps={audienceProps} />);
    const input = screen.getByRole("textbox", { name: "备注1字段值" });
    fireEvent.change(input, { target: { value: "要求原包装" } });
    expect(onChange).toHaveBeenLastCalledWith([{ field_def_id: 1, value: "要求原包装" }]);
    fireEvent.change(input, { target: { value: "" } });
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it("字段名联想可以复用已有定义及其类型，不创建同名字段", async () => {
    const onChange = vi.fn();
    render(<CustomFieldsEditor subjectType="demo" level="line" template={template}
      onChange={onChange} audienceProps={audienceProps} />);
    fireEvent.click(screen.getByRole("button", { name: "新增字段" }));
    fireEvent.change(screen.getByRole("textbox", { name: "字段名称" }), { target: { value: "质保" } });
    fireEvent.click(await screen.findByRole("option", { name: "使用字段：质保年限" }));
    expect(mocks.listFieldDefinitions).toHaveBeenCalledWith("demo", "line", expect.any(AbortSignal));
    expect(mocks.suggestFieldNames).toHaveBeenCalledWith("demo", "质保", expect.any(AbortSignal));
    const number = screen.getByRole("spinbutton", { name: "质保年限字段值" });
    fireEvent.change(number, { target: { value: "0" } });
    expect(onChange).toHaveBeenLastCalledWith([{ field_def_id: 3, value: "0" }]);
    expect(onChange).not.toHaveBeenCalledWith(expect.arrayContaining([expect.objectContaining({ new_name: "质保年限" })]));
  });

  it("新名称即时保存个人池定义，空白新框不提交业务值", async () => {
    const onChange = vi.fn();
    render(<CustomFieldsEditor subjectType="demo" level="line" template={template}
      definitions={pool} onChange={onChange} audienceProps={audienceProps} />);
    fireEvent.click(screen.getByRole("button", { name: "新增字段" }));
    fireEvent.change(screen.getByRole("textbox", { name: "字段名称" }), { target: { value: " 备注2 " } });
    fireEvent.click(screen.getByRole("button", { name: "新建字段「备注2」" }));
    expect(mocks.defineField).toHaveBeenCalledWith({ subject_type: "demo", level: "line", name: "备注2",
      owner_scope: "personal", value_type: "text", auto_include: true });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.change(await screen.findByRole("textbox", { name: "备注2字段值" }), { target: { value: "补充说明" } });
    expect(onChange).toHaveBeenLastCalledWith([{ field_def_id: 10, value: "补充说明" }]);
  });

  it("字段定义创建失败时保留名称且不添加未保存的输入框", async () => {
    mocks.defineField.mockRejectedValue(new Error("重复名称"));
    const onChange = vi.fn();
    render(<CustomFieldsEditor subjectType="demo" level="line" template={template}
      definitions={pool} onChange={onChange} audienceProps={audienceProps} />);
    fireEvent.click(screen.getByRole("button", { name: "新增字段" }));
    fireEvent.change(screen.getByRole("textbox", { name: "字段名称" }), { target: { value: "备注2" } });
    fireEvent.click(screen.getByRole("button", { name: "新建字段「备注2」" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("字段创建失败");
    expect(screen.getByRole("textbox", { name: "字段名称" })).toHaveValue("备注2");
    expect(screen.queryByRole("textbox", { name: "备注2字段值" })).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("临时隐藏仅影响当前实例及输出，同一模板下次仍带出空框", () => {
    const onChange = vi.fn();
    const { unmount } = render(<CustomFieldsEditor subjectType="demo" level="line" template={template}
      definitions={pool} onChange={onChange} audienceProps={audienceProps} />);
    fireEvent.change(screen.getByRole("textbox", { name: "备注1字段值" }), { target: { value: "本单值" } });
    fireEvent.click(screen.getByRole("button", { name: "临时隐藏备注1" }));
    expect(screen.queryByRole("textbox", { name: "备注1字段值" })).toBeNull();
    expect(onChange).toHaveBeenLastCalledWith([]);
    expect(template).toHaveLength(2);
    unmount();
    render(<CustomFieldsEditor subjectType="demo" level="line" template={template}
      definitions={pool} audienceProps={audienceProps} />);
    expect(screen.getByRole("textbox", { name: "备注1字段值" })).toHaveValue("");
  });

  it("可以先设置空框的私密规则，填写时输出覆盖规则，空值仍省略", async () => {
    const onChange = vi.fn();
    render(<CustomFieldsEditor subjectType="demo" level="line" template={template}
      definitions={pool} onChange={onChange} audienceProps={audienceProps} />);
    fireEvent.click(screen.getByRole("button", { name: "备注1可见性：公开" }));
    fireEvent.click(await screen.findByRole("switch", { name: "私密" }));
    expect(onChange).toHaveBeenLastCalledWith([]);
    expect(screen.getByRole("button", { name: "备注1可见性：私密" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "备注1字段值" }), { target: { value: "内部说明" } });
    expect(onChange).toHaveBeenLastCalledWith([{ field_def_id: 1, value: "内部说明",
      rule: { mode: "private", user_ids: [], group_ids: [] } }]);
  });

  it("受控值回填，值自身规则优先于定义规则", () => {
    render(<CustomFieldsEditor subjectType="demo" level="line" template={template}
      definitions={pool} audienceProps={audienceProps} value={[{ field_def_id: 2, value: "已有说明",
        rule: { mode: "exclude", user_ids: ["bob"], group_ids: [] } }]} />);
    expect(screen.getByRole("textbox", { name: "特殊要求字段值" })).toHaveValue("已有说明");
    expect(screen.getByRole("button", { name: "特殊要求可见性：不给谁看" })).toHaveAttribute("data-mode", "exclude");
  });

  it("有值字段以父组件规则回填为准，拒绝或省略覆盖不会复活本地旧规则", async () => {
    const onChange = vi.fn();
    const base = { subjectType: "demo", level: "line" as const, template, definitions: pool, audienceProps, onChange };
    const { rerender } = render(<CustomFieldsEditor {...base}
      value={[{ field_def_id: 1, value: "原值" }]} />);
    fireEvent.click(screen.getByRole("button", { name: "备注1可见性：公开" }));
    fireEvent.click(await screen.findByRole("switch", { name: "私密" }));
    expect(onChange).toHaveBeenLastCalledWith([{ field_def_id: 1, value: "原值",
      rule: { mode: "private", user_ids: [], group_ids: [] } }]);
    // 父组件没有接受变更，受控字段仍沿用默认公开。
    expect(screen.getByRole("button", { name: "备注1可见性：公开" })).toBeInTheDocument();
    rerender(<CustomFieldsEditor {...base} value={[{ field_def_id: 1, value: "回填值",
      rule: { mode: "private", user_ids: [], group_ids: [] } }]} />);
    expect(screen.getByRole("button", { name: "备注1可见性：私密" })).toBeInTheDocument();
    rerender(<CustomFieldsEditor {...base} value={[{ field_def_id: 1, value: "回填值",
      rule: { mode: "public", user_ids: [], group_ids: [] } }]} />);
    expect(screen.getByRole("button", { name: "备注1可见性：公开" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "备注1字段值" }), { target: { value: "新值" } });
    expect(onChange).toHaveBeenLastCalledWith([{ field_def_id: 1, value: "新值",
      rule: { mode: "public", user_ids: [], group_ids: [] } }]);
    rerender(<CustomFieldsEditor {...base} value={[{ field_def_id: 1, value: "回填值" }]} />);
    fireEvent.change(screen.getByRole("textbox", { name: "备注1字段值" }), { target: { value: "再次修改" } });
    expect(onChange).toHaveBeenLastCalledWith([{ field_def_id: 1, value: "再次修改" }]);
    // 空框的私密草稿被接受后，父组件改成公开；清空并重填也必须保持最新公开。
    rerender(<CustomFieldsEditor {...base} value={[]} />);
    fireEvent.click(screen.getByRole("switch", { name: "私密" }));
    fireEvent.change(screen.getByRole("textbox", { name: "备注1字段值" }), { target: { value: "空框后填写" } });
    expect(onChange).toHaveBeenLastCalledWith([{ field_def_id: 1, value: "空框后填写",
      rule: { mode: "private", user_ids: [], group_ids: [] } }]);
    rerender(<CustomFieldsEditor {...base} value={[{ field_def_id: 1, value: "空框后填写",
      rule: { mode: "private", user_ids: [], group_ids: [] } }]} />);
    rerender(<CustomFieldsEditor {...base} value={[{ field_def_id: 1, value: "空框后填写",
      rule: { mode: "public", user_ids: [], group_ids: [] } }]} />);
    fireEvent.change(screen.getByRole("textbox", { name: "备注1字段值" }), { target: { value: "" } });
    expect(onChange).toHaveBeenLastCalledWith([]);
    rerender(<CustomFieldsEditor {...base} value={[]} />);
    fireEvent.change(screen.getByRole("textbox", { name: "备注1字段值" }), { target: { value: "重填" } });
    expect(onChange).toHaveBeenLastCalledWith([{ field_def_id: 1, value: "重填",
      rule: { mode: "public", user_ids: [], group_ids: [] } }]);
  });

  it("布尔和单选保留false，日期按字符串输出", () => {
    const onChange = vi.fn();
    const typed = [definition(5, "是否加急", { value_type: "bool" }),
      definition(6, "交期", { value_type: "date" }),
      definition(7, "包装", { value_type: "choice", choices: ["原装", "散装"] })];
    render(<CustomFieldsEditor subjectType="demo" level="line" template={typed} definitions={typed}
      onChange={onChange} audienceProps={audienceProps} />);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "是否加急字段值" }));
    fireEvent.click(screen.getByText("否", { selector: ".ant-select-item-option-content" }));
    expect(onChange).toHaveBeenLastCalledWith([{ field_def_id: 5, value: "false" }]);
    fireEvent.change(screen.getByLabelText("交期字段值"), { target: { value: "2026-11-10" } });
    expect(onChange).toHaveBeenLastCalledWith([{ field_def_id: 5, value: "false" }, { field_def_id: 6, value: "2026-11-10" }]);
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "包装字段值" }));
    fireEvent.click(screen.getByText("原装", { selector: ".ant-select-item-option-content" }));
    expect(onChange).toHaveBeenLastCalledWith(expect.arrayContaining([{ field_def_id: 7, value: "原装" }]));
  });

  it("20个字段达到上限；字段池加载失败时不冒险新建重复名称", async () => {
    const twenty = Array.from({ length: 20 }, (_, index) => definition(index + 1, `字段${index + 1}`));
    const { unmount } = render(<CustomFieldsEditor subjectType="demo" level="line" template={twenty}
      definitions={twenty} audienceProps={audienceProps} />);
    expect(screen.getByRole("button", { name: "新增字段" })).toBeDisabled();
    unmount();
    mocks.listFieldDefinitions.mockRejectedValue(new Error("offline"));
    render(<CustomFieldsEditor subjectType="demo" level="line" template={template} audienceProps={audienceProps} />);
    fireEvent.click(screen.getByRole("button", { name: "新增字段" }));
    fireEvent.change(screen.getByRole("textbox", { name: "字段名称" }), { target: { value: "备注3" } });
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("字段池加载失败"));
    expect(screen.getByRole("button", { name: "新建字段「备注3」" })).toBeDisabled();
  });
});
