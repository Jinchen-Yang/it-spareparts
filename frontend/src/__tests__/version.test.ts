import { describe, expect, it } from "vitest";
import { APP_VERSION, CHANGELOG, LATEST } from "../version";

describe("v1.26.0 release notes", () => {
  it("publishes the attachment and maintenance salesperson updates", () => {
    expect(APP_VERSION).toBe("1.26.0");
    expect(LATEST).toBe(CHANGELOG[0]);
    expect(LATEST.version).toBe(APP_VERSION);
    expect(LATEST.date).toBe("2026-08-31");

    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/任意常见文件均可上传/);
    expect(notes).toMatch(/显示上传人姓名.*显示账号名/);
    expect(notes).toMatch(/销售人员.*手工.*清空/);
    expect(notes).toMatch(/负责人.*改派.*同步销售人员/);
  });

  it("keeps prior releases archived in the changelog", () => {
    const versions = CHANGELOG.map((entry) => entry.version);
    expect(versions).toContain("1.25.0");
    expect(versions).toContain("1.24.0");
    expect(versions).toContain("1.23.0");
    expect(versions).toContain("1.21.0");
    // 数组按新→旧排列
    expect(versions[0]).toBe("1.26.0");
  });
});

describe("v1.25.0 release notes (archived)", () => {
  const v125 = CHANGELOG.find((entry) => entry.version === "1.25.0")!;

  it("keeps the maintenance contract, cost and refresh fixes (2026-08-27)", () => {
    expect(v125.date).toBe("2026-08-27");
    const notes = v125.items.join("\n");
    expect(notes).toMatch(/合同总额.*明确的含税金额/);
    expect(notes).toMatch(/不再按固定税率猜值/);
    expect(notes).toMatch(/Excel 中修改合同总额/);
    expect(notes).toMatch(/真实 0、部分缺失与全部缺失/);
    expect(notes).toMatch(/同文件重试.*即时刷新/);
  });
});

describe("v1.24.0 release notes (archived)", () => {
  const v124 = CHANGELOG.find((entry) => entry.version === "1.24.0")!;

  it("keeps the customer-feedback display fixes (2026-08-21)", () => {
    expect(v124.date).toBe("2026-08-21");
    const notes = v124.items.join("\n");
    expect(notes).toMatch(/项目卡改显「销售」/);
    expect(notes).toMatch(/不再显示项目经理/);
    expect(notes).toMatch(/维保备件发货数/);
    expect(notes).toMatch(/3,446 个/);
    expect(notes).toMatch(/日期倒序/);
  });
});

describe("v1.23.0 release notes (archived)", () => {
  const v123 = CHANGELOG.find((entry) => entry.version === "1.23.0")!;

  it("publishes the two-page maintenance redesign", () => {
    expect(v123.date).toBe("2026-08-17");

    const notes = v123.items.join("\n");
    expect(notes).toMatch(/22 个页面收敛为 2 个/);
    expect(notes).toMatch(/维保主页（项目卡墙）/);
    expect(notes).toMatch(/项目面板/);
    expect(notes).toMatch(/旧页面地址自动跳转/);
    expect(notes).not.toMatch(/Beta|试用/);
  });

  it("documents the card wall with the three-color cost ratio bar", () => {
    const notes = v123.items.join("\n");
    expect(notes).toMatch(/一行五卡/);
    expect(notes).toMatch(/成本÷合同额/);
    expect(notes).toMatch(/80% 绿色.*80–100% 黄色.*100% 红色/);
    expect(notes).toMatch(/默认显示进行中项目/);
    expect(notes).toMatch(/期限缺失可切换筛出/);
  });

  it("documents period and contract amount derived from existing data (#51)", () => {
    const notes = v123.items.join("\n");
    expect(notes).toMatch(/无需先导入台账/);
    expect(notes).toMatch(/维保起止日期/);
    expect(notes).toMatch(/销售订单自动汇总/);
    // 诚实标注（铁律 5）：共用单 / 不完整必须写进公告，不许静默
    expect(notes).toMatch(/共用单/);
    expect(notes).toMatch(/不完整/);
    expect(notes).toMatch(/台账导入将以台账为准/);
  });

  it("documents the panel tabs, assignment and period editing (#39)", () => {
    const notes = v123.items.join("\n");
    expect(notes).toMatch(/项目基础信息 \/ 备件成本 \/ 报销 \/ 回款/);
    expect(notes).toMatch(/归属挂靠.*XSDD 销售订单预筛/);
    expect(notes).toMatch(/维保负责人与维保期限/);
    expect(notes).toMatch(/状态自动重算/);
  });

  it("documents the three download/upload spots and honest empty states", () => {
    const notes = v123.items.join("\n");
    expect(notes).toMatch(/在哪下载就在哪上传/);
    expect(notes).toMatch(/全项目备件行级表/);
    expect(notes).toMatch(/六 sheet/);
    expect(notes).toMatch(/尚未导入/);
    expect(notes).toMatch(/绝不显示 0/);
  });
});
