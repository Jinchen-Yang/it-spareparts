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

it("sends the selected basis with the expected version", () => {
  updateSystemSettings({
    maintenance_project_profit_default_basis: "ex",
    expected_version: 7,
  });
  expect(put).toHaveBeenCalledWith("/system-settings", {
    maintenance_project_profit_default_basis: "ex",
    expected_version: 7,
  });
});
