// 采购分析 URL 状态层（D-39）：解析容错 / 序列化默认省略 / 双向往返 /
// 状态 → 请求参数（「全部」不传日期、默认今天、多选全量传递）。
import { describe, expect, it, vi } from "vitest";
import dayjs from "dayjs";

import {
  DEFAULT_GROUP_BY,
  parseAnalyticsUrl,
  serializeAnalyticsState,
  toAnalyticsApiFilters,
} from "../urlState";
import { toAnalyticsParams } from "../../../../api/purchaseAnalytics";

const sp = (qs: string) => new URLSearchParams(qs);
const TODAY = dayjs("2026-10-17");

describe("parseAnalyticsUrl：默认与容错", () => {
  it("零参数 = 今天 + 采购类型分组 + 三口径并排 + 第 1 页 + 日期倒序", () => {
    const s = parseAnalyticsUrl(sp(""));
    expect(s).toEqual({
      preset: "today",
      from: null,
      to: null,
      sourceType: [],
      purchaser: [],
      salesperson: [],
      customerIds: [],
      category: [],
      groupBy: DEFAULT_GROUP_BY,
      groupKey: null,
      sort: "order_date",
      dir: "desc",
      page: 1,
      pageSize: 50,
      basis: ["inc", "ex", "paid"],
      orderId: null,
    });
  });

  it("URL 野值一律回默认：未知 preset / group_by / sort / dir / page / order_id / basis", () => {
    const s = parseAnalyticsUrl(sp(
      "preset=forever&group_by=vendor&sort=weight&dir=sideways&page=-3&order_id=abc&basis=gold",
    ));
    expect(s.preset).toBe("today");
    expect(s.groupBy).toBe(DEFAULT_GROUP_BY);
    expect(s.sort).toBe("order_date");
    expect(s.dir).toBe("desc");
    expect(s.page).toBe(1);
    expect(s.orderId).toBeNull();
    expect(s.basis).toEqual(["inc", "ex", "paid"]);
  });

  it("customer 只收正整数 id；多选去重去空（小数 ID 被拒绝）", () => {
    const s = parseAnalyticsUrl(sp("customer=12,abc,7,,7,-1,12.5,0"));
    expect(s.customerIds).toEqual([12, 7]);
  });

  it("from/to 只有两个都在时才有意义（单边视为残缺）", () => {
    expect(parseAnalyticsUrl(sp("preset=custom&from=2026-09-01")).from).toBeNull();
    expect(parseAnalyticsUrl(sp("preset=custom&to=2026-09-01")).to).toBeNull();
    expect(parseAnalyticsUrl(sp("preset=custom&from=2026-09-01&to=2026-10-15"))).toMatchObject({
      from: "2026-09-01",
      to: "2026-10-15",
    });
  });

  it("page=1 / page=2 边界：1 与非法都归 1，>1 保留", () => {
    expect(parseAnalyticsUrl(sp("page=1")).page).toBe(1);
    expect(parseAnalyticsUrl(sp("page=0")).page).toBe(1);
    expect(parseAnalyticsUrl(sp("page=3")).page).toBe(3);
  });

  it("basis 只保留合法值；全非法 → 默认三口径（受限账号也不会得到空口径）", () => {
    expect(parseAnalyticsUrl(sp("basis=inc,paid")).basis).toEqual(["inc", "paid"]);
    expect(parseAnalyticsUrl(sp("basis=inc,x")).basis).toEqual(["inc"]);
    expect(parseAnalyticsUrl(sp("basis=,")).basis).toEqual(["inc", "ex", "paid"]);
  });
});

describe("serializeAnalyticsState：默认值不落 URL", () => {
  it("筛选空数组 → 删键；非空 → 重复参数值数组 + v2 标记", () => {
    expect(serializeAnalyticsState({ sourceType: [] })).toEqual({ source_type: undefined });
    expect(serializeAnalyticsState({ sourceType: ["备件采购", "批量采购"] }))
      .toEqual({ source_type: ["备件采购", "批量采购"], url_version: "2" });
    expect(serializeAnalyticsState({ customerIds: [12, 7] })).toEqual({ customer: ["12", "7"], url_version: "2" });
  });

  it("preset=today / group_by=source_type / sort=order_date / dir=desc / page=1 都转 undefined", () => {
    expect(serializeAnalyticsState({
      preset: "today",
      groupBy: DEFAULT_GROUP_BY,
      sort: "order_date",
      dir: "desc",
      page: 1,
    })).toEqual({
      preset: undefined, from: undefined, to: undefined,
      group_by: undefined, sort: undefined, dir: undefined, page: undefined,
    });
  });

  it("非自定义预设把残留 from/to 一并清掉；custom 保留区间", () => {
    expect(serializeAnalyticsState({ preset: "all", from: "2026-01-01", to: "2026-02-01" }))
      .toEqual({ preset: "all", from: undefined, to: undefined });
    expect(serializeAnalyticsState({ preset: "custom", from: "2026-09-01", to: "2026-10-15" }))
      .toEqual({ preset: "custom", from: "2026-09-01", to: "2026-10-15" });
  });

  it("group_key / order_id 关闭层时删键", () => {
    expect(serializeAnalyticsState({ groupKey: null })).toEqual({ group_key: undefined });
    expect(serializeAnalyticsState({ groupKey: "备件采购" })).toEqual({ group_key: "备件采购" });
    expect(serializeAnalyticsState({ orderId: null })).toEqual({ order_id: undefined });
    expect(serializeAnalyticsState({ orderId: 88 })).toEqual({ order_id: "88" });
  });

  it("basis=三口径全选 → 删键（默认态）；子集才落键", () => {
    expect(serializeAnalyticsState({ basis: ["inc", "ex", "paid"] })).toEqual({ basis: undefined });
    expect(serializeAnalyticsState({ basis: ["paid"] })).toEqual({ basis: ["paid"], url_version: "2" });
  });
});

describe("URL 双向往返（parse ∘ serialize = 恒等）", () => {
  const cases: string[] = [
    // 完整形态：日期预设 + 全部多选筛选 + 分组 + 下钻 + 排序 + 翻页 + 口径子集 + 抽屉
    "preset=custom&from=2026-09-01&to=2026-10-15&source_type=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD,%E6%89%B9%E9%87%8F%E9%87%87%E8%B4%AD&purchaser=%E5%BC%A0%E4%B8%89&salesperson=%E6%9D%8E%E5%9B%9B&customer=12,7&category=%E6%9C%AA%E5%88%86%E7%B1%BB&group_by=category&group_key=%E6%9C%AA%E5%88%86%E7%B1%BB&sort=amount_paid&dir=asc&page=4&basis=inc,paid&order_id=88",
    "preset=all",
    "preset=30d&group_by=month",
    "preset=yesterday&group_by=customer&group_key=12&basis=paid",
  ];
  for (const qs of cases) {
    it(qs.slice(0, 48) + (qs.length > 48 ? "…" : ""), () => {
      const state = parseAnalyticsUrl(sp(qs));
      // 把完整状态按段序列化回 URL，再解析，应与首次解析一致
      const full = serializeAnalyticsState(state);
      const rebuilt = new URLSearchParams();
      for (const [k, v] of Object.entries(full)) {
        if (Array.isArray(v)) v.forEach(item => rebuilt.append(k, item));
        else if (v !== undefined) rebuilt.set(k, v);
      }
      expect(parseAnalyticsUrl(rebuilt)).toEqual(state);
      // 原始旧链接只改变多选编码与版本标记；其余键必须完整保留（排序后比较排除键顺序）。
      const expected = new URLSearchParams(qs);
      let hasList = false;
      for (const key of ["source_type", "purchaser", "salesperson", "customer", "category", "basis"]) {
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
    const state = parseAnalyticsUrl(sp(
      "preset=today&group_by=source_type&sort=order_date&dir=desc&page=1&basis=inc,ex,paid",
    ));
    const full = serializeAnalyticsState(state);
    expect(Object.values(full).every((v) => v === undefined)).toBe(true);
    expect(parseAnalyticsUrl(sp(""))).toEqual(state);
  });
});

describe("toAnalyticsApiFilters：状态 → 请求筛选", () => {
  it("默认（今天）→ date_from = date_to = 今天", () => {
    expect(toAnalyticsApiFilters(parseAnalyticsUrl(sp("")), TODAY)).toEqual({
      date_from: "2026-10-17",
      date_to: "2026-10-17",
    });
  });

  it("「全部」→ 不传任何日期键（后端约定缺省即全部历史）", () => {
    const f = toAnalyticsApiFilters(parseAnalyticsUrl(sp("preset=all")), TODAY);
    expect(f).toEqual({});
    expect("date_from" in f).toBe(false);
    expect("date_to" in f).toBe(false);
  });

  it("各预设解析为对应闭区间", () => {
    expect(toAnalyticsApiFilters(parseAnalyticsUrl(sp("preset=7d")), TODAY))
      .toEqual({ date_from: "2026-10-11", date_to: "2026-10-17" });
    expect(toAnalyticsApiFilters(parseAnalyticsUrl(sp("preset=yesterday")), TODAY))
      .toEqual({ date_from: "2026-10-16", date_to: "2026-10-16" });
    expect(toAnalyticsApiFilters(parseAnalyticsUrl(sp("preset=1y")), TODAY))
      .toEqual({ date_from: "2025-10-17", date_to: "2026-10-17" });
  });

  it("自定义非法回退今天；合法原样透传", () => {
    expect(toAnalyticsApiFilters(parseAnalyticsUrl(sp("preset=custom&from=2026-02-31&to=2026-03-05")), TODAY))
      .toEqual({ date_from: "2026-10-17", date_to: "2026-10-17" });
    expect(toAnalyticsApiFilters(parseAnalyticsUrl(sp("preset=custom&from=2026-09-01&to=2026-10-15")), TODAY))
      .toEqual({ date_from: "2026-09-01", date_to: "2026-10-15" });
  });

  it("多选筛选全量进请求；空筛选不产生空数组键", () => {
    const f = toAnalyticsApiFilters(parseAnalyticsUrl(sp(
      "source_type=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD,%E6%89%B9%E9%87%8F%E9%87%87%E8%B4%AD&customer=12,7&category=%E6%9C%AA%E5%88%86%E7%B1%BB",
    )), TODAY);
    expect(f).toEqual({
      date_from: "2026-10-17",
      date_to: "2026-10-17",
      source_type: ["备件采购", "批量采购"],
      customer_id: [12, 7],
      category: ["未分类"],
    });
  });
});

describe("toAnalyticsParams：URLSearchParams 序列化（FastAPI 重复键形态）", () => {
  it("多选值用重复键 k=v1&k=v2，绝不出 k[]=v（后端解析不出）", () => {
    const p = toAnalyticsParams(
      {
        date_from: "2026-10-17",
        date_to: "2026-10-17",
        source_type: ["备件采购", "批量采购"],
        purchaser: ["张三"],
        salesperson: ["李四"],
        customer_id: [12, 7],
        category: ["未分类"],
      },
      { group_by: "category", page: 2, page_size: 50 },
    );
    const qs = p.toString();
    expect(qs).toContain("source_type=%E5%A4%87%E4%BB%B6%E9%87%87%E8%B4%AD");
    expect(qs).toContain("source_type=%E6%89%B9%E9%87%8F%E9%87%87%E8%B4%AD");
    expect(qs).toContain("customer_id=12&customer_id=7");
    expect(qs).toContain("category=%E6%9C%AA%E5%88%86%E7%B1%BB");
    expect(qs).toContain("group_by=category&page=2&page_size=50");
    expect(qs).not.toContain("[]");
    // 每个 key 的出现次数 = 值个数（重复键而非逗号拼接）
    expect(p.getAll("source_type")).toEqual(["备件采购", "批量采购"]);
    expect(p.getAll("customer_id")).toEqual(["12", "7"]);
  });

  it("「全部」不传日期：序列化结果不含 date_from/date_to", () => {
    const p = toAnalyticsParams({});
    expect(p.toString()).toBe("");
    expect(p.has("date_from")).toBe(false);
    expect(p.has("date_to")).toBe(false);
  });

  it("空串/非整数 id 不产出参数（脏值不静默进请求）", () => {
    const p = toAnalyticsParams(
      { source_type: ["", "备件采购"], customer_id: [12, Number.NaN] },
      { sort: "" },
    );
    expect(p.getAll("source_type")).toEqual(["备件采购"]);
    expect(p.getAll("customer_id")).toEqual(["12"]);
    expect(p.has("sort")).toBe(false);
  });
});

describe("toAnalyticsApiFilters：默认「今天」按 Asia/Shanghai", () => {
  it("不传 today 时用业务日：UTC 16:30 = 上海次日", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-10-17T16:30:00Z"));
      const f = toAnalyticsApiFilters(parseAnalyticsUrl(new URLSearchParams("")));
      expect(f).toMatchObject({ date_from: "2026-10-18", date_to: "2026-10-18" });
    } finally {
      vi.useRealTimers();
    }
  });
});
