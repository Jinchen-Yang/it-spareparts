import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, dashboardPriceDisciplineSummary } from "../../api";

const get = vi.spyOn(api, "get");

beforeEach(() => vi.clearAllMocks());

describe("老板看板价格纪律 API 契约", () => {
  it("按同一闭区间调用唯一摘要端点", async () => {
    get.mockResolvedValueOnce({ data: { restricted: false } });
    const params = { date_from: "2026-07-01", date_to: "2026-07-15" };
    await dashboardPriceDisciplineSummary(params);
    expect(get).toHaveBeenCalledWith("/dashboard/price-discipline-summary", { params });
  });
});
