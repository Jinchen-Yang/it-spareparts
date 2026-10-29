import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "../../../test/antdRender";
import { MemoryRouter, useLocation } from "react-router-dom";
import type { LibraryPart, Offer } from "../../../api/presales";
import LibraryPage from "../LibraryPage";
import { usePresalesError, isOfferActive, offerDeadline, OfferBadge } from "../shared";

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
    valid_from: "2026-10-25T00:00:00Z", ...overrides };
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
  expect(screen.getByText(/有效报价.*不含税/)).toBeInTheDocument();
  expect(screen.queryByText(/剩 \d+ 天/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "用这条报价建询价单" }));
  expect(screen.getByLabelText("导航")).toHaveTextContent("/presales/inquiries/new");
  expect(screen.getByLabelText("导航")).toHaveTextContent('"referenceOffer":{"id":7');
});

it("D-0046：报价不随时间失效，跨日后仍可引用", async () => {
  await search();
  await act(async () => { await vi.advanceTimersByTimeAsync(2 * 86_400_000); });
  expect(screen.getByRole("button", { name: "用这条报价建询价单" })).toBeEnabled();
  expect(screen.getByLabelText("报价 7")).toHaveAttribute("aria-disabled", "false");
  expect(screen.getByLabelText("报价 7")).not.toHaveTextContent("已失效");
  expect(screen.getByText(/报价一直有效，直到报价人更新或撤回/)).toBeInTheDocument();
});

it("独立主动报价没有来源单，不请求或导航到null来源，仍可引用", async () => {
  api.searchLibrary.mockResolvedValue([{ part_id: 1, pn_std: "PN-A", offers: [offer({ inquiry_id: null, line_id: null })], offer_count: 1 } as LibraryPart]);
  await search();
  expect(api.getInquiry).not.toHaveBeenCalled();
  expect(screen.getByText("来源：主动报价")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "查看来源单" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "用这条报价建询价单" })).toBeEnabled();
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

it("普通 409 保留业务原因不刷新，字段错误显示服务端原因", async () => {
  const refresh = vi.fn();
  function Feedback({ status }: { status: number }) {
    const handleError = usePresalesError();
    return <button onClick={() => void handleError({ response: { status, data: { detail: "未完成逐行报价" } } }, refresh)}>触发错误</button>;
  }
  const view = render(<Feedback status={409} />);
  fireEvent.click(screen.getByRole("button", { name: "触发错误" }));
  await act(async () => {});
  expect(screen.getByText("未完成逐行报价")).toBeInTheDocument();
  expect(refresh).not.toHaveBeenCalled();
  view.rerender(<Feedback status={422} />);
  fireEvent.click(screen.getByRole("button", { name: "触发错误" }));
  await act(async () => {});
  expect(screen.getAllByText("未完成逐行报价")).toHaveLength(2);
  expect(refresh).not.toHaveBeenCalled();
});

it("报价角标只展示服务端返回的报价人，不随时间变为失效", async () => {
  render(<OfferBadge offer={offer({ reporter: { id: 4, name: "采购甲" } })} />);
  expect(screen.getByText(/采购甲/)).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(86_400_000); });
  expect(screen.getByText(/有效报价/)).toBeInTheDocument();
  expect(screen.queryByText(/已失效/)).not.toBeInTheDocument();
});

it("有效判断只看状态与服务端取代标记，不看时间截止", () => {
  expect(isOfferActive(offer(), Date.parse("2026-10-25T15:59:59.999Z"))).toBe(true);
  expect(isOfferActive(offer(), Date.parse("2036-10-25T16:00:00Z"))).toBe(true);
  expect(isOfferActive(offer(), Date.parse("2026-10-24T23:59:59Z"))).toBe(false);
  expect(isOfferActive(offer({ expired: true }))).toBe(false);
  expect(isOfferActive(offer({ no_stock: true }))).toBe(false);
  expect(isOfferActive(offer({ original_withdrawn: true }))).toBe(false);
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
  fireEvent.click(screen.getAllByRole("button", { name: "报价详情（更多 1 条）" })[0]);
  expect(screen.getByLabelText("报价 8")).toHaveTextContent("¥80");
  expect(within(screen.getByRole("dialog")).getAllByRole("button", { name: "用这条报价建询价单" })).toHaveLength(2);
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
  fireEvent.click(screen.getByRole("button", { name: "报价详情（更多 1 条）" }));
  const nested = screen.getByRole("table", { name: "PN-A 更多报价" });
  const row = within(nested).getByLabelText("报价 8").closest("tr")!;
  expect(row).toHaveTextContent("可供数量42");
  expect(row).toHaveTextContent("交期三个工作日");
  expect(row).not.toHaveTextContent("主报价交期");
});

it("主报价不可用但同组其他报价仍有效时，主行提升为可引用的最新有效报价", async () => {
  api.searchLibrary.mockResolvedValue([{ part_id: 1, pn_std: "PN-A", offer_count: 1, offers: [
    offer({ expired: true, more: [offer({ id: 8, unit_price: "80.00", valid_from: "2026-10-24T00:00:00Z" })] }),
  ] }]);
  await search();
  expect(screen.queryByLabelText("报价 7")).not.toBeInTheDocument();
  expect(screen.getByLabelText("报价 8")).toHaveTextContent("有效报价 ¥80");
  expect(screen.getByRole("button", { name: "用这条报价建询价单" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "用这条报价建询价单" }));
  expect(screen.getByLabelText("导航")).toHaveTextContent('"referenceOffer":{"id":8');
});
