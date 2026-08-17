import { describe, expect, it } from "vitest";

import { NAV_GROUPS, NAV_REDIRECTS } from "../nav";

describe("补库申请正式版导航", () => {
  it("归入维保项目组，用独立页面权限暴露正式入口", () => {
    const maintenance = NAV_GROUPS.find((group) => group.key === "grp-maintenance");
    const item = maintenance?.items.find((candidate) => candidate.key === "replenishment-beta");

    // 2026-08-17 业务指示：补库申请是维保业务动作，从销售组迁入维保组
    expect(item).toMatchObject({
      path: "/maintenance/replenishment",
      label: "补库申请",
      perm: "page_replenishment_beta",
      betaFeature: "replenishment",
    });

    // 销售组不再有补库申请入口
    const sales = NAV_GROUPS.find((group) => group.key === "grp-sales");
    expect(sales?.items.find((candidate) => candidate.key === "replenishment-beta"))
      .toBeUndefined();
  });

  it("旧销售组路径保留重定向，收藏/外链不失效", () => {
    const redirect = NAV_REDIRECTS.find((r) => r.from === "/sales/replenishment-beta");
    expect(redirect?.to).toBe("/maintenance/replenishment");
    expect(redirect?.perm).toBe("page_replenishment_beta");
  });
});
