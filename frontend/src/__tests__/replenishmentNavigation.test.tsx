import { describe, expect, it } from "vitest";

import { NAV_GROUPS } from "../nav";

describe("补库申请 Beta 导航", () => {
  it("只在销售管理下用独立页面权限暴露 Beta 入口", () => {
    const sales = NAV_GROUPS.find((group) => group.key === "grp-sales");
    const item = sales?.items.find((candidate) => candidate.key === "replenishment-beta");

    expect(item).toMatchObject({
      path: "/sales/replenishment-beta",
      label: "补库申请 Beta",
      perm: "page_replenishment_beta",
    });
  });
});
