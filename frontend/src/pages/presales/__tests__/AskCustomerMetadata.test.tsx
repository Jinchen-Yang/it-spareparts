import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { act, cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import type { Inquiry } from "../../../api/presales";

const mocks = vi.hoisted(() => ({
  listInquiries: vi.fn(), getInquiry: vi.fn(), urgeInquiry: vi.fn(), customers: vi.fn(),
  listWorkCandidates: vi.fn(), suggestParts: vi.fn(),
}));
vi.mock("../../../api/presales", () => ({ presales: mocks }));
vi.mock("../../../api/workItems", () => ({ listWorkCandidates: mocks.listWorkCandidates }));
vi.mock("../../../api/search", () => ({ suggestParts: mocks.suggestParts }));
vi.mock("../../../components/parts/CreatePartModal", () => ({ default: () => null }));
vi.mock("../../../components/gallery/PartThumb", () => ({ default: () => null }));
vi.mock("../../../components/audience/AudiencePicker", () => ({
  PUBLIC_AUDIENCE: { mode: "public", user_ids: [], group_ids: [] }, default: () => null,
}));

import AskPage from "../AskPage";

const inquiry = (id: number, patch: Partial<Inquiry> = {}): Inquiry => ({
  id, kind: "sales_inquiry", inquiry_no: `XJ-${id}`, status: "submitted", version: 2, revision: 1,
  customer_id: id + 100, customer_name: `客户-${id}`, created_by: 3, is_owner: true,
  created_at: "2026-10-29T00:00:00Z", updated_at: "2026-10-29T00:00:00Z",
  line_count: 1, progress: { quoted_lines: 0, line_count: 1 }, can_urge: true,
  lines: [{ id: id + 10, inquiry_id: id, line_key: `line-${id}`, seq: 1, pn_raw: `PN-${id}`,
    qty: "1", status: "active", version: 1 }],
  offers: [], ...patch,
});

// 与真实详情/催办响应一致：客户 ID 仍可见时才允许补回列表中的名称。
const detailOf = (row: Inquiry): Inquiry => {
  const detail = { ...row };
  delete detail.customer_name;
  delete detail.is_owner;
  return detail;
};

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem("role", "sales");
  mocks.customers.mockResolvedValue([]);
  mocks.listWorkCandidates.mockResolvedValue({ items: [] });
  mocks.suggestParts.mockResolvedValue([]);
});
afterEach(async () => { await cleanupAntd(); vi.restoreAllMocks(); });

async function renderRows(rows: Inquiry[], details: Inquiry[]) {
  mocks.listInquiries.mockResolvedValue({ items: rows, total: rows.length, page: 1, page_size: 10 });
  mocks.getInquiry.mockImplementation(async (id: number) => details.find((row) => row.id === id));
  await act(async () => { render(<MemoryRouter><AskPage /></MemoryRouter>); });
  await screen.findByRole("link", { name: rows[0].inquiry_no });
}

const card = (id: number) => within(screen.getByRole("link", { name: `XJ-${id}` }).closest(".presales-inquiry-card") as HTMLElement);

describe("询价卡片客户名称", () => {
  it("成功加载详情后保留每单已授权的客户名，并使用详情中的当前行内容", async () => {
    const first = inquiry(1), second = inquiry(2);
    const firstDetail = detailOf(first), secondDetail = detailOf(second);
    firstDetail.lines = [{ ...first.lines![0], pn_raw: "PN-DETAIL-1" }];
    await renderRows([first, second], [firstDetail, secondDetail]);
    expect(card(1).getByText("客户-1")).toBeInTheDocument();
    expect(card(1).queryByText("客户-2")).not.toBeInTheDocument();
    expect(card(2).getByText("客户-2")).toBeInTheDocument();
    expect(card(2).queryByText("客户-1")).not.toBeInTheDocument();
    expect(card(1).getByText("PN-DETAIL-1", { exact: false })).toBeInTheDocument();
    expect(card(1).queryByText("PN-1", { exact: false })).not.toBeInTheDocument();
  });

  it.each([
    ["名称显式为 null", { customer_name: null }],
    ["客户 ID 被权限隐藏", { customer_id: undefined }],
    ["客户已清空", { customer_id: null }],
    ["客户已改为另一身份", { customer_id: 999 }],
  ] as const)("详情%s时不补回旧名称", async (_label, patch) => {
    const listed = inquiry(1);
    const detail = { ...detailOf(listed), ...patch };
    if (detail.customer_id === undefined) delete detail.customer_id;
    await renderRows([listed], [detail]);
    expect(card(1).queryByText("客户-1")).not.toBeInTheDocument();
  });

  it("详情显式返回新名称时采用新值", async () => {
    const listed = inquiry(1);
    await renderRows([listed], [{ ...detailOf(listed), customer_name: "新客户名称" }]);
    expect(card(1).getByText("新客户名称")).toBeInTheDocument();
    expect(card(1).queryByText("客户-1")).not.toBeInTheDocument();
  });

  it.each([
    ["仅省略名称", {}, true],
    ["显式清空名称", { customer_name: null }, false],
    ["隐藏客户身份", { customer_id: undefined }, false],
  ] as const)("催办响应%s时仍遵守最新客户可见性", async (_label, patch, retained) => {
    const listed = inquiry(1);
    await renderRows([listed], [detailOf(listed)]);
    expect(card(1).getByText("客户-1")).toBeInTheDocument();
    const urged = { ...detailOf(listed), ...patch, urge_count: 1, can_urge: false,
      last_urged_at: "2026-10-29T00:30:00Z" };
    if (urged.customer_id === undefined) delete urged.customer_id;
    mocks.urgeInquiry.mockResolvedValue(urged);
    fireEvent.click(card(1).getByRole("button", { name: /催\s?办/ }));
    await waitFor(() => expect(mocks.urgeInquiry).toHaveBeenCalledWith(1));
    await waitFor(() => expect(card(1).getByText(/已催 1 次/)).toBeInTheDocument());
    expect(card(1).queryByText("客户-1") !== null).toBe(retained);
    expect(card(1).queryByRole("button", { name: /催\s?办/ })).not.toBeInTheDocument();
  });
});
