import { describe, expect, it } from "vitest";
import { APP_VERSION, CHANGELOG, LATEST } from "../version";

describe("v1.20.0 release notes", () => {
  it("publishes the three project entry information architecture", () => {
    expect(APP_VERSION).toBe("1.20.0");
    expect(LATEST).toBe(CHANGELOG[0]);
    expect(LATEST.version).toBe(APP_VERSION);
    expect(LATEST.date).toBe("2026-07-30");

    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/项目数据默认直接展示原有详细盈亏/);
    expect(notes).toMatch(/下载中心.*项目提醒后置/);
    expect(notes).toMatch(/旧能力继续保留/);
  });

  it("documents fail-closed download semantics and safe batch roundtrip bounds", () => {
    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/不存在的项目或合同明确返回 404/);
    expect(notes).toMatch(/全局空范围明确返回 422/);
    expect(notes).toMatch(/每个合同保持既有固定 Sheet、Table、行签名和版本协议/);
    expect(notes).toMatch(/少于 500.*少于 512 MiB/);
    expect(notes).toMatch(/超过任一上限整批拒绝.*不返回截断文件/);
    expect(notes).toMatch(/合同详细盈亏 CSV.*单合同 XLSX.*逐分/);
    expect(notes).toMatch(/报销含税、未税和证据状态/);
    expect(notes).toMatch(/回填模板的导出与导入.*客户信息权限/);
  });
});
