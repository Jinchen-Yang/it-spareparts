import { describe, expect, it, vi } from "vitest";
import { parseAnalyticsUrl, serializeAnalyticsState, useAnalyticsPatch } from "../../pages/purchases/analytics/urlState";
import { parseSalesAnalyticsUrl, serializeSalesAnalyticsState, useSalesAnalyticsPatch } from "../../pages/sales/analytics/urlState";
import { ANALYTICS_PAGE_MAX, ANALYTICS_PAGE_SIZES, applyAnalyticsUrlPatch, clampAnalyticsPage, parseAnalyticsPageSize, parsePositiveId, type UrlPatch } from "../analyticsUrlState";

function rebuild(patch: UrlPatch) {
  const sp = new URLSearchParams();
  for (const [key, value] of Object.entries(patch)) {
    if (Array.isArray(value)) value.forEach(item => sp.append(key, item));
    else if (value !== undefined) sp.set(key, value);
  }
  return sp;
}

describe("分析 URL 共享边界", () => {
  it.each([null, "", "-1", "abc", "2x", "1.5", "Infinity", "NaN", " 2 "])("页码非法 %s 回退 1，不截断为合法数", raw => {
    expect(clampAnalyticsPage(raw)).toBe(1);
  });
  it.each(["10001", "999999999999999999999999999", "9".repeat(400)])("超大页码 %s 收敛上界", raw => {
    expect(clampAnalyticsPage(raw)).toBe(ANALYTICS_PAGE_MAX);
  });
  it.each([null, "", "-20", "abc", "50x", "50.5", "99999999", "0"])("每页条数非法 %s 回退 50", raw => {
    expect(parseAnalyticsPageSize(raw)).toBe(50);
  });
  it.each(ANALYTICS_PAGE_SIZES)("每页条数白名单 %s 保留", value => {
    expect(parseAnalyticsPageSize(String(value))).toBe(value);
  });
  it.each([null, "", "-1", "0", "12.5", "12x", "2147483648"])("标识 %s 严格校验，非法保持 null", raw => {
    expect(parsePositiveId(raw)).toBeNull();
  });
});

function testUrlContract<T extends { page: number; pageSize: number }>(
  name: string,
  parse: (sp: URLSearchParams) => T,
  serialize: (state: Partial<T>) => UrlPatch,
  usePatch: typeof useAnalyticsPatch | typeof useSalesAnalyticsPatch,
  listKey: string,
  stateKey: string,
) {
  describe(`${name} URL 契约`, () => {
    it("重复键去空去重、旧逗号链接兼容，原值空格不被 trim", () => {
      const parsed = parse(new URLSearchParams(`${listKey}=a,b&${listKey}=b&${listKey}=&${listKey}=%20c%20`));
      expect((parsed as unknown as Record<string, unknown>)[stateKey]).toEqual(["a", "b", " c "]);
    });
    it("新格式含逗号的单个值保持原样，重复写回稳定", () => {
      const sp = new URLSearchParams(`url_version=2&${listKey}=a%2Cb&${listKey}=c`);
      const state = parse(sp);
      expect((state as unknown as Record<string, unknown>)[stateKey]).toEqual(["a,b", "c"]);
      const encoded = rebuild(serialize(state));
      expect(encoded.getAll(listKey)).toEqual(["a,b", "c"]);
      expect(encoded.get("url_version")).toBe("2");
      expect(parse(encoded)).toEqual(state);
      const single = parse(new URLSearchParams(`url_version=2&${listKey}=a%2Cb`));
      expect(parse(rebuild(serialize(single)))).toEqual(single);
    });
    it.each([
      "page=&page=4&page_size=&page_size=20",
      "page=-1&page_size=42&order_id=12x",
      "page=100000000000000000000&page_size=200",
      "preset=all&from=2026-01-01&to=2026-03-01",
      "preset=custom&from=invalid&to=invalid",
      "preset=custom&from=2026-01-01",
      "basis=inc,ex&basis=inc",
      "basis=paid,ex,inc&basis=revenue",
      "unknown=value&sort=bad",
    ])("解析→序列化→解析幂等：%s", query => {
      const state = parse(new URLSearchParams(query));
      expect(parse(rebuild(serialize(state)))).toEqual(state);
    });
    it("单值重复键取首值；页码与白名单每页条数可持久化", () => {
      const state = parse(new URLSearchParams("page=3&page=4&page_size=100&page_size=20"));
      expect(state).toMatchObject({ page: 3, pageSize: 100 });
      expect(rebuild(serialize(state)).get("page_size")).toBe("100");
    });
    it("筛选/分页/排序 replace，下钻 push；保留未知参数与已有多值", () => {
      const setSp = vi.fn();
      const original = new URLSearchParams(`unknown=kept&${listKey}=a,b&page=7`);
      const patch = usePatch(original, setSp);
      patch({ page: 2, pageSize: 100 });
      expect(setSp).toHaveBeenLastCalledWith(expect.any(URLSearchParams), { replace: true });
      const encoded = setSp.mock.calls[setSp.mock.calls.length - 1][0] as URLSearchParams;
      expect(encoded.get("unknown")).toBe("kept");
      expect(encoded.getAll(listKey)).toEqual(["a", "b"]);
      expect(encoded.get("page_size")).toBe("100");
      expect(parse(encoded)).toMatchObject({ page: 2, pageSize: 100 });
      patch({ orderId: 12 }, { history: "push" });
      expect(setSp).toHaveBeenLastCalledWith(expect.any(URLSearchParams), { replace: false });
      patch({ page: 2 }, { resetPage: true });
      expect((setSp.mock.calls[setSp.mock.calls.length - 1][0] as URLSearchParams).has("page")).toBe(false);
      expect(rebuild(serialize(parse(original))).has("unknown")).toBe(false);
    });
    it("将某多值改为 v2 时其他旧多值同步升级，不把旧列表误当单值", () => {
      const setSp = vi.fn();
      const patch = usePatch(new URLSearchParams(`${listKey}=a,b&basis=inc,ex`), setSp);
      patch({ page: 3 });
      const encoded = setSp.mock.calls[0][0] as URLSearchParams;
      expect(encoded.getAll(listKey)).toEqual(["a", "b"]);
      expect(encoded.getAll("basis")).toEqual(["inc", "ex"]);
      expect(encoded.get("url_version")).toBe("2");
    });
  });
}

testUrlContract("采购", parseAnalyticsUrl, serializeAnalyticsState, useAnalyticsPatch, "category", "category");
testUrlContract("销售", parseSalesAnalyticsUrl, serializeSalesAnalyticsState, useSalesAnalyticsPatch, "business_type", "businessType");

it("删除末个多值时清除版本标记，未知参数仍保留", () => {
  const encoded = applyAnalyticsUrlPatch(new URLSearchParams("url_version=2&category=a&unknown=1"), { category: undefined }, ["category"]);
  expect(encoded.toString()).toBe("unknown=1");
});


it("销售客户/销售人员是单值，升级多值链接不拆其中逗号", () => {
  const setSp = vi.fn();
  useSalesAnalyticsPatch(new URLSearchParams("customer=a,b&salesperson=c,d&business_type=e,f"), setSp)({ page: 2 });
  const encoded = setSp.mock.calls[0][0] as URLSearchParams;
  expect(parseSalesAnalyticsUrl(encoded)).toMatchObject({ customer: "a,b", salesperson: "c,d", businessType: ["e", "f"] });
});

it("采购客户/销售人员在增量升级时也使用重复键", () => {
  const setSp = vi.fn();
  useAnalyticsPatch(new URLSearchParams("customer=12,7&salesperson=a,b"), setSp)({ page: 2 });
  const encoded = setSp.mock.calls[0][0] as URLSearchParams;
  expect(encoded.getAll("customer")).toEqual(["12", "7"]);
  expect(encoded.getAll("salesperson")).toEqual(["a", "b"]);
  expect(parseAnalyticsUrl(encoded)).toMatchObject({ customerIds: [12, 7], salesperson: ["a", "b"] });
});
