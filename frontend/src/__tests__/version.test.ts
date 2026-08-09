import { describe, expect, it } from "vitest";
import { APP_VERSION, CHANGELOG, LATEST } from "../version";

describe("v1.21.0 release notes", () => {
  it("publishes the stable-by-default Beta boundary", () => {
    expect(APP_VERSION).toBe("1.21.0");
    expect(LATEST).toBe(CHANGELOG[0]);
    expect(LATEST.version).toBe(APP_VERSION);
    expect(LATEST.date).toBe("2026-08-10");

    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/稳定版继续作为默认入口/);
    expect(notes).toMatch(/实名白名单账号/);
    expect(notes).toMatch(/Beta 新增流程使用附加事实、影子状态/);
    expect(notes).toMatch(/正式成本和库存切换.*独立闸门/);
  });

  it("documents the integrated maintenance manager workflow", () => {
    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/项目方块工作台/);
    expect(notes).toMatch(/缺合同额、价格或期限.*仍完整展示/);
    expect(notes).toMatch(/已回款\/合同额.*已消耗\/合同额/);
    expect(notes).toMatch(/80%.*黄色.*100%.*红色/);
    expect(notes).toMatch(/服务端 7 秒/);
    expect(notes).toMatch(/现场备件领用单.*不直接写库存/);
  });

  it("documents the replenishment cart and its external-review boundary", () => {
    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/补库购物车 Beta/);
    expect(notes).toMatch(/所属池和近半年采购\/销售价量/);
    expect(notes).toMatch(/逐条审核反馈、打回复提、版本留存/);
    expect(notes).toMatch(/不自动审批.*不修改库存/);
  });
});
