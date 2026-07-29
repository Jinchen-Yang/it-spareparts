import { beforeEach, expect, it, vi } from "vitest";

const get = vi.fn();
const put = vi.fn();

vi.mock("../../api", () => ({
  api: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
  },
}));

import {
  getSystemSettings,
  updateSystemSettings,
} from "../systemSettings";

beforeEach(() => {
  vi.clearAllMocks();
});

it("loads the typed singleton endpoint", () => {
  getSystemSettings();
  expect(get).toHaveBeenCalledWith("/system-settings");
});

it("sends all three administrator-controlled bases atomically with the expected version", () => {
  updateSystemSettings({
    purchase_display_basis: "both",
    sales_display_basis: "ex",
    maintenance_display_basis: "inc",
    expected_version: 7,
  });
  expect(put).toHaveBeenCalledWith("/system-settings", {
    purchase_display_basis: "both",
    sales_display_basis: "ex",
    maintenance_display_basis: "inc",
    expected_version: 7,
  });
});
