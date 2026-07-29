/** 图表底座公共格式化：空值语义（null→占位符，绝不 0）、金额/数量/百分比/HTML 转义。 */
import { describe, expect, it } from "vitest";
import {
  completeTaxPair, EMPTY, escapeHtml, money, moneyAxis, moneyExact, pctSigned, qty,
  splitByFlag, splitFixed,
} from "../../../utils/format";

describe("空值语义", () => {
  it("null/undefined 一律占位符，绝不折算成 0", () => {
    expect(qty(null)).toBe(EMPTY);
    expect(moneyExact(undefined)).toBe(EMPTY);
    expect(moneyAxis(null)).toBe(EMPTY);
    expect(pctSigned(null)).toBe(EMPTY);
    [qty(null), moneyExact(null), moneyAxis(null)].forEach((s) => {
      expect(s).not.toContain("0");
    });
  });

  it("真 0 是合法数值，正常显示", () => {
    expect(moneyExact(0)).toBe("¥0");
    expect(moneyAxis(0)).toBe("¥0");
    expect(qty(0)).toBe("0");
  });
});

describe("moneyExact / qty", () => {
  it("千分位 + 小数位上限；负数沿用全站 ¥-1,234 形态", () => {
    expect(moneyExact(1234567.891)).toBe("¥1,234,567.89");
    expect(moneyExact(-6161)).toBe("¥-6,161");
    expect(qty(1234.5678)).toBe("1,234.568");
  });
});

describe("moneyAxis（轴刻度压缩）", () => {
  it("万/亿压缩、去尾零、负号在前", () => {
    expect(moneyAxis(12000)).toBe("¥1.2万");
    expect(moneyAxis(350000000)).toBe("¥3.5亿");
    expect(moneyAxis(-45000)).toBe("-¥4.5万");
    expect(moneyAxis(10000)).toBe("¥1万");
    expect(moneyAxis(999)).toBe("¥999");
  });
});

describe("pctSigned", () => {
  it("正值显式 +，负值 -，方向不依赖颜色", () => {
    expect(pctSigned(0.123)).toBe("+12.3%");
    expect(pctSigned(-0.045)).toBe("-4.5%");
    expect(pctSigned(0)).toBe("0.0%");
  });
});

describe("escapeHtml", () => {
  it("五个危险字符全转义；null 转空串", () => {
    expect(escapeHtml(`<img src="x" onerror='a&b'>`))
      .toBe("&lt;img src=&quot;x&quot; onerror=&#39;a&amp;b&#39;&gt;");
    expect(escapeHtml(null)).toBe("");
  });
});

describe("既有 money 不回归", () => {
  it("money 维持原行为（其它页面在用）", () => {
    expect(money(null)).toBe(EMPTY);
    expect(money(1000)).toContain("1,000");
  });
});

describe("固定 13% 双税口径", () => {
  it("从含税或未税原值补齐另一侧并按分保留两位", () => {
    expect(splitFixed(113, "inc")).toEqual({ inc: 113, ex: 100 });
    expect(splitFixed(100, "ex")).toEqual({ inc: 113, ex: 100 });
    expect(splitFixed(null, "ex")).toEqual({ inc: null, ex: null });
  });

  it("金额中点与后端/PostgreSQL 一样按分远离零舍入，且先舍入原始侧", () => {
    expect(splitFixed(0.505, "ex")).toEqual({ inc: 0.58, ex: 0.51 });
    expect(splitFixed(2.675, "ex")).toEqual({ inc: 3.03, ex: 2.68 });
    expect(splitFixed(-0.505, "ex")).toEqual({ inc: -0.58, ex: -0.51 });
    expect(splitFixed(1.005, "inc")).toEqual({ inc: 1.01, ex: 0.89 });
  });

  it("采购未标注税口径时按未税原值处理", () => {
    expect(splitByFlag(100, null)).toEqual({ inc: 113, ex: 100 });
    expect(splitByFlag(113, true)).toEqual({ inc: 113, ex: 100 });
  });

  it("API 双值优先，仅补缺失侧", () => {
    expect(completeTaxPair(120, 100)).toEqual({ inc: 120, ex: 100 });
    expect(completeTaxPair(null, 100)).toEqual({ inc: 113, ex: 100 });
  });
});
