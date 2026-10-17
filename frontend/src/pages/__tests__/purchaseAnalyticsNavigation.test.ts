// 采购分析导航注册（D-39 PR-4a）：新页挂采购管理组、仅老板/管理员
// （perm=page_purchase_analytics，模板只给 boss，admin 由 App 层恒放行）；
// 原逐型号频次页更名「型号分析」，路径与权限不变（老链接不失效）。
import { beforeEach, describe, expect, it } from "vitest";

import { NAV_ITEMS, matchNavItem } from "../../nav";

describe("采购分析导航（D-39）", () => {
  beforeEach(() => localStorage.clear());

  it("新页 /purchases/analytics：key/标签/权限正确登记", () => {
    expect(matchNavItem("/purchases/analytics")).toMatchObject({
      key: "purchases-analytics",
      label: "采购分析",
      path: "/purchases/analytics",
      perm: "page_purchase_analytics",
    });
  });

  it("原逐型号页更名「型号分析」，路径与权限不变", () => {
    expect(matchNavItem("/purchases/analysis")).toMatchObject({
      key: "purchases-analysis",
      label: "型号分析",
      path: "/purchases/analysis",
      perm: "page_purchases",
    });
  });

  it("采购组内两入口并存、标签互不重复（analysis ≠ analytics 不互相顶替）", () => {
    const labels = NAV_ITEMS.filter((it) => it.path.startsWith("/purchases"))
      .map((it) => it.label);
    expect(labels).toEqual(["采购分析", "型号分析", "采购异常", "采购明细"]);
    expect(new Set(labels).size).toBe(labels.length);
  });
});
