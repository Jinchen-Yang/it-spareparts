import { beforeEach, describe, expect, it } from "vitest";

import {
  DETAIL_ROUTES,
  NAV_GROUPS,
  NAV_ITEMS,
  NAV_REDIRECTS,
  matchNavItem,
} from "../nav";

describe("维保管理信息架构", () => {
  beforeEach(() => localStorage.clear());

  it("固定定义项目面板、项目主档、经理月报、单项目更新和成本回填入口", () => {
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
        key: "maintenance-manager-workbook",
        path: "/maintenance/project-manager/monthly-workbook",
        label: "经理月报",
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

  it("经理月报按页面和合同额权限显示，高风险单项目写入仍需独立动作权限", () => {
    const maintenance = NAV_GROUPS.find((group) => group.key === "grp-maintenance");
    const managerWorkbook = maintenance?.items.find(
      (item) => item.key === "maintenance-manager-workbook",
    );
    const updates = maintenance?.items.find((item) => item.key === "maintenance-updates");
    const refill = maintenance?.items.find((item) => item.key === "maintenance-cost-refill");
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({ page_maintenance: true }));
    expect(updates?.visibleWhen?.()).toBe(false);
    expect(refill?.visibleWhen?.()).toBe(false);
    expect(managerWorkbook?.visibleWhen?.()).toBe(false);

    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: false,
      data_purchase_cost: true,
      action_maintenance_project_manage: true,
    }));
    expect(refill?.visibleWhen?.()).toBe(false);
    expect(managerWorkbook?.visibleWhen?.()).toBe(false);

    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_customer: true,
      data_purchase_cost: true,
      data_profit: true,
      action_maintenance_roundtrip_apply: true,
      action_maintenance_project_manage: true,
    }));
    expect(updates?.visibleWhen?.()).toBe(true);
    expect(refill?.visibleWhen?.()).toBe(true);
    expect(managerWorkbook?.visibleWhen?.()).toBe(true);
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
