import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

const { post } = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("../../api", () => ({ api: { post } }));

import {
  PRICE_REF_CACHE_TTL_MS,
  clearPriceRefCache,
  fetchPriceRefs,
} from "../priceRefs";
import { clearSessionScopedPreferences } from "../../sessionPreferences";

const side = {
  avg_inc_tax: "113.00",
  avg_ex_tax: "100.00",
  sample_count: "2",
  order_count: "1",
  latest_date: "2026-10-19",
  oldest_date: "2026-10-12",
};

const item = {
  sale: side,
  purchase: { ...side, avg_inc_tax: "90.00", avg_ex_tax: "79.65" },
  purchase_restricted: false,
  inquiry_ref: {
    min_money: "10.00", max_money: "20.00", last_money: "15.00", count: "3",
  },
  window: { start: "2026-09-20", end: "2026-10-19", days: 30 },
  method: "time_decay_qty_weighted",
};

const responseFor = (ids: number[]) => ({
  data: { items: Object.fromEntries(ids.map((id) => [String(id), item])) },
});

beforeEach(() => {
  post.mockReset();
  post.mockImplementation(async (_url: string, body: { part_ids: number[] }) => (
    responseFor(body.part_ids)
  ));
  clearPriceRefCache();
  localStorage.clear();
  localStorage.setItem("token", "account-a");
});

afterEach(() => {
  vi.restoreAllMocks();
  clearPriceRefCache();
});

describe("价格参考批量客户端", () => {
  it("同一 tick 内多个调用合并为一次请求，并去重、按默认 30 天窗口", async () => {
    const [first, second, third] = await Promise.all([
      fetchPriceRefs([1]),
      fetchPriceRefs([2]),
      fetchPriceRefs([3, 2]),
    ]);

    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith("/parts/price-refs", {
      part_ids: [1, 2, 3],
      window_days: 30,
    });
    expect(first[1]).toMatchObject({
      sale: { avg_inc_tax: 113, avg_ex_tax: 100 },
      purchase: { avg_inc_tax: 90 },
      purchase_restricted: false,
    });
    expect(second[2]?.inquiry_ref.count).toBe(3);
    expect(third[3]).not.toBeNull();
  });

  it("按账号命中缓存；退出登录清空后重新请求", async () => {
    await fetchPriceRefs([1]);
    await fetchPriceRefs([1]);
    expect(post).toHaveBeenCalledTimes(1);

    clearSessionScopedPreferences();
    await fetchPriceRefs([1]);
    expect(post).toHaveBeenCalledTimes(2);
  });

  it("不同账号不复用缓存，切回原账号仍可命中", async () => {
    await fetchPriceRefs([1]);
    localStorage.setItem("token", "account-b");
    await fetchPriceRefs([1]);
    expect(post).toHaveBeenCalledTimes(2);

    localStorage.setItem("token", "account-a");
    await fetchPriceRefs([1]);
    expect(post).toHaveBeenCalledTimes(2);
  });

  it("缓存 TTL 到期后重新请求", async () => {
    let now = 1_000_000;
    vi.spyOn(Date, "now").mockImplementation(() => now);
    await fetchPriceRefs([1]);
    now += PRICE_REF_CACHE_TTL_MS;
    await fetchPriceRefs([1]);
    expect(post).toHaveBeenCalledTimes(2);
  });

  it("超过 200 个 id 拆成多批，返回全部结果", async () => {
    const ids = Array.from({ length: 205 }, (_value, index) => index + 1);
    const values = await fetchPriceRefs(ids);

    expect(post).toHaveBeenCalledTimes(2);
    expect(post.mock.calls[0][1].part_ids).toHaveLength(200);
    expect(post.mock.calls[1][1].part_ids).toHaveLength(5);
    expect(Object.keys(values)).toHaveLength(205);
  });

  it("空列表不发请求", async () => {
    await expect(fetchPriceRefs([])).resolves.toEqual({});
    expect(post).not.toHaveBeenCalled();
  });
});
