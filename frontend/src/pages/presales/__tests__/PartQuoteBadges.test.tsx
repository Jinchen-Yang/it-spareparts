import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "../../../test/antdRender";
import type { Offer } from "../../../api/presales";
import PartQuoteBadges from "../PartQuoteBadges";

const library = vi.hoisted(() => vi.fn());
vi.mock("../../../api/presales", () => ({ presales: { library } }));
vi.mock("../../../components/pricing/PriceRefBadge", () => ({ default: () => <span>参考价角标</span> }));
const quote: Offer = { id: 1, inquiry_id: 2, line_id: 3, part_id: 8, version: 1, revision_no: 1,
  unit_price: "25.00", status: "published", valid_from: "2026-10-25T00:00:00Z" };
beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-25T15:59:59.999Z")); library.mockReset();
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it("检索候选未选择前即可显示有效报价，角标不随时间移除（D-0046）", async () => {
  library.mockResolvedValue({ 8: [quote] });
  render(<PartQuoteBadges partId={8} />);
  await act(async () => {});
  expect(library).toHaveBeenCalledWith([8]);
  expect(screen.getByText("参考价角标")).toBeInTheDocument();
  expect(screen.getByText(/有效报价/)).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(86_400_000); });
  expect(screen.getByText(/有效报价/)).toBeInTheDocument();
});

it("切换 PN 后忽略旧请求，不产生隐藏报价的占位", async () => {
  let resolve!: (value: Record<number, Offer[]>) => void;
  library.mockReturnValueOnce(new Promise((done) => { resolve = done; })).mockResolvedValueOnce({ 9: [] });
  const view = render(<PartQuoteBadges partId={8} />);
  view.rerender(<PartQuoteBadges partId={9} />);
  await act(async () => { resolve({ 8: [quote] }); });
  expect(screen.queryByText(/有效报价|¥25|已失效/)).not.toBeInTheDocument();
});

it("置顶只显示主列表首条同税基优先最低价及报价人数，不用 more 低价替代", async () => {
  library.mockResolvedValue({ 8: [
    { ...quote, unit_price: "100.00", is_tax_inclusive: true, more: [{ ...quote, id: 2, unit_price: "1.00" }] },
    { ...quote, id: 3, unit_price: "50.00", is_tax_inclusive: false },
  ] });
  render(<PartQuoteBadges partId={8} />);
  await act(async () => {});
  expect(screen.getByText(/有效报价 ¥100.*含税/)).toBeInTheDocument();
  expect(screen.getByText("最低价 · 2 位报价人")).toBeInTheDocument();
  expect(screen.queryByText(/¥50|¥1 ·/)).not.toBeInTheDocument();
});
