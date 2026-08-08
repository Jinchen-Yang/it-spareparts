import { describe, expect, it } from "vitest";

import {
  NAV_GROUPS,
  NAV_ITEMS,
  NAV_REDIRECTS,
  matchNavItem,
} from "../nav";

describe("维保管理信息架构", () => {
  it("固定展示项目数据、项目主档、下载中心、项目提醒四个并列入口", () => {
    const maintenance = NAV_GROUPS.find((group) => group.key === "grp-maintenance");

    expect(maintenance?.items.map(({ key, path, label }) => ({
      key,
      path,
      label,
    }))).toEqual([
      {
        key: "maintenance",
        path: "/maintenance",
        label: "项目数据",
      },
      {
        key: "maintenance-project-master",
        path: "/maintenance/project-master",
        label: "项目主档",
      },
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
  });

  it("/maintenance 直接渲染项目数据且不存在旧错误路由或重定向", () => {
    expect(matchNavItem("/maintenance")?.label).toBe("项目数据");
    expect(NAV_ITEMS.some((item) => item.path === "/maintenance/projects")).toBe(false);
    expect(NAV_ITEMS.some((item) => item.path === "/maintenance/alerts")).toBe(false);
    expect(NAV_REDIRECTS.some((item) => item.from === "/maintenance")).toBe(false);
  });
});
