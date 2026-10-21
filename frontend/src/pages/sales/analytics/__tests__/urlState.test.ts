// 销售分析 URL 状态层（D-41）：解析容错 / 序列化默认省略 / 双向往返 /
// 状态 → 请求筛选（「全部」不传日期、默认今天、business_type 原值不映射不 trim）/
// 请求参数序列化（重复键形态，含退货只在上开时落键）。
import { describe, expect, it, vi } from "vitest";
import dayjs from "dayjs";

import {
  ALL_AMOUNTS,
  DEFAULT_SORT,
  parseSalesAnalyticsUrl,
  serializeSalesAnalyticsState,
  toSalesApiFilters,
} from "../urlState";
import { toSalesAnalyticsParams } from "../../../../api/salesAnalytics";

const sp = (qs: string) => new URLSearchParams(qs);
const TODAY = dayjs("2026-10-17");

describe("parseSalesAnalyticsUrl：默认与容错", () => {
  it("零参数 = 今天 + 三金额并排 + 不含税金额倒序 + 第 1 页 + 不含退货", () => {
    const s = parseSalesAnalyticsUrl(sp(""));
    expect(s).toEqual({
      preset: "today",
      from: null,
      to: null,
      businessType: [],
      customer: null,
      salesperson: null,
      amounts: [...ALL_AMOUNTS],
      sort: DEFAULT_SORT,
      page: 1,
      pageSize: 50,
      includeReturns: false,
      orderId: null,
      // D-44 新键默认
      customers: [], customerTypes: [], salespeople: [], categories: [], brands: [],
      amountBasis: "amount_ex_tax", amountMin: null, amountMax: null,
      includeNegative: true, includeEmpty: true, order: "desc", tab: "orders",
      groupBy: "customer", groupSort: "amount_ex_tax", groupOrder: "desc", groupTop: 20,
      granularity: null, compare: true,
    });
  });

  it("URL 野值一律回默认：未知 preset / sort / page / order_id / basis", () => {
    const s = parseSalesAnalyticsUrl(sp(
      "preset=forever&sort=weight&page=-3&order_id=abc&basis=gold&include_returns=true",
    ));
    expect(s.preset).toBe("today");
    expect(s.sort).toBe(DEFAULT_SORT);
    expect(s.page).toBe(1);
    expect(s.orderId).toBeNull();
    expect(s.amounts).toEqual([...ALL_AMOUNTS]);
    expect(s.includeReturns).toBe(false); // 只有字面 "1" 才算开
  });

  it("business_type 多选去重去空、不 trim（原值精确匹配口径，首尾空格是取值的一部分）", () => {
    expect(parseSalesAnalyticsUrl(sp(
      "business_type=%E6%9C%8D%E5%8A%A1,%E6%9C%8D%E5%8A%A1,,%20%E5%A4%87%E4%BB%B6%E9%94%80%E5%94%AE%20",
    )).businessType).toEqual(["服务", " 备件销售 "]);
  });

  it("from/to 只有两个都在时才有意义（单边视为残缺）", () => {
    expect(parseSalesAnalyticsUrl(sp("preset=custom&from=2026-09-01")).from).toBeNull();
    expect(parseSalesAnalyticsUrl(sp("preset=custom&to=2026-09-01")).to).toBeNull();
    expect(parseSalesAnalyticsUrl(sp("preset=custom&from=2026-09-01&to=2026-10-15")))
      .toMatchObject({ from: "2026-09-01", to: "2026-10-15" });
  });

  it("page / order_id 边界：1 与非法都归默认，合法保留", () => {
    expect(parseSalesAnalyticsUrl(sp("page=1&page=0&page=abc")).page).toBe(1);
    expect(parseSalesAnalyticsUrl(sp("page=3")).page).toBe(3);
    // 上界与后端 ANALYTICS_PAGE_MAX 一致：超大页码收敛到 10000（不发必 422 的请求）
    expect(parseSalesAnalyticsUrl(sp("page=10000")).page).toBe(10000);
    expect(parseSalesAnalyticsUrl(sp("page=10001")).page).toBe(10000);
    expect(parseSalesAnalyticsUrl(sp("page=99999999999999999999")).page).toBe(10000);
    expect(parseSalesAnalyticsUrl(sp("order_id=0")).orderId).toBeNull();
    expect(parseSalesAnalyticsUrl(sp("order_id=201")).orderId).toBe(201);
  });

  it("basis 只保留合法值；全非法 → 默认三金额（受限账号也不会得到空口径）", () => {
    expect(parseSalesAnalyticsUrl(sp("basis=inc,revenue")).amounts).toEqual(["inc", "revenue"]);
    expect(parseSalesAnalyticsUrl(sp("basis=inc,x")).amounts).toEqual(["inc"]);
    expect(parseSalesAnalyticsUrl(sp("basis=,")).amounts).toEqual([...ALL_AMOUNTS]);
  });
});

describe("serializeSalesAnalyticsState：默认值不落 URL", () => {
  it("preset=today / sort=默认键 / page=1 / 含退货关 / 三金额全选 都转 undefined", () => {
    expect(serializeSalesAnalyticsState({
      preset: "today",
      sort: DEFAULT_SORT,
      page: 1,
      pageSize: 50,
      includeReturns: false,
      amounts: [...ALL_AMOUNTS],
    })).toEqual({
      preset: undefined, from: undefined, to: undefined,
      sort: undefined, page: undefined, include_returns: undefined, basis: undefined,
    });
  });

  it("非自定义预设把残留 from/to 一并清掉；custom 保留区间", () => {
    expect(serializeSalesAnalyticsState({ preset: "all", from: "2026-01-01", to: "2026-02-01" }))
      .toEqual({ preset: "all", from: undefined, to: undefined });
    expect(serializeSalesAnalyticsState({ preset: "custom", from: "2026-09-01", to: "2026-10-15" }))
      .toEqual({ preset: "custom", from: "2026-09-01", to: "2026-10-15" });
  });

  it("business_type 空数组 → 删键；非空 → 重复参数值数组 + v2 标记（原值含空格也原样保留）", () => {
    expect(serializeSalesAnalyticsState({ businessType: [] }))
      .toEqual({ business_type: undefined });
    expect(serializeSalesAnalyticsState({ businessType: ["服务", " 备件销售 "] }))
      .toEqual({ business_type: ["服务", " 备件销售 "], url_version: "2" });
  });

  it("customer / salesperson：空串与 null 都转 undefined（不落空键）", () => {
    expect(serializeSalesAnalyticsState({ customer: null, salesperson: "" }))
      .toEqual({ customer: undefined, salesperson: undefined });
    expect(serializeSalesAnalyticsState({ customer: "客户甲" }))
      .toEqual({ customer: "客户甲" });
  });

  it("basis 子集才落键；include_returns=true 落 1；order_id 关抽屉时删键", () => {
    expect(serializeSalesAnalyticsState({ amounts: ["ex"] })).toEqual({ basis: ["ex"], url_version: "2" });
    expect(serializeSalesAnalyticsState({ includeReturns: true }))
      .toEqual({ include_returns: "1" });
    expect(serializeSalesAnalyticsState({ orderId: null })).toEqual({ order_id: undefined });
    expect(serializeSalesAnalyticsState({ orderId: 88 })).toEqual({ order_id: "88" });
  });
});

describe("URL 双向往返（parse ∘ serialize = 恒等）", () => {
  const cases: string[] = [
    // 完整形态：自定义区间 + 业务类型多选 + 客户/销售人员 + 金额子集 + 排序 + 翻页 + 含退货 + 抽屉
    "preset=custom&from=2026-09-01&to=2026-10-15"
    + "&business_type=%E5%A4%87%E4%BB%B6%E9%94%80%E5%94%AE,%E6%9C%8D%E5%8A%A1"
    + "&customer=%E5%AE%A2%E6%88%B7%E7%94%B2&salesperson=%E7%8E%8B%E4%BA%94"
    + "&basis=ex,revenue&sort=amount_inc_tax&page=4&include_returns=1&order_id=201",
    "preset=all",
    "preset=7d&business_type=%E6%9C%8D%E5%8A%A1",
    "preset=yesterday&basis=revenue&sort=revenue",
    "preset=custom&from=2026-09-01&to=2026-10-15&include_returns=1",
  ];
  for (const qs of cases) {
    it(qs.slice(0, 48) + (qs.length > 48 ? "…" : ""), () => {
      const state = parseSalesAnalyticsUrl(sp(qs));
      // 把完整状态按段序列化回 URL，再解析，应与首次解析一致
      const full = serializeSalesAnalyticsState(state);
      const rebuilt = new URLSearchParams();
      for (const [k, v] of Object.entries(full)) {
        if (Array.isArray(v)) v.forEach(item => rebuilt.append(k, item));
        else if (v !== undefined) rebuilt.set(k, v);
      }
      expect(parseSalesAnalyticsUrl(rebuilt)).toEqual(state);
      // 原始旧链接只改变多选编码与版本标记；其余键必须完整保留（排序后比较排除键顺序）。
      const expected = new URLSearchParams(qs);
      let hasList = false;
      for (const key of ["business_type", "basis"]) {
        if (!expected.has(key)) continue;
        hasList = true;
        const values = [...new Set(expected.getAll(key).flatMap(value => value.split(",")).filter(Boolean))];
        expected.delete(key);
        values.forEach(value => expected.append(key, value));
      }
      if (hasList) expected.set("url_version", "2");
      const actual = new URLSearchParams(rebuilt);
      expected.sort();
      actual.sort();
      expect(actual.toString()).toBe(expected.toString());
      // 新格式必须用重复参数：已知多值键逐项保真，不再写旧逗号列表。
      for (const [key, value] of Object.entries(full)) {
        if (Array.isArray(value)) expect(rebuilt.getAll(key)).toEqual(value);
        else expect(rebuilt.get(key)).toBe(value ?? null);
      }
    });
  }
  it("URL 里显式写默认值 → 归一化为零参数（分享链接不携带冗余）", () => {
    const state = parseSalesAnalyticsUrl(sp(
      "preset=today&sort=amount_ex_tax&page=1&include_returns=0&basis=ex,inc,revenue",
    ));
    const full = serializeSalesAnalyticsState(state);
    expect(Object.values(full).every((v) => v === undefined)).toBe(true);
    expect(parseSalesAnalyticsUrl(sp(""))).toEqual(state);
  });
});

describe("toSalesApiFilters：状态 → 请求筛选", () => {
  it("默认（今天）→ date_from = date_to = 今天", () => {
    expect(toSalesApiFilters(parseSalesAnalyticsUrl(sp("")), TODAY)).toEqual({
      date_from: "2026-10-17",
      date_to: "2026-10-17",
    });
  });

  it("「全部」→ 不传任何日期键（后端约定缺省即全部历史）", () => {
    const f = toSalesApiFilters(parseSalesAnalyticsUrl(sp("preset=all")), TODAY);
    expect(f).toEqual({});
    expect("date_from" in f).toBe(false);
    expect("date_to" in f).toBe(false);
  });

  it("各预设解析为对应闭区间", () => {
    expect(toSalesApiFilters(parseSalesAnalyticsUrl(sp("preset=7d")), TODAY))
      .toEqual({ date_from: "2026-10-11", date_to: "2026-10-17" });
    expect(toSalesApiFilters(parseSalesAnalyticsUrl(sp("preset=yesterday")), TODAY))
      .toEqual({ date_from: "2026-10-16", date_to: "2026-10-16" });
    expect(toSalesApiFilters(parseSalesAnalyticsUrl(sp("preset=1y")), TODAY))
      .toEqual({ date_from: "2025-10-17", date_to: "2026-10-17" });
  });

  it("自定义非法回退今天；合法原样透传", () => {
    expect(toSalesApiFilters(parseSalesAnalyticsUrl(sp("preset=custom&from=2026-02-31&to=2026-03-05")), TODAY))
      .toEqual({ date_from: "2026-10-17", date_to: "2026-10-17" });
    expect(toSalesApiFilters(parseSalesAnalyticsUrl(sp("preset=custom&from=2026-09-01&to=2026-10-15")), TODAY))
      .toEqual({ date_from: "2026-09-01", date_to: "2026-10-15" });
  });

  it("business_type 原值全量进请求（不 trim 不映射）；客户/销售人员 trim 后才发，纯空白视为未筛", () => {
    expect(toSalesApiFilters(parseSalesAnalyticsUrl(sp(
      "business_type=%20%E5%A4%87%E4%BB%B6%E9%94%80%E5%94%AE%20&customer=%20%E5%AE%A2%E6%88%B7%E7%94%B2%20&salesperson=%20%20",
    )), TODAY)).toEqual({
      date_from: "2026-10-17",
      date_to: "2026-10-17",
      business_type: [" 备件销售 "], // 精确匹配口径：不 trim
      customer: "客户甲", // ILIKE 子串：首尾空白剔除
    });
  });
});

describe("toSalesAnalyticsParams：URLSearchParams 序列化（FastAPI 重复键形态）", () => {
  it("business_type 多选用重复键 k=v1&k=v2，绝不出 k[]=v（后端解析不出）", () => {
    const p = toSalesAnalyticsParams(
      {
        date_from: "2026-10-17",
        date_to: "2026-10-17",
        business_type: ["备件销售", "服务"],
        customer: "客户甲",
      },
      { sort: "amount_inc_tax", page: 2, page_size: 50, include_returns: true },
    );
    const qs = p.toString();
    expect(qs).toContain("business_type=%E5%A4%87%E4%BB%B6%E9%94%80%E5%94%AE");
    expect(qs).toContain("business_type=%E6%9C%8D%E5%8A%A1");
    expect(qs).toContain("customer=%E5%AE%A2%E6%88%B7%E7%94%B2");
    expect(qs).toContain("sort=amount_inc_tax&page=2&page_size=50&include_returns=true");
    expect(qs).not.toContain("[]");
    expect(p.getAll("business_type")).toEqual(["备件销售", "服务"]);
  });

  it("「全部」不传日期：序列化结果不含 date_from/date_to", () => {
    const p = toSalesAnalyticsParams({});
    expect(p.toString()).toBe("");
    expect(p.has("date_from")).toBe(false);
    expect(p.has("date_to")).toBe(false);
  });

  it("include_returns=false / 空串 / 空值不落键（脏值与缺省不进请求）", () => {
    const p = toSalesAnalyticsParams(
      { business_type: ["", "服务"] },
      { sort: "", page: 0, include_returns: false },
    );
    expect(p.getAll("business_type")).toEqual(["服务"]);
    expect(p.has("sort")).toBe(false);
    expect(p.has("page")).toBe(false);
    expect(p.has("include_returns")).toBe(false);
  });
});

describe("toSalesApiFilters：默认「今天」按 Asia/Shanghai（与共享 DateRangePresets 同口径）", () => {
  it("不传 today 时用业务日：UTC 16:30 = 上海次日", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-10-17T16:30:00Z"));
      const f = toSalesApiFilters(parseSalesAnalyticsUrl(new URLSearchParams("")));
      expect(f).toMatchObject({ date_from: "2026-10-18", date_to: "2026-10-18" });
    } finally {
      vi.useRealTimers();
    }
  });
});
