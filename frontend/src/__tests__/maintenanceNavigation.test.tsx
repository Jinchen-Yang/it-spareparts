import { beforeEach, describe, expect, it } from "vitest";

import {
  DETAIL_ROUTES,
  NAV_GROUPS,
  NAV_ITEMS,
  NAV_REDIRECTS,
  matchDetailRoute,
  matchNavItem,
} from "../nav";

/**
 * 维保信息架构：2026-08-16 定稿两页（REQUIREMENTS #33/#44），
 * 2026-08-19 #267 增加第三页「需求单与同步」（WBDD 快照差异 + 需求单作废/恢复）。
 * ①维保主页（项目卡墙，导航项）②需求单与同步（导航项）
 * ③项目面板（从卡片进入的详情路由，不占导航项）。
 * 旧的三代页面（旧版 3 / 工作台 9 / 展示板 5 / 数据维护 5）已随重设计删除。
 */
describe("维保信息架构（2 页定稿 + #267 需求单页）", () => {
  beforeEach(() => localStorage.clear());

  it("维保只有一个导航组；组内为维保主页 + 数据分析 + 需求单与同步 + 补库申请", () => {
    const groups = NAV_GROUPS.filter((group) => group.key.startsWith("grp-maintenance"));
    expect(groups).toHaveLength(1);
    expect(groups[0].label).toBe("维保项目");
    expect(groups[0].items.map((item) => ({ key: item.key, path: item.path }))).toEqual([
      { key: "maintenance-home", path: "/maintenance" },
      { key: "maintenance-analytics", path: "/maintenance/analytics" },
      { key: "maintenance-demands", path: "/maintenance/demands" },
      { key: "replenishment-beta", path: "/maintenance/replenishment" },
    ]);
  });

  it("组名互不重复（不再出现两个同名维保分组）", () => {
    const labels = NAV_GROUPS.map((group) => group.label).filter(
      (label): label is string => label !== null,
    );
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("主页 anyPerm＝老板或项目经理，正式功能不挂 Beta 总闸", () => {
    const home = matchNavItem("/maintenance");
    expect(home?.anyPerm).toEqual(["page_maintenance_boss", "page_maintenance"]);
    // 看板已去 Beta 化（2026-08-17）：只按权限展示，不再受 betaFeature 隐藏
    expect(home?.betaFeature).toBeUndefined();
    // 两条查看权限任一即可进（M0-B 改判①后两者都是全项目范围）
    expect(home?.perm).toBeUndefined();
  });

  it("项目面板是详情路由，与主页同门", () => {
    const panel = DETAIL_ROUTES.find((route) => route.key === "maintenance-project-panel");
    expect(panel?.path).toBe("/maintenance/projects/:projectId");
    expect(panel?.anyPerm).toEqual(["page_maintenance_boss", "page_maintenance"]);
    expect(panel?.betaFeature).toBeUndefined();
    expect(panel?.menuKey).toBe("maintenance-home");
    expect(matchDetailRoute("/maintenance/projects/abc-123")?.key)
      .toBe("maintenance-project-panel");
  });

  it("已删除的旧维保页面不再有任何导航项或详情路由", () => {
    const deadPaths = [
      "/maintenance/downloads",
      "/maintenance/reminders",
      "/maintenance/boss",
      "/maintenance/boss/projects",
      "/maintenance/boss/uploads",
      "/maintenance/boss/master",
      "/maintenance/beta/workbench",
      "/maintenance/beta/sales-dashboard",
      "/maintenance/beta/projects",
      "/maintenance/beta/updates",
      "/maintenance/beta/acceptance",
      "/maintenance/beta/collection-reminders",
      "/maintenance/beta/project-master",
      "/maintenance/beta/demands",
      "/maintenance/beta/warehouse",
      "/maintenance/beta/cost-refill",
      "/maintenance/beta/migration",
    ];
    for (const path of deadPaths) {
      expect(matchNavItem(path), `${path} 仍有导航项`).toBeUndefined();
      expect(matchDetailRoute(path), `${path} 仍有详情路由`).toBeUndefined();
    }
  });

  it("旧路径全部重定向回维保主页，不留空白页", () => {
    const targets = new Map(NAV_REDIRECTS.map((r) => [r.from, r.to]));
    for (const from of [
      "/maintenance/boss",
      "/maintenance/beta/workbench",
      "/maintenance/beta/project-master/source-orders",
      "/maintenance/cost-refill",
      "/maintenance/migration",
    ]) {
      expect(targets.get(from), `${from} 未配置重定向`).toBe("/maintenance");
    }
  });

  it("重定向不与现存路由冲突（不能把活路由重定向掉）", () => {
    for (const redirect of NAV_REDIRECTS) {
      expect(matchNavItem(redirect.from), `${redirect.from} 既是路由又是重定向`)
        .toBeUndefined();
      expect(matchDetailRoute(redirect.from), `${redirect.from} 既是详情路由又是重定向`)
        .toBeUndefined();
    }
  });

  it("维保导航项路径唯一（重设计后不留重复入口）", () => {
    const paths = NAV_ITEMS.filter((item) => item.path.startsWith("/maintenance"))
      .map((item) => item.path);
    expect(paths).toEqual([
      "/maintenance",
      "/maintenance/analytics",
      "/maintenance/demands",
      "/maintenance/replenishment",
    ]);
  });
});
