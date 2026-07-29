import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Grid } from "antd";
import { MemoryRouter } from "react-router-dom";

const fetchCancellationStats = vi.fn();
const getSystemSettings = vi.fn();

vi.mock("../../api", () => ({
  fetchCancellationStats: (...args: unknown[]) => fetchCancellationStats(...args),
}));
vi.mock("../../api/systemSettings", () => ({
  getSystemSettings: (...args: unknown[]) => getSystemSettings(...args),
}));

import { TaxBasisProvider } from "../../context/TaxBasis";
import PurchaseExceptionsPage from "../purchases/PurchaseExceptionsPage";

const breakpoint = vi.spyOn(Grid, "useBreakpoint");
const RESPONSE = {
  granularity: "month",
  statuses: ["已取消"],
  rows: [{
    period: "2026-03",
    total: 2,
    cancelled: 1,
    cancel_rate: 50,
    cancelled_amount_ex: 100,
    cancelled_amount_inc: 113,
    cancelled_amount: 100,
    by_status: {
      已取消: { count: 1, amount_ex: 100, amount_inc: 113, amount: 100 },
    },
  }],
  summary: {
    total: 2,
    cancelled: 1,
    cancel_rate: 50,
    cancelled_amount_ex: 100,
    cancelled_amount_inc: 113,
    cancelled_amount: 100,
  },
};

function renderPage() {
  return render(
    <MemoryRouter>
      <TaxBasisProvider>
        <PurchaseExceptionsPage />
      </TaxBasisProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  breakpoint.mockReturnValue({ xs: false, sm: true, md: true, lg: true, xl: true, xxl: true });
  fetchCancellationStats.mockResolvedValue({ data: RESPONSE });
  getSystemSettings.mockResolvedValue({ data: {
    purchase_display_basis: "both",
    sales_display_basis: "ex",
    maintenance_display_basis: "both",
    version: 2,
  } });
});

afterEach(cleanup);

describe("采购异常双税展示", () => {
  it("桌面端两列口径分别展示后端聚合的含税和未税取消金额", async () => {
    renderPage();
    expect(await screen.findByRole("columnheader", { name: "取消金额(含税)" }))
      .toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "取消金额(不含税)" }))
      .toBeInTheDocument();
    const row = screen.getByRole("row", { name: /2026-03/ });
    expect(row).toHaveTextContent("¥113");
    expect(row).toHaveTextContent("¥100");
  });

  it("移动端按管理员含税口径展示列表与详情抽屉", async () => {
    breakpoint.mockReturnValue({ xs: true, sm: false, md: false, lg: false, xl: false, xxl: false });
    getSystemSettings.mockResolvedValue({ data: {
      purchase_display_basis: "inc",
      sales_display_basis: "ex",
      maintenance_display_basis: "both",
      version: 2,
    } });
    renderPage();

    const row = await screen.findByRole("button", { name: "查看 2026-03 采购异常详情" });
    expect(row).toHaveTextContent("取消金额 ¥113");
    fireEvent.click(row);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText("取消金额(含税)").parentElement)
      .toHaveTextContent("¥113"));
    expect(within(dialog).queryByText("取消金额(不含税)")).toBeNull();
  });
});
