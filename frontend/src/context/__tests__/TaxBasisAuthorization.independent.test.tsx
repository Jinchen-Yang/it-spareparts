import { useLayoutEffect } from "react";
import { act, cleanup, render, screen, waitFor } from "../../test/antdRender";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AuthzProvider, useAuthz } from "../../authz/AuthzProvider";
import { authzSnapshot } from "../../test/authzFixtures";
import { TaxBasisProvider, useTaxDisplayPolicy } from "../TaxBasis";
const mocks = vi.hoisted(() => ({ snapshot: vi.fn(), settings: vi.fn() }));
vi.mock("../../api/authz", async (original) => ({
  ...(await original<typeof import("../../api/authz")>()),
  getAuthzSnapshot: mocks.snapshot,
}));
vi.mock("../../api/systemSettings", () => ({
  getSystemSettings: mocks.settings,
}));
const observations: {
  ready: boolean;
  basis: string;
  version: string | undefined;
}[] = [];
let refresh: () => Promise<void>;
function Probe() {
  const authz = useAuthz(),
    tax = useTaxDisplayPolicy();
  refresh = authz.refresh;
  useLayoutEffect(() => {
    observations.push({
      ready: !tax.loading && !tax.loadFailed,
      basis: tax.policy.sales,
      version: authz.snapshot?.version,
    });
  });
  return (
    <div>
      {tax.loading ? "pending" : tax.loadFailed ? "failed" : tax.policy.sales}
    </div>
  );
}
function snapshot(version: string) {
  const value = authzSnapshot(
    {
      enabled: false,
      read: false,
      export: false,
      scope: "none",
      field_permissions: [],
      customer_count: 0,
      reason: "",
    },
    { version },
  );
  return value;
}
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("token", "tax-owner");
  observations.length = 0;
  mocks.snapshot.mockResolvedValue({ data: snapshot("v1") });
  mocks.settings.mockResolvedValue({
    data: {
      purchase_display_basis: "inc",
      sales_display_basis: "inc",
      maintenance_display_basis: "inc",
      version: 4,
    },
  });
});
afterEach(cleanup);
it("a batched authorization refresh never presents the default basis as a verified policy", async () => {
  render(
    <AuthzProvider token="tax-owner">
      <TaxBasisProvider>
        <Probe />
      </TaxBasisProvider>
    </AuthzProvider>,
  );
  await screen.findByText("inc");
  mocks.snapshot.mockResolvedValue({ data: snapshot("v2") });
  mocks.settings.mockImplementation(() => new Promise(() => {}));
  await act(async () => {
    await refresh();
  });
  await waitFor(() => expect(mocks.settings).toHaveBeenCalledTimes(2));
  expect(
    observations.filter(
      (x) => x.version === "v2" && x.ready && x.basis === "ex",
    ),
  ).toEqual([]);
});
