/** PartPicker（可复用型号选择器）：远程统一搜索 + 防抖 + onChange 只回传 part_id。 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const unifiedSearch = vi.fn();

vi.mock("../../api/search", () => ({
  unifiedSearch: (...a: unknown[]) => unifiedSearch(...a),
}));

import PartPicker from "../PartPicker";

const item = (over: Record<string, unknown> = {}) => ({
  part_id: 42, pn_std: "02311DYQ", description: "华为部件 DYQ", brand: "华为",
  category: "备件", category_major: "备件", needs_review: false, is_excluded: false,
  match_type: "exact_pn", matched_text: "02311DYQ", score: 1, match_reason: "PN精确匹配",
  pool_group_id: 7, pool_name: "华为互通池", ...over,
});

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

describe("PartPicker", () => {
  it("远程搜索（300ms 防抖）并把 part_id（数字）传给 onChange —— 不回传展示文本", async () => {
    unifiedSearch.mockResolvedValue({
      total: 1, page: 1, page_size: 20, exact: true, ambiguous: false, low_confidence: false,
      items: [item()],
      similar_items: [item({ part_id: 43, pn_std: "02311DYA", match_type: "fuzzy_pn", score: 0.8 })],
    });
    const onChange = vi.fn();
    render(<PartPicker onChange={onChange} />);
    const combo = screen.getByRole("combobox");
    fireEvent.mouseDown(combo);                       // 打开下拉
    fireEvent.change(combo, { target: { value: "02311DYQ" } });
    // 防抖后才发请求
    expect(unifiedSearch).not.toHaveBeenCalled();
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledWith(
      "02311DYQ", { pageSize: 20 }), { timeout: 3000 });
    // 精确命中排第一并带"精确"徽标；相似候选在"相似型号（非精确）"分组
    await screen.findByText("精确匹配");
    await screen.findByText("相似型号（非精确）");
    const opt = await screen.findByText("02311DYQ");
    fireEvent.click(opt.closest(".ant-select-item-option") || opt);
    expect(onChange).toHaveBeenCalledWith(42, expect.objectContaining({ pn_std: "02311DYQ" }));
    expect(typeof onChange.mock.calls[0][0]).toBe("number");
  });

  it("代次守卫：旧响应不覆盖新输入的结果", async () => {
    let resolveOld!: (v: unknown) => void;
    const oldP = new Promise((res) => { resolveOld = res; });
    unifiedSearch
      .mockImplementationOnce(() => oldP)             // 第一次搜索挂起
      .mockResolvedValueOnce({                        // 第二次先返回
        total: 1, page: 1, page_size: 20, exact: false, ambiguous: false, low_confidence: false,
        items: [item({ part_id: 99, pn_std: "NEW-RESULT", match_type: "fuzzy_pn", score: 0.7 })],
        similar_items: [],
      });
    render(<PartPicker />);
    const combo = screen.getByRole("combobox");
    fireEvent.mouseDown(combo);
    fireEvent.change(combo, { target: { value: "old" } });
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledTimes(1), { timeout: 3000 });
    fireEvent.change(combo, { target: { value: "new" } });
    await waitFor(() => expect(unifiedSearch).toHaveBeenCalledTimes(2), { timeout: 3000 });
    await screen.findByText("NEW-RESULT");
    // 旧的慢响应此刻才回来：不得把 NEW-RESULT 顶掉
    resolveOld({ total: 1, page: 1, page_size: 20, exact: false, ambiguous: false,
                 low_confidence: false, items: [item({ part_id: 1, pn_std: "STALE-OLD" })],
                 similar_items: [] });
    await Promise.resolve();
    expect(screen.queryByText("STALE-OLD")).toBeNull();
    expect(screen.getAllByText("NEW-RESULT").length).toBeGreaterThan(0);
  });
});
