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

  it("旧版入口与工作台分组命名区分，不再出现两个同名维保管理", () => {
    const stable = NAV_GROUPS.find((group) => group.key === "grp-maintenance");
    const workbench = NAV_GROUPS.find((group) => group.key === "grp-maintenance-beta");
    const admin = NAV_GROUPS.find((group) => group.key === "grp-maintenance-admin");

    // 所有带 label 的组名互不相同
    const labels = NAV_GROUPS
      .map((group) => group.label)
      .filter((label): label is string => label !== null);
    expect(new Set(labels).size).toBe(labels.length);

    expect(stable?.label).toBe("维保项目（旧版）");
    expect(stable?.items.map(({ key, path, label }) => ({
      key,
      path,
      label,
    }))).toEqual([
      { key: "maintenance", path: "/maintenance", label: "项目数据" },
      {
        key: "maintenance-downloads",
        path: "/maintenance/downloads",
        label: "下载中心",
      },
      {
        key: "maintenance-reminders",
        path: "/maintenance/reminders",
        label: "项目提醒",
      },
    ]);

    expect(workbench?.label).toBe("维保工作台");
    expect(workbench?.items.map(({ key, path, label, perm }) => ({
      key,
      path,
      label,
      perm,
    }))).toEqual([
      {
        key: "maintenance-workbench",
        path: "/maintenance/beta/workbench",
        label: "我的维保",
        perm: "page_maintenance_beta",
      },
      {
        key: "maintenance-sales-dashboard",
        path: "/maintenance/beta/sales-dashboard",
        label: "销售看板",
        perm: undefined,
      },
      {
        key: "maintenance-projects",
        path: "/maintenance/beta/projects",
        label: "项目总览",
        perm: "page_maintenance_beta",
      },
      {
        key: "maintenance-updates",
        path: "/maintenance/beta/updates",
        label: "月度项目更新",
        perm: undefined,
      },
      {
        key: "maintenance-manager-workbook",
        path: "/maintenance/beta/project-manager/monthly-workbook",
        label: "经理月报",
        perm: undefined,
      },
      {
        key: "maintenance-acceptance",
        path: "/maintenance/beta/acceptance",
        label: "验收与结项",
        perm: "page_maintenance_beta",
      },
      {
        key: "maintenance-collection-reminders",
        path: "/maintenance/beta/collection-reminders",
        label: "回款提醒",
        perm: "page_maintenance_beta",
      },
    ]);
    expect(
      workbench?.items.find((item) => item.key === "maintenance-collection-reminders")
        ?.betaFeature,
    ).toBe("maintenance");

    expect(admin?.label).toBe("维保数据维护");
    expect(admin?.items.map(({ key, path, label, perm }) => ({
      key,
      path,
      label,
      perm,
    }))).toEqual([
      {
        key: "maintenance-project-master",
        path: "/maintenance/beta/project-master",
        label: "项目主档维护",
        perm: "page_maintenance_beta",
      },
      {
        key: "maintenance-demands",
        path: "/maintenance/beta/demands",
        label: "异常维保单处理",
        perm: "page_maintenance_beta",
      },
      {
        key: "maintenance-warehouse",
        path: "/maintenance/beta/warehouse",
        label: "仓库单据核对",
        perm: "page_maintenance_beta",
      },
      {
        key: "maintenance-cost-refill",
        path: "/maintenance/beta/cost-refill",
        label: "领用缺价补录",
        perm: undefined,
      },
      {
        key: "maintenance-migration",
        path: "/maintenance/beta/migration",
        label: "历史数据迁移核对",
        perm: undefined,
      },
    ]);
  });

  it("经理月报按页面和合同额权限显示，高风险单项目写入仍需独立动作权限", () => {
    const workbench = NAV_GROUPS.find((group) => group.key === "grp-maintenance-beta");
    const admin = NAV_GROUPS.find((group) => group.key === "grp-maintenance-admin");
    const managerWorkbook = workbench?.items.find(
      (item) => item.key === "maintenance-manager-workbook",
    );
    const updates = workbench?.items.find((item) => item.key === "maintenance-updates");
    const refill = admin?.items.find((item) => item.key === "maintenance-cost-refill");
    const migration = admin?.items.find((item) => item.key === "maintenance-migration");
    localStorage.setItem("role", "readonly");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      page_maintenance_beta: true,
    }));
    expect(updates?.visibleWhen?.()).toBe(false);
    expect(refill?.visibleWhen?.()).toBe(false);
    expect(managerWorkbook?.visibleWhen?.()).toBe(false);
    expect(migration?.visibleWhen?.()).toBe(false);

    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: false,
      page_maintenance_beta: true,
      data_purchase_cost: true,
      action_maintenance_project_manage: true,
      action_maintenance_migration_review: true,
    }));
    expect(refill?.visibleWhen?.()).toBe(false);
    expect(managerWorkbook?.visibleWhen?.()).toBe(false);
    expect(migration?.visibleWhen?.()).toBe(false);

    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      page_maintenance_beta: true,
      data_customer: true,
      data_purchase_cost: true,
      data_profit: true,
      action_maintenance_roundtrip_apply: true,
      action_maintenance_project_manage: true,
      action_maintenance_migration_review: true,
    }));
    expect(updates?.visibleWhen?.()).toBe(true);
    expect(refill?.visibleWhen?.()).toBe(true);
    expect(managerWorkbook?.visibleWhen?.()).toBe(true);
    expect(migration?.visibleWhen?.()).toBe(true);
  });

  it("共享管理员被显式关闭迁移权限时不显示迁移核对入口", () => {
    const admin = NAV_GROUPS.find((group) => group.key === "grp-maintenance-admin");
    const migration = admin?.items.find((item) => item.key === "maintenance-migration");
    localStorage.setItem("role", "admin");
    localStorage.setItem("permissions", JSON.stringify({
      page_maintenance: true,
      data_purchase_cost: true,
      data_profit: true,
      action_maintenance_migration_review: false,
    }));

    expect(migration?.visibleWhen?.()).toBe(false);
  });

  it("我的维保是维保工作台第一入口，销售看板仅 admin/boss 可见", () => {
    const workbench = NAV_GROUPS.find((group) => group.key === "grp-maintenance-beta");
    expect(workbench?.items[0]?.key).toBe("maintenance-workbench");
    expect(workbench?.items[0]?.label).toBe("我的维保");
    expect(matchNavItem("/maintenance/beta/workbench")?.label).toBe("我的维保");
    expect(matchNavItem("/maintenance/beta/workbench")?.perm).toBe("page_maintenance_beta");

    const dashboard = workbench?.items.find((item) => item.key === "maintenance-sales-dashboard");
    expect(dashboard).toBeDefined();
    expect(dashboard?.visibleWhen).toBeDefined();
    localStorage.setItem("role", "sales");
    expect(dashboard?.visibleWhen?.()).toBe(false);
    localStorage.setItem("role", "boss");
    expect(dashboard?.visibleWhen?.()).toBe(true);
    localStorage.setItem("role", "admin");
    expect(dashboard?.visibleWhen?.()).toBe(true);
  });

  it("旧维保路径仍是稳定版，Beta 路由与权限独立", () => {
    expect(matchNavItem("/maintenance")?.label).toBe("项目数据");
    expect(matchNavItem("/maintenance/downloads")?.label).toBe("下载中心");
    expect(matchNavItem("/maintenance/reminders")?.label).toBe("项目提醒");
    expect(matchNavItem("/maintenance/beta/projects")?.label).toBe("项目总览");
    expect(matchNavItem("/maintenance/beta/projects")?.perm).toBe("page_maintenance_beta");
    expect(matchNavItem("/maintenance/beta/collection-reminders")?.label).toBe("回款提醒");
    expect(matchNavItem("/maintenance/beta/collection-reminders")?.perm)
      .toBe("page_maintenance_beta");
    expect(NAV_REDIRECTS).not.toContainEqual({
      from: "/maintenance",
      to: "/maintenance/projects",
      perm: "page_maintenance",
    });
    expect(DETAIL_ROUTES.some((route) => route.path === "/maintenance/downloads")).toBe(false);
    expect(DETAIL_ROUTES.some((route) => route.path === "/maintenance/reminders")).toBe(false);
    expect(NAV_REDIRECTS).toContainEqual({
      from: "/maintenance/legacy",
      to: "/maintenance",
      perm: "page_maintenance",
    });
    expect(NAV_ITEMS.some((item) => item.path === "/maintenance/downloads")).toBe(true);
    expect(
      DETAIL_ROUTES.some((route) => route.path === "/maintenance/projects/:projectId"),
    ).toBe(true);
    expect(
      DETAIL_ROUTES.some((route) => route.path === "/maintenance/project-master/source-orders"),
    ).toBe(true);
    expect(
      DETAIL_ROUTES
        .filter((route) => route.path.startsWith("/maintenance/beta/"))
        .every((route) => route.perm === "page_maintenance_beta"),
    ).toBe(true);
    expect(
      DETAIL_ROUTES
        .filter((route) => route.perm === "page_maintenance_beta")
        .every((route) => route.path.startsWith("/maintenance/beta/")),
    ).toBe(true);
  });
});
