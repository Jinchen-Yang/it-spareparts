import { describe, expect, it } from "vitest";
import { APP_VERSION, CHANGELOG, LATEST } from "../version";

describe("v1.19.0 release notes", () => {
  it("publishes the confirmed contract-level dual-tax margin policy", () => {
    expect(APP_VERSION).toBe("1.19.0");
    expect(LATEST).toBe(CHANGELOG[0]);
    expect(LATEST.version).toBe(APP_VERSION);
    expect(LATEST.date).toBe("2026-07-29");

    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/合同级备件毛利.*合同级贡献毛利/);
    expect(notes).toMatch(/扣除维保报销/);
    expect(notes).toMatch(/含税和未税两套/);
    expect(notes).toMatch(/采购、销售和项目维保.*管理员统一设置/);
    expect(notes).toMatch(/销售默认显示未税/);
  });

  it("documents fixed 13% tax handling and the three-month cost waterfall", () => {
    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/双税计算统一使用 13%/);
    expect(notes).toMatch(/没有明确税务口径.*按未税读入/);
    expect(notes).toMatch(/三个月内有效互通池采购.*池销售.*本 PN 采购.*本 PN 销售.*人工回填/);
    expect(notes).toMatch(/全部 active 池均可参与/);
  });

  it("states fail-closed margin and roundtrip workbook contracts", () => {
    const notes = LATEST.items.join("\n");
    expect(notes).toMatch(/证据不完整.*毛利保持空值.*不用 0/);
    expect(notes).toMatch(/固定的维保往返工作簿.*Excel Table/);
    expect(notes).toMatch(/整本先预检后单事务写入.*重复上传不重复写/);
    expect(notes).toMatch(/日期范围同时限制每本内的订单、明细和报销.*全部.*完整合同/);
    expect(notes).toMatch(/不再改变在线项目页/);
    expect(notes).toMatch(/完整往返工作簿成功导入后才发布贡献毛利/);
  });
});
