import { beforeEach, describe, expect, it } from "vitest";

import { maintenanceBetaCompatTarget } from "../MaintenanceBetaCompatRedirect";


describe("旧维保工作台深链兼容", () => {
  beforeEach(() => localStorage.clear());

  it("总闸关闭或能力快照损坏时回到稳定版", () => {
    expect(maintenanceBetaCompatTarget({
      pathname: "/maintenance/projects/PRJ-1",
      search: "?project_id=secret-beta-project",
      hash: "#beta-panel",
    })).toBe(
      "/maintenance",
    );
    localStorage.setItem("beta_features", "not-json");
    expect(maintenanceBetaCompatTarget({
      pathname: "/maintenance/project-master",
      search: "?source=legacy",
      hash: "#source-orders",
    })).toBe(
      "/maintenance",
    );
  });

  it("服务端签发 Beta 能力后保留原深链上下文", () => {
    localStorage.setItem(
      "beta_features",
      JSON.stringify({ maintenance: true }),
    );
    expect(maintenanceBetaCompatTarget({
      pathname: "/maintenance/projects/PRJ-1",
      search: "?reminder=warning&owner=%E5%BC%A0%E4%B8%89",
      hash: "#cost-progress",
    })).toBe(
      "/maintenance/beta/projects/PRJ-1?reminder=warning&owner=%E5%BC%A0%E4%B8%89#cost-progress",
    );
    expect(
      maintenanceBetaCompatTarget({
        pathname: "/maintenance/project-master/source-orders",
        search: "",
        hash: "",
      }),
    ).toBe("/maintenance/beta/project-master/source-orders");
  });
});
