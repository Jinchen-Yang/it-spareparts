import { describe, expect, it } from "vitest";

import {
  DETAIL_ROUTES,
  NAV_GROUPS,
  NAV_ITEMS,
  NAV_REDIRECTS,
  matchNavItem,
} from "../nav";

describe("维保管理信息架构", () => {
  it("固定展示项目面板、项目主档、月度更新和成本回填四个入口", () => {
    const maintenance = NAV_GROUPS.find((group) => group.key === "grp-maintenance");

    expect(maintenance?.items.map(({ key, path, label }) => ({
      key,
      path,
      label,
    }))).toEqual([
      {
        key: "maintenance-projects",
        path: "/maintenance/projects",
        label: "项目面板",
      },
      {
        key: "maintenance-project-master",
        path: "/maintenance/project-master",
        label: "项目主档",
      },
      {
        key: "maintenance-updates",
        path: "/maintenance/updates",
        label: "月度更新",
      },
      {
        key: "maintenance-cost-refill",
        path: "/maintenance/cost-refill",
        label: "成本回填",
      },
    ]);
  });

  it("旧维保路径继续可访问并进入新的稳定项目流程", () => {
    expect(matchNavItem("/maintenance/projects")?.label).toBe("项目面板");
    expect(NAV_REDIRECTS).toContainEqual({
      from: "/maintenance",
      to: "/maintenance/projects",
      perm: "page_maintenance",
    });
    expect(DETAIL_ROUTES.some((route) => route.path === "/maintenance/downloads")).toBe(true);
    expect(DETAIL_ROUTES.some((route) => route.path === "/maintenance/reminders")).toBe(true);
    expect(DETAIL_ROUTES.some((route) => route.path === "/maintenance/legacy")).toBe(true);
    expect(NAV_ITEMS.some((item) => item.path === "/maintenance/downloads")).toBe(false);
  });
});
