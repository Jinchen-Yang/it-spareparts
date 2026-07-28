import { describe, expect, it } from "vitest";
import { APP_VERSION, CHANGELOG, LATEST } from "../version";

describe("v1.19.0 pending release notes", () => {
  it("publishes the contract-level dual-tax margin scope", () => {
    expect(APP_VERSION).toBe("1.19.0");
    expect(LATEST).toBe(CHANGELOG[0]);
    expect(LATEST.version).toBe(APP_VERSION);
    expect(LATEST.date).toBe("2026-07-28");

    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/合同级备件毛利.*合同级贡献毛利/);
    expect(notes).toMatch(/含税、未税两套.*毛利.*毛利率/);
    expect(notes).toMatch(/管理员.*默认显示含税、未税或两者同时显示/);
    expect(notes).toMatch(/不会.*丢弃另一套数据/);
  });

  it("documents the missing-cost waterfall and audit evidence", () => {
    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(
      /池采购数量加权均价.*池销售均价.*本 PN 历史采购.*本 PN 历史销售/,
    );
    expect(notes).toMatch(/出库日当日或以前/);
    expect(notes).toMatch(/池版本.*样本窗口.*样本数.*追溯月份/);
  });

  it("states fail-closed and production approval gates", () => {
    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/证据不完整.*毛利保持空值.*不用 0/);
    expect(notes).toMatch(/项目追踪工作簿报销明细尚未建立费用全量数据水位.*贡献毛利.*空值/);
    expect(notes).toMatch(/代码完成不等于生产批准/);
    expect(notes).toMatch(/须经甲方确认.*发布门禁后才可上线/);
  });
});
