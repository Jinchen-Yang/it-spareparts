import { describe, expect, it } from "vitest";
import { APP_VERSION, CHANGELOG, LATEST } from "../version";

describe("v1.17.0 release notes", () => {
  it("publishes the complete import-safety and maintenance-export release summary", () => {
    expect(APP_VERSION).toBe("1.17.0");
    expect(LATEST).toBe(CHANGELOG[0]);
    expect(LATEST.version).toBe(APP_VERSION);
    expect(LATEST.date).toBe("2026-07-27");

    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/导入预检/);
    expect(notes).toMatch(/完整.*问题明细/);
    expect(notes).toMatch(/重复文件.*归档安全/);
    expect(notes).toMatch(/维保订单.*时间.*XLSX/);
    expect(notes).toMatch(/批量.*项目工作簿.*ZIP/);
  });

  it("states the hard per-batch file limit without silent truncation or splitting", () => {
    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/单批最多 20 个文件/);
    expect(notes).toMatch(/21 个及以上.*整批拒绝/);
    expect(notes).toMatch(/不截断.*不拆批/);
  });

  it("distinguishes exact duplicate handling between skip and upsert modes", () => {
    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(
      /skip 模式.*成功批次.*SHA-256.*完全相同.*建作业前拦截/,
    );
    expect(notes).toMatch(/upsert 模式.*只提示.*允许重处理/);
  });

  it("explains that ZIP dates select contracts without truncating their workbooks", () => {
    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/ZIP.*时间范围只用于选中合同/);
    expect(notes).toMatch(/每本.*完整四个 Sheet 工作簿/);
  });
});
