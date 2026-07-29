import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

const getSystemSettings = vi.fn();

vi.mock("../../api/systemSettings", () => ({
  getSystemSettings: (...args: unknown[]) => getSystemSettings(...args),
}));

import {
  announceTaxDisplayPolicyChanged,
  TaxBasisProvider,
  TaxMoney,
  TaxPolicyBoundary,
  useTaxBasis,
  useTaxDisplayPolicy,
} from "../TaxBasis";

function Probe() {
  const purchase = useTaxBasis("purchase");
  const sales = useTaxBasis("sales");
  const maintenance = useTaxBasis("maintenance");
  return <div>{purchase}|{sales}|{maintenance}</div>;
}

function RefreshAtLeastProbe({ version }: { version: number }) {
  const { refresh } = useTaxDisplayPolicy();
  return (
    <button onClick={() => void refresh(version)}>
      refresh-at-least-{version}
    </button>
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("uses the administrator's three independent server policies and ignores legacy local storage", async () => {
  localStorage.setItem("tax_basis", "inc");
  localStorage.setItem("maintenance_project_profit_basis", "ex");
  getSystemSettings.mockResolvedValue({
    data: {
      purchase_display_basis: "inc",
      sales_display_basis: "ex",
      maintenance_display_basis: "both",
      version: 2,
    },
  });

  render(<TaxBasisProvider><Probe /></TaxBasisProvider>);

  expect(await screen.findByText("inc|ex|both")).toBeInTheDocument();
});

it("keeps fallback policy internal but blocks monetary content when policy loading fails", async () => {
  getSystemSettings.mockRejectedValue(new Error("network down"));

  render(
    <TaxBasisProvider>
      <Probe />
      <TaxMoney scope="sales" inc={113} ex={100} />
      <TaxPolicyBoundary><div>敏感金额 ¥999</div></TaxPolicyBoundary>
    </TaxBasisProvider>,
  );

  expect(await screen.findByText("both|ex|both")).toBeInTheDocument();
  expect(screen.getByText("—")).toBeInTheDocument();
  expect(screen.getByText("金额展示口径加载失败")).toBeInTheDocument();
  expect(screen.queryByText("敏感金额 ¥999")).toBeNull();
});

it("retries a failed policy read and releases the page only after a valid response", async () => {
  getSystemSettings
    .mockRejectedValueOnce(new Error("network down"))
    .mockResolvedValueOnce({
      data: {
        purchase_display_basis: "inc",
        sales_display_basis: "ex",
        maintenance_display_basis: "both",
        version: 2,
      },
    });

  render(
    <TaxBasisProvider>
      <TaxPolicyBoundary><div>金额页面已就绪</div></TaxPolicyBoundary>
    </TaxBasisProvider>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "重新加载" }));
  expect(await screen.findByText("金额页面已就绪")).toBeInTheDocument();
  expect(getSystemSettings).toHaveBeenCalledTimes(2);
});

it("refreshes a mounted login session when its tab becomes active", async () => {
  getSystemSettings
    .mockResolvedValueOnce({
      data: {
        purchase_display_basis: "both",
        sales_display_basis: "ex",
        maintenance_display_basis: "both",
        version: 2,
      },
    })
    .mockResolvedValueOnce({
      data: {
        purchase_display_basis: "inc",
        sales_display_basis: "both",
        maintenance_display_basis: "ex",
        version: 3,
      },
    });

  render(<TaxBasisProvider><Probe /></TaxBasisProvider>);
  expect(await screen.findByText("both|ex|both")).toBeInTheDocument();

  window.dispatchEvent(new Event("focus"));

  expect(await screen.findByText("inc|both|ex")).toBeInTheDocument();
  expect(getSystemSettings).toHaveBeenCalledTimes(2);
});

it("fails closed when a background policy refresh cannot verify the organization setting", async () => {
  getSystemSettings
    .mockResolvedValueOnce({
      data: {
        purchase_display_basis: "both",
        sales_display_basis: "ex",
        maintenance_display_basis: "both",
        version: 2,
      },
    })
    .mockRejectedValueOnce(new Error("network down"));

  render(
    <TaxBasisProvider>
      <TaxPolicyBoundary><div>敏感金额 ¥999</div></TaxPolicyBoundary>
    </TaxBasisProvider>,
  );
  expect(await screen.findByText("敏感金额 ¥999")).toBeInTheDocument();

  window.dispatchEvent(new Event("focus"));

  await waitFor(() => {
    expect(screen.getByText("金额展示口径加载失败")).toBeInTheDocument();
  });
  expect(screen.queryByText("敏感金额 ¥999")).toBeNull();
});

it("does not report a saved setting as failed when cross-tab notification is unavailable", () => {
  vi.stubGlobal("BroadcastChannel", class {
    constructor() {
      throw new Error("transport unavailable");
    }
  });

  expect(() => announceTaxDisplayPolicyChanged(3)).not.toThrow();
});

it("does not let a save join a stale in-flight read below the committed version", async () => {
  const staleRead = deferred<{
    data: {
      purchase_display_basis: "both";
      sales_display_basis: "ex";
      maintenance_display_basis: "both";
      version: number;
    };
  }>();
  getSystemSettings
    .mockResolvedValueOnce({
      data: {
        purchase_display_basis: "both",
        sales_display_basis: "ex",
        maintenance_display_basis: "both",
        version: 2,
      },
    })
    .mockReturnValueOnce(staleRead.promise)
    .mockResolvedValueOnce({
      data: {
        purchase_display_basis: "inc",
        sales_display_basis: "both",
        maintenance_display_basis: "ex",
        version: 3,
      },
    });

  render(
    <TaxBasisProvider>
      <Probe />
      <RefreshAtLeastProbe version={3} />
    </TaxBasisProvider>,
  );
  expect(await screen.findByText("both|ex|both")).toBeInTheDocument();

  window.dispatchEvent(new Event("focus"));
  await waitFor(() => expect(getSystemSettings).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByRole("button", { name: "refresh-at-least-3" }));
  staleRead.resolve({
    data: {
      purchase_display_basis: "both",
      sales_display_basis: "ex",
      maintenance_display_basis: "both",
      version: 2,
    },
  });

  expect(await screen.findByText("inc|both|ex")).toBeInTheDocument();
  expect(getSystemSettings).toHaveBeenCalledTimes(3);
});

it("rejects an incomplete or invalid server policy instead of mixing in client defaults", async () => {
  getSystemSettings.mockResolvedValue({
    data: {
      purchase_display_basis: "invalid",
      sales_display_basis: "ex",
      maintenance_display_basis: "both",
      version: 2,
    },
  });

  render(
    <TaxBasisProvider>
      <TaxPolicyBoundary><div>敏感金额 ¥999</div></TaxPolicyBoundary>
    </TaxBasisProvider>,
  );

  expect(await screen.findByText("金额展示口径加载失败")).toBeInTheDocument();
  expect(screen.queryByText("敏感金额 ¥999")).toBeNull();
});
