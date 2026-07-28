import { describe, expect, it } from "vitest";
import { APP_VERSION, CHANGELOG, LATEST } from "../version";

describe("v1.18.0 release notes", () => {
  it("publishes the maintenance cost quality decision-gate summary", () => {
    expect(APP_VERSION).toBe("1.18.0");
    expect(LATEST).toBe(CHANGELOG[0]);
    expect(LATEST.version).toBe(APP_VERSION);
    expect(LATEST.date).toBe("2026-07-28");

    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/实际采购参考.*估算参考.*成本缺失/);
    expect(notes).toMatch(/含税\/不含税.*不跨税口径相加/);
    expect(notes).toMatch(/任一.*缺失.*停止计算预算余额.*红黄绿/);
    expect(notes).toMatch(/不定义正式项目毛利/);
  });

  it("states that every maintenance consumer uses the same cost truth", () => {
    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/逐行 API/);
    expect(notes).toMatch(/项目与明细 CSV/);
    expect(notes).toMatch(/订单汇总 Excel/);
    expect(notes).toMatch(/单本及批量四 Sheet 项目工作簿/);
    expect(notes).toMatch(/Agent.*前端页面.*同一成本事实层级/);
  });

  it("documents both cost-blind and profit-blind permission boundaries", () => {
    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/无成本权限.*金额.*层级.*来源统计.*异常标记.*脱敏/);
    expect(notes).toMatch(
      /无利润权限.*合同额.*预算.*余量.*决策状态.*状态计数.*筛选.*排序.*隐藏/,
    );
  });
});
