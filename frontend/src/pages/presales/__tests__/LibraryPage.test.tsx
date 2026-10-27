import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "../../../test/antdRender";
import { MemoryRouter, useLocation } from "react-router-dom";
import type { LibraryPart, Offer } from "../../../api/presales";
import LibraryPage from "../LibraryPage";
import { expiryLabel, usePresalesError, isOfferActive, offerDeadline, OfferBadge } from "../shared";

const api = vi.hoisted(() => ({ searchLibrary: vi.fn(), getInquiry: vi.fn() }));
vi.mock("../../../api/presales", () => ({ presales: api }));
vi.mock("../../../components/parts/LivePartSearch", () => ({ default: ({ inputValue, onInputChange }: {
  inputValue: string; onInputChange: (value: string) => void;
}) => <input aria-label="报价库搜索" value={inputValue} onChange={(event) => onInputChange(event.target.value)} /> }));
vi.mock("../../../components/gallery/PartThumb", () => ({ default: () => <span>缩略图</span> }));
vi.mock("../../../components/pricing/PriceRefBadge", () => ({ default: () => <span>参考价</span> }));

function offer(overrides: Partial<Offer> = {}): Offer {
  return { id: 7, inquiry_id: 2, line_id: 3, part_id: 1, version: 1, revision_no: 1,
    status: "published", unit_price: "100.00", is_tax_inclusive: false, stock_location: "深圳",
    valid_from: "2026-10-25T00:00:00Z", valid_until: "2026-10-25T15:59:59.999999Z", ...overrides };
}

function Location() {
  const location = useLocation();
  return <output aria-label="导航">{location.pathname} {JSON.stringify(location.state)}</output>;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-25T15:59:59.500Z"));
  api.searchLibrary.mockReset().mockResolvedValue([{ part_id: 1, pn_std: "PN-A", offers: [offer()], offer_count: 1 } as LibraryPart]);
  api.getInquiry.mockReset().mockResolvedValue({ id: 2, kind: "proactive_offer" });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

async function search() {
  render(<MemoryRouter><LibraryPage /><Location /></MemoryRouter>);
  fireEvent.change(screen.getByRole("textbox", { name: "报价库搜索" }), { target: { value: "PN-A" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(150); });
}

it("报价库按输入查询，显示税基、来源和服务端允许的字段；引用携带报价身份", async () => {
  await search();
  expect(api.searchLibrary).toHaveBeenCalledWith("PN-A", expect.any(AbortSignal));
  expect(screen.getByText("来源：主动报价")).toBeInTheDocument();
  expect(screen.getByText("所在地：深圳")).toBeInTheDocument();
  expect(screen.queryByText(/报价人：|供应商：/)).not.toBeInTheDocument();
  expect(screen.getByText(/有效报价.*不含税.*剩 1 天/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "用这条报价建询价单" }));
  expect(screen.getByLabelText("导航")).toHaveTextContent("/presales/inquiries/new");
  expect(screen.getByLabelText("导航")).toHaveTextContent('"referenceOffer":{"id":7');
});

it("微秒截止在浏览器可表示的最后一毫秒仍有效，跨到次日立即置灰并禁用引用", async () => {
  await search();
  await act(async () => { await vi.advanceTimersByTimeAsync(349); });
  expect(screen.getByRole("button", { name: "用这条报价建询价单" })).toBeEnabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByRole("button", { name: "用这条报价建询价单" })).toBeDisabled();
  expect(screen.getByLabelText("报价 7")).toHaveAttribute("aria-disabled", "true");
  expect(screen.getByLabelText("报价 7")).toHaveTextContent("已失效");
});

it("旧搜索的迟到结果不会覆盖新的查询", async () => {
  let resolve!: (value: LibraryPart[]) => void;
  api.searchLibrary.mockReturnValueOnce(new Promise<LibraryPart[]>((done) => { resolve = done; }));
  await search();
  fireEvent.change(screen.getByRole("textbox", { name: "报价库搜索" }), { target: { value: "PN-B" } });
  api.searchLibrary.mockResolvedValueOnce([]);
  await act(async () => { await vi.advanceTimersByTimeAsync(150); });
  await act(async () => { resolve([{ part_id: 1, pn_std: "旧结果", offers: [offer()], offer_count: 1 } as LibraryPart]); });
  expect(screen.queryByText("旧结果")).not.toBeInTheDocument();
  expect(screen.getByText("没有可见的有效报价")).toBeInTheDocument();
});

it("409 显示统一提示并执行刷新，非冲突显示服务端错误", async () => {
  const refresh = vi.fn();
  function Feedback({ status }: { status: number }) {
    const handleError = usePresalesError();
    return <button onClick={() => void handleError({ response: { status, data: { detail: "未完成逐行报价" } } }, refresh)}>触发错误</button>;
  }
  const view = render(<Feedback status={409} />);
  fireEvent.click(screen.getByRole("button", { name: "触发错误" }));
  await act(async () => {});
  expect(screen.getByText("已被他人修改/领取")).toBeInTheDocument();
  expect(refresh).toHaveBeenCalledOnce();
  view.rerender(<Feedback status={422} />);
  fireEvent.click(screen.getByRole("button", { name: "触发错误" }));
  await act(async () => {});
  expect(screen.getByText("未完成逐行报价")).toBeInTheDocument();
  expect(refresh).toHaveBeenCalledOnce();
});

it("报价角标只展示服务端返回的报价人，并在边界刷新", async () => {
  render(<OfferBadge offer={offer({ reporter: { id: 4, name: "采购甲" } })} />);
  expect(screen.getByText(/采购甲/)).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(screen.getByText(/已失效/)).toBeInTheDocument();
});

it("1 天和 3 天按上海自然日计算，覆盖月底及周末", () => {
  const now = Date.parse("2026-10-30T16:30:00Z");
  expect(expiryLabel(1, now)).toBe("将于 10月31日 23:59 失效（上海时间）");
  expect(expiryLabel(3, now)).toBe("将于 11月2日 23:59 失效（上海时间）");
  expect(isOfferActive(offer(), Date.parse("2026-10-25T15:59:59.999Z"))).toBe(true);
  expect(isOfferActive(offer(), Date.parse("2026-10-25T16:00:00Z"))).toBe(false);
  expect(offerDeadline("2026-10-25T16:00:00.000000Z")).toBe(Date.parse("2026-10-25T16:00:00Z"));
});

it("每位报价人保留主报价，展开后才显示服务端 more 中的其他报价", async () => {
  api.searchLibrary.mockResolvedValue([{ part_id: 1, pn_std: "PN-A", offer_count: 2, offers: [
    offer({ id: 7, unit_price: "100.00", reporter: { id: 1, name: "采购甲" }, more: [
      offer({ id: 8, unit_price: "80.00", reporter: { id: 1, name: "采购甲" } }),
    ] }),
    offer({ id: 9, unit_price: "150.00", reporter: { id: 2, name: "采购乙" }, more: [offer({ id: 10, unit_price: "130.00" })] }),
  ] }]);
  await search();
  expect(screen.getAllByRole("button", { name: "用这条报价建询价单" })).toHaveLength(2);
  expect(screen.queryByLabelText("报价 8")).not.toBeInTheDocument();
  fireEvent.click(screen.getAllByRole("button", { name: "展开更多（1 条）" })[0]);
  expect(screen.getByLabelText("报价 8")).toHaveTextContent("¥80");
  expect(screen.getAllByRole("button", { name: "用这条报价建询价单" })).toHaveLength(3);
  expect(screen.queryByLabelText("报价 10")).not.toBeInTheDocument();
});

it("钉版本角标只用已授权 latest 展示更新提示，并标出原报价撤销", () => {
  render(<OfferBadge offer={offer({ unit_price: "100.00", original_withdrawn: true,
    latest: { unit_price: "250.00", is_tax_inclusive: true, revision_no: 2 } })} />);
  expect(screen.getByText(/已失效 ¥100/)).toBeInTheDocument();
  expect(screen.getByText("该报价已更新为 ¥250 · 含税")).toBeInTheDocument();
  expect(screen.getByText("原报价已撤销")).toBeInTheDocument();
});

it("更多报价也能展开各自的可供数量与交期，不误用主报价的字段", async () => {
  api.searchLibrary.mockResolvedValue([{ part_id: 1, pn_std: "PN-A", offer_count: 2, offers: [
    offer({ id: 7, qty_available: 9, lead_time: "主报价交期", more: [
      offer({ id: 8, unit_price: "80.00", qty_available: 42, lead_time: "三个工作日" }),
    ] }),
  ] }]);
  await search();
  fireEvent.click(screen.getByRole("button", { name: "展开更多（1 条）" }));
  const nested = screen.getByRole("table", { name: "PN-A 更多报价" });
  expect(within(nested).queryByText("可供数量：42")).not.toBeInTheDocument();
  fireEvent.click(within(nested).getByRole("button", { name: "展开 PN-A" }));
  expect(within(nested).getByText("可供数量：42")).toBeVisible();
  expect(within(nested).getByText("交期：三个工作日")).toBeVisible();
  expect(within(nested).queryByText("交期：主报价交期")).not.toBeInTheDocument();
});

it("主报价到期但同组旧报价仍有效时，主行提升为可引用的最新有效报价", async () => {
  api.searchLibrary.mockResolvedValue([{ part_id: 1, pn_std: "PN-A", offer_count: 1, offers: [
    offer({ more: [offer({ id: 8, unit_price: "80.00", valid_from: "2026-10-24T00:00:00Z",
      valid_until: "2026-10-26T15:59:59.999999Z" })] }),
  ] }]);
  await search();
  expect(screen.getByLabelText("报价 7")).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(350); });
  expect(screen.queryByLabelText("报价 7")).not.toBeInTheDocument();
  expect(screen.getByLabelText("报价 8")).toHaveTextContent("有效报价 ¥80");
  expect(screen.getByRole("button", { name: "用这条报价建询价单" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "用这条报价建询价单" }));
  expect(screen.getByLabelText("导航")).toHaveTextContent('"referenceOffer":{"id":8');
});
