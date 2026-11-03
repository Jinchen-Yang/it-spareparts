import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { cleanupAntd, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import * as audience from "../../../api/audience";
import { presales } from "../../../api/presales";
import { presalesPool } from "../../../api/presalesPool";
import * as search from "../../../api/search";
import InquiryVisibilityDialog from "../InquiryVisibility";
import NewInquiryPanel from "../NewInquiryPanel";

beforeEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.spyOn(audience, "listAudiencePeople").mockResolvedValue([{ id: "sales_b", display_name: "销售乙" }]);
});
afterEach(cleanupAntd);

function showDialog() {
  const onSaved = vi.fn();
  const onDenied = vi.fn();
  const onClose = vi.fn();
  render(<InquiryVisibilityDialog inquiryId={9} inquiryNo="XJ9" onSaved={onSaved} onDenied={onDenied} onClose={onClose} />);
  return { onSaved, onDenied, onClose };
}

describe("询价额外可见人员", () => {
  it("按回读版本保存具体人员，空选择可保存", async () => {
    vi.spyOn(presales, "getInquiryVisibility").mockResolvedValue({ version: 7, visible_to: [] });
    const save = vi.spyOn(presales, "updateInquiryVisibility").mockResolvedValue({ version: 8, visible_to: [] });
    const { onSaved } = showDialog();
    const button = screen.getByRole("button", { name: "保存可见人员" });
    await waitFor(() => expect(button).toBeEnabled());
    await waitFor(() => expect(screen.getByText(/草稿仅本人和管理员可见/)).toBeVisible());
    fireEvent.click(button);
    await waitFor(() => expect(save).toHaveBeenCalledWith(9, { expected_version: 7, visible_to: [] }));
    expect(onSaved).toHaveBeenCalledOnce();
  });

  it("保留回读人员名称并可搜索选择额外销售", async () => {
    vi.spyOn(presales, "getInquiryVisibility").mockResolvedValue({ version: 4, visible_to: [{ id: "old_sales", name: "旧销售" }] });
    const save = vi.spyOn(presales, "updateInquiryVisibility").mockResolvedValue({ version: 5, visible_to: [] });
    showDialog();
    await waitFor(() => expect(screen.getByText("旧销售")).toBeVisible());
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "额外可见人员" }));
    fireEvent.click(await screen.findByText("销售乙", { selector: ".ant-select-item-option-content" }));
    fireEvent.click(screen.getByRole("button", { name: "保存可见人员" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith(9, { expected_version: 4, visible_to: ["old_sales", "sales_b"] }));
  });

  it("版本冲突后需要显式重载，不能带新版本直接覆盖他人名单", async () => {
    vi.spyOn(presales, "getInquiryVisibility").mockResolvedValueOnce({ version: 3, visible_to: [] })
      .mockResolvedValue({ version: 4, visible_to: [{ id: "sales_b", name: "销售乙" }] });
    const save = vi.spyOn(presales, "updateInquiryVisibility").mockRejectedValueOnce({ response: { status: 409, data: { detail: { code: "version_conflict" } } } })
      .mockResolvedValue({ version: 5, visible_to: [] });
    showDialog();
    const button = screen.getByRole("button", { name: "保存可见人员" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(screen.getByText("询价已被修改，请重新加载可见人员后再保存")).toBeVisible());
    expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重新加载可见人员" }));
    await screen.findByText("销售乙");
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    await waitFor(() => expect(save).toHaveBeenLastCalledWith(9, { expected_version: 4, visible_to: ["sales_b"] }));
  });

  it("读名单失败时不能把默认空名单覆盖到服务器", async () => {
    vi.spyOn(presales, "getInquiryVisibility").mockRejectedValue(new Error("offline"));
    const save = vi.spyOn(presales, "updateInquiryVisibility");
    showDialog();
    await waitFor(() => expect(screen.getByText("可见人员加载失败，请确认当前权限后重试")).toBeVisible());
    expect(screen.getByRole("button", { name: "保存可见人员" })).toBeDisabled();
    expect(save).not.toHaveBeenCalled();
  });

  it("读名单发现权限撤销时通知页面清理数据", async () => {
    vi.spyOn(presales, "getInquiryVisibility").mockRejectedValue({ response: { status: 403 } });
    const { onDenied } = showDialog();
    await waitFor(() => expect(onDenied).toHaveBeenCalledOnce());
  });
});

describe("共享池新建询价", () => {
  beforeEach(() => {
    vi.spyOn(presales, "customers").mockResolvedValue([]);
    vi.spyOn(search, "suggestParts").mockResolvedValue([]);
    vi.spyOn(presales, "library").mockResolvedValue({});
  });

  it.each([false, true])("新建提交携带可选可见人员，选择=%s", async (selectPerson) => {
    const create = vi.spyOn(presalesPool, "create").mockResolvedValue({ inquiry_id: 9, inquiry_no: "XJ9", line_count: 1 });
    render(<NewInquiryPanel onCreated={vi.fn()} />);
    fireEvent.change(screen.getByRole("combobox", { name: "第 1 行 PN" }), { target: { value: "NEW-PN" } });
    if (selectPerson) {
      fireEvent.mouseDown(screen.getByRole("combobox", { name: "额外可见人员" }));
      fireEvent.click(await screen.findByText("销售乙", { selector: ".ant-select-item-option-content" }));
    }
    fireEvent.click(screen.getByRole("button", { name: "提交询价" }));
    await waitFor(() => expect(create).toHaveBeenCalledWith(expect.objectContaining({ visible_to: selectPerson ? ["sales_b"] : [] })));
    expect(await screen.findByText("询价 XJ9 已提交，共 1 行")).toBeVisible();
  });
});
