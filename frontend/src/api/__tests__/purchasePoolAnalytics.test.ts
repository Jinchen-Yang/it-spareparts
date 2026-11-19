import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api";
import { fetchPoolMembers, fetchPoolSources } from "../purchasePoolAnalytics";

vi.mock("../../api", () => ({ api: { get: vi.fn() } }));
afterEach(() => vi.resetAllMocks());

describe("pool evidence unit wire contract", () => {
  it.each([
    ["sources", fetchPoolSources, { part_id: 7 }],
    ["members", fetchPoolMembers, { pool_id: 11 }],
  ] as const)("%s omits a null unit; the endpoint treats absence as IS NULL", async (route, fetch, identity) => {
    vi.mocked(api.get).mockResolvedValue({ data: { items: [], has_more: false } });
    await fetch({ date_from: "2026-01-01" }, { ...identity, unit: null, scope: '{"buyer":"合成采购员"}' });
    const [path, config] = vi.mocked(api.get).mock.calls[0];
    expect(path).toBe(`/purchases/analytics/pool/${route}`);
    const params = config?.params as URLSearchParams;
    expect(params.has("unit")).toBe(false);
    expect(params.get("scope")).toBe('{"buyer":"合成采购员"}');
    expect(params.get("date_from")).toBe("2026-01-01");

    await fetch({}, { ...identity, unit: "件" });
    expect((vi.mocked(api.get).mock.calls[1][1]?.params as URLSearchParams).get("unit")).toBe("件");
    await fetch({}, { ...identity, unit: null, scope: '{"unit":null}' });
    expect((vi.mocked(api.get).mock.calls[2][1]?.params as URLSearchParams).get("scope")).toBe('{"unit":null}');
  });
});
