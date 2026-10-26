import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { PartSuggestion } from "../../../api/search";

const suggestParts = vi.fn();
vi.mock("../../../api/search", () => ({ suggestParts: (...args: unknown[]) => suggestParts(...args) }));
import LivePartSearch from "../LivePartSearch";

const item = (id = 1, pn = "0231-AC4L"): PartSuggestion => ({
  part_id: id, pn_std: pn, brand: "华为", manufacturer: "Huawei",
  summary: "服务器硬盘", description: "详细描述", key_specs: [{ label: "容量", value: "1200GB" }],
  match_type: "pn_prefix", match_reason: "PN前缀", matched_terms: ["0231"],
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
async function tick(ms = 150) { await act(async () => { vi.advanceTimersByTime(ms); }); }
const input = () => screen.getByRole("textbox");
const type = (value: string) => fireEvent.change(input(), { target: { value } });

beforeEach(() => {
  vi.useFakeTimers();
  suggestParts.mockReset();
  suggestParts.mockResolvedValue([item()]);
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("LivePartSearch", () => {
  it.each(["S", "硬", " S "])("默认支持单字符，去除首尾空白后按 300ms 防抖：%s", async (query) => {
    render(<LivePartSearch />);
    type(query);
    await tick(299);
    expect(suggestParts).not.toHaveBeenCalled();
    await tick(1);
    expect(suggestParts).toHaveBeenCalledExactlyOnceWith(query, 10, expect.any(AbortSignal));
  });

  it("扩展汉字按一个 Unicode 字符使用 300ms 防抖，仍受 minChars 阈值限制", async () => {
    const { rerender } = render(<LivePartSearch />);
    type("𠀀");
    await tick(299);
    expect(suggestParts).not.toHaveBeenCalled();
    await tick(1);
    expect(suggestParts).toHaveBeenCalledExactlyOnceWith("𠀀", 10, expect.any(AbortSignal));
    rerender(<LivePartSearch minChars={2} />);
    type(""); type("𠀀");
    await tick(300);
    expect(suggestParts).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it.each(["HP", "硬盘", "SAS"])("两个及以上字符按 150ms 防抖：%s", async (query) => {
    render(<LivePartSearch />);
    type(query);
    await tick(149);
    expect(suggestParts).not.toHaveBeenCalled();
    await tick(1);
    expect(suggestParts).toHaveBeenCalledExactlyOnceWith(query, 10, expect.any(AbortSignal));
  });

  it("从单字符改成双字符时取消 300ms 计时，重新按 150ms 查询", async () => {
    render(<LivePartSearch />);
    type("S"); await tick(200);
    type("SA"); await tick(149);
    expect(suggestParts).not.toHaveBeenCalled();
    await tick(1);
    expect(suggestParts).toHaveBeenCalledExactlyOnceWith("SA", 10, expect.any(AbortSignal));
    await tick(300);
    expect(suggestParts).toHaveBeenCalledTimes(1);
  });

  it("minChars 按去除首尾空白后的长度拦截输入，达到阈值才查询", async () => {
    render(<LivePartSearch minChars={2} />);
    type(" S "); await tick(300);
    expect(suggestParts).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).toBeNull();
    type(" SA "); await tick(149);
    expect(suggestParts).not.toHaveBeenCalled();
    await tick(1);
    expect(suggestParts).toHaveBeenCalledExactlyOnceWith(" SA ", 10, expect.any(AbortSignal));
  });

  it("低于 minChars 时取消待发请求和进行中请求，并清空旧结果", async () => {
    const pending = deferred<PartSuggestion[]>();
    suggestParts.mockReturnValue(pending.promise);
    render(<LivePartSearch minChars={2} />);
    type("SA"); await tick(100);
    type("S"); await tick(300);
    expect(suggestParts).not.toHaveBeenCalled();
    type("SA"); await tick();
    const signal: AbortSignal = suggestParts.mock.calls[0][2];
    type("S");
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve([item()]));
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.queryByRole("option")).toBeNull();
    await tick(300);
    expect(suggestParts).toHaveBeenCalledTimes(1);
  });

  it("中文组合输入结束仍须达到 minChars 才立即请求", async () => {
    render(<LivePartSearch minChars={2} />);
    fireEvent.compositionStart(input());
    type("硬");
    fireEvent.compositionEnd(input());
    await tick(300);
    expect(suggestParts).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.compositionStart(input());
    type("硬盘");
    fireEvent.compositionEnd(input());
    expect(suggestParts).toHaveBeenCalledExactlyOnceWith("硬盘", 10, expect.any(AbortSignal));
  });

  it("保留缩略图插槽，以及 PN、命中原因、品牌同一行和简介第二行的布局", async () => {
    render(<LivePartSearch renderThumb={() => <img alt="型号缩略图" src="/thumb.jpg" />} />);
    type("0231"); await tick();
    const option = screen.getByRole("option");
    const line = option.querySelector(".live-part-search-line")!;
    expect(within(option).getByRole("img", { name: "型号缩略图" }).parentElement).toBe(option);
    expect(line).toHaveTextContent("0231-AC4L");
    expect(line).toHaveTextContent("PN前缀");
    expect(line).toHaveTextContent("华为 / Huawei");
    expect(line.nextElementSibling).toHaveClass("live-part-search-summary");
    expect(line.nextElementSibling).toHaveTextContent("服务器硬盘");
  });

  it("150ms 防抖且只发最后一次输入；展示资料、命中原因和 PN 高亮", async () => {
    render(<LivePartSearch />);
    type("02");
    await tick(100);
    type("0231");
    await tick(149);
    expect(suggestParts).not.toHaveBeenCalled();
    await tick(1);
    expect(suggestParts).toHaveBeenCalledWith("0231", 10, expect.any(AbortSignal));
    expect(suggestParts).toHaveBeenCalledTimes(1);
    const option = screen.getByRole("option");
    expect(option).toHaveTextContent("华为 / Huawei");
    expect(option).toHaveTextContent("服务器硬盘");
    expect(option).toHaveTextContent("容量 1200GB");
    expect(option).toHaveTextContent("PN前缀");
    expect(option.querySelector("mark")).toHaveTextContent("0231");
  });

  it.each([
    { pn: "0231-AC4L", terms: ["0231AC4L"], marks: ["0231-AC4L"] },
    { pn: "０２３１－ａｃ４Ｌ", terms: ["0231AC4L"], marks: ["０２３１－ａｃ４Ｌ"] },
    { pn: "0231-AC4L", terms: ["０２３１－ＡＣ４Ｌ"], marks: ["0231-AC4L"] },
    { pn: "ABC-123-X", terms: ["ab*12?"], marks: ["AB", "12"] },
    { pn: "ABC-123", terms: ["品牌华为"], marks: [] },
    { pn: "A-B_X", terms: ["AB_X", "AB%X", "AB\\X"], marks: [] },
    { pn: "A_B.%X", terms: ["_B.%"], marks: ["_B.%"] },
  ])("高亮映回 PN 原文：$pn / $terms", async ({ pn, terms, marks }) => {
    suggestParts.mockResolvedValue([{ ...item(1, pn), matched_terms: terms }]);
    render(<LivePartSearch />);
    type("query"); await tick();
    const option = screen.getByRole("option");
    expect([...option.querySelectorAll("mark")].map((mark) => mark.textContent)).toEqual(marks);
    expect(option.querySelector("strong")).toHaveTextContent(pn);
  });

  it("新输入立即取消旧请求，旧响应在新防抖窗口内及之后都不能覆盖结果", async () => {
    const old = deferred<PartSuggestion[]>();
    suggestParts.mockReturnValueOnce(old.promise).mockResolvedValueOnce([item(2, "NEW-PN")]);
    render(<LivePartSearch />);
    type("old"); await tick();
    const signal: AbortSignal = suggestParts.mock.calls[0][2];
    type("new");
    expect(signal.aborted).toBe(true);
    await act(async () => { old.resolve([item(3, "STALE-PN")]); });
    expect(screen.queryByText("STALE-PN")).toBeNull();
    await tick();
    expect(screen.getByRole("option")).toHaveTextContent("NEW-PN");
  });

  it("不依赖后端遵守 AbortSignal：旧请求晚于新请求返回仍被代次守卫丢弃", async () => {
    const abort = vi.spyOn(AbortController.prototype, "abort").mockImplementation(() => {});
    const old = deferred<PartSuggestion[]>();
    suggestParts.mockReturnValueOnce(old.promise).mockResolvedValueOnce([item(2, "NEW-PN")]);
    render(<LivePartSearch />);
    type("old"); await tick();
    type("new"); await tick();
    await act(async () => { old.resolve([item(3, "STALE-PN")]); });
    expect(screen.getByRole("option")).toHaveTextContent("NEW-PN");
    expect(screen.queryByText("STALE-PN")).toBeNull();
    abort.mockRestore();
  });

  it("清空、Esc、卸载都取消待发或进行中的请求，关闭后晚响应不重开下拉", async () => {
    const old = deferred<PartSuggestion[]>();
    suggestParts.mockReturnValue(old.promise);
    const { unmount } = render(<LivePartSearch />);
    type("first"); type(""); await tick();
    expect(suggestParts).not.toHaveBeenCalled();
    type("first"); await tick();
    const firstSignal: AbortSignal = suggestParts.mock.calls[0][2];
    fireEvent.keyDown(input(), { key: "Escape" });
    expect(firstSignal.aborted).toBe(true);
    await act(async () => old.resolve([item()]));
    expect(screen.queryByRole("listbox")).toBeNull();
    type("next"); await tick();
    const nextSignal: AbortSignal = suggestParts.mock.calls[1][2];
    unmount();
    expect(nextSignal.aborted).toBe(true);
  });

  it("中文组合输入不请求、不回车选择，compositionend 立即请求且不重复防抖", async () => {
    render(<LivePartSearch />);
    fireEvent.compositionStart(input());
    type("ying"); await tick(300);
    type("硬盘"); await tick(300);
    expect(suggestParts).not.toHaveBeenCalled();
    expect(fireEvent.keyDown(input(), { key: "Enter", isComposing: true })).toBe(false);
    fireEvent.compositionEnd(input());
    expect(suggestParts).toHaveBeenCalledWith("硬盘", 10, expect.any(AbortSignal));
    type("硬盘"); await tick(300);
    expect(suggestParts).toHaveBeenCalledTimes(1);
  });

  it("上下键移动、Enter 单选返回完整条目；未移动时 Enter 保留表单提交", async () => {
    suggestParts.mockResolvedValue([item(), item(2, "SECOND")]);
    const onChange = vi.fn();
    render(<LivePartSearch onChange={onChange} />);
    type("0231"); await tick();
    expect(fireEvent.keyDown(input(), { key: "Enter" })).toBe(true);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(fireEvent.keyDown(input(), { key: "Enter" })).toBe(false);
    expect(onChange).toHaveBeenCalledWith([item()]);
    expect(input()).toHaveValue("0231-AC4L");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("多选去重、Tag 移除返回完整数组，受控 value 也可回填", async () => {
    const onChange = vi.fn();
    render(<LivePartSearch mode="multiple" onChange={onChange} />);
    type("one"); await tick(); fireEvent.click(screen.getByRole("option"));
    expect(onChange).toHaveBeenLastCalledWith([item()]);
    expect(input()).toHaveValue("");
    type("duplicate"); await tick(); fireEvent.click(screen.getByRole("option"));
    expect(onChange).toHaveBeenLastCalledWith([item()]);
    suggestParts.mockResolvedValue([item(2, "SECOND")]);
    type("two"); await tick(); fireEvent.click(screen.getByRole("option"));
    expect(onChange).toHaveBeenLastCalledWith([item(), item(2, "SECOND")]);
    const firstTag = screen.getByText("0231-AC4L").closest(".ant-tag")!;
    fireEvent.click(within(firstTag as HTMLElement).getByRole("img", { name: /^close$/i }));
    expect(onChange).toHaveBeenLastCalledWith([item(2, "SECOND")]);
    expect(screen.queryByText("0231-AC4L")).toBeNull();
  });

  it("原文添加保留输入，未提供回调不显示；图片和角标插槽按条目渲染", async () => {
    const onAddRaw = vi.fn();
    const { rerender } = render(<LivePartSearch onAddRaw={onAddRaw}
      renderThumb={(candidate) => <span>图片 {candidate.part_id}</span>}
      renderBadges={() => <span>后续角标</span>} />);
    type(" new PN "); await tick();
    expect(screen.getByRole("option")).toHaveTextContent("图片 1");
    expect(screen.getByRole("option")).toHaveTextContent("后续角标");
    fireEvent.click(screen.getByRole("button", { name: /没找到？按原文添加/ }));
    expect(onAddRaw).toHaveBeenCalledWith(" new PN ");
    expect(screen.queryByRole("listbox")).toBeNull();
    rerender(<LivePartSearch />);
    type("other"); await tick();
    expect(screen.queryByRole("button", { name: /按原文添加/ })).toBeNull();
  });

  it("加载、无结果、错误都有可见状态，异常不调用浏览器弹窗", async () => {
    const pending = deferred<PartSuggestion[]>();
    suggestParts.mockReturnValueOnce(pending.promise).mockRejectedValueOnce(new Error("offline"));
    const alert = vi.spyOn(window, "alert");
    render(<LivePartSearch />);
    type("one"); await tick();
    expect(screen.getByRole("status")).toHaveTextContent("搜索中");
    await act(async () => pending.resolve([]));
    expect(screen.getByText("暂无匹配型号")).toBeVisible();
    type("two"); await tick();
    expect(screen.getByRole("alert")).toHaveTextContent("联想加载失败");
    expect(alert).not.toHaveBeenCalled();
    alert.mockRestore();
  });

  it("外部输入同步会废弃旧请求且不自动查询；受控已选项支持移除", async () => {
    const old = deferred<PartSuggestion[]>();
    suggestParts.mockReturnValue(old.promise);
    const onChange = vi.fn();
    const { rerender } = render(<LivePartSearch inputValue="" mode="multiple" value={[item()]} onChange={onChange} />);
    type("old"); await tick();
    rerender(<LivePartSearch inputValue="URL-PN" mode="multiple" value={[item()]} onChange={onChange} />);
    expect(suggestParts.mock.calls[0][2].aborted).toBe(true);
    await act(async () => old.resolve([item(2, "STALE")]));
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(input()).toHaveValue("URL-PN");
    fireEvent.click(screen.getByRole("img", { name: /^close$/i }));
    expect(onChange).toHaveBeenCalledWith([]);
  });
});
