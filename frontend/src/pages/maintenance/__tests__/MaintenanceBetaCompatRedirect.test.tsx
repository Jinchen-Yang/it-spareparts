import { beforeEach, describe, expect, it } from "vitest";

import { maintenanceBetaCompatTarget } from "../MaintenanceBetaCompatRedirect";


describe("旧维保工作台深链兼容", () => {
  beforeEach(() => localStorage.clear());

  it("总闸关闭或能力快照损坏时回到稳定版", () => {
    expect(maintenanceBetaCompatTarget("/maintenance/projects/PRJ-1")).toBe(
      "/maintenance",
    );
    localStorage.setItem("beta_features", "not-json");
    expect(maintenanceBetaCompatTarget("/maintenance/project-master")).toBe(
      "/maintenance",
    );
  });

  it("服务端签发 Beta 能力后保留原深链上下文", () => {
    localStorage.setItem(
      "beta_features",
      JSON.stringify({ maintenance: true }),
    );
    expect(maintenanceBetaCompatTarget("/maintenance/projects/PRJ-1")).toBe(
      "/maintenance/beta/projects/PRJ-1",
    );
    expect(
      maintenanceBetaCompatTarget("/maintenance/project-master/source-orders"),
    ).toBe("/maintenance/beta/project-master/source-orders");
  });
});
