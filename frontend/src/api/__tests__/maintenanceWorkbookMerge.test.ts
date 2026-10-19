import { beforeEach, describe, expect, it, vi } from "vitest";

const { post } = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("../../api", () => ({ api: { post } }));

import { applyProjectMaster } from "../maintenanceWorkbooks";

describe("项目总表合并预览凭据", () => {
  beforeEach(() => {
    post.mockReset();
    post.mockResolvedValue({ data: { site_issue_merge_count: 1 } });
  });

  it("将预检凭据与原文件一起提交，同时保留强制接管选项", async () => {
    const file = new File(["synthetic"], "合并.xlsx");
    await applyProjectMaster("project/id", file, {
      forceTakeover: true, siteIssueMergeToken: "signed-preview",
    });
    expect(post.mock.calls[0][0]).toBe("/maintenance/projects/stable/project%2Fid/master-workbook/apply");
    const body = post.mock.calls[0][1] as FormData;
    expect(body.get("file")).toBe(file);
    expect(body.get("site_issue_merge_token")).toBe("signed-preview");
    expect(body.get("force_takeover")).toBe("true");
  });

  it("非合并上传保持原协议，不添加空凭据", async () => {
    await applyProjectMaster("project", new File(["synthetic"], "普通.xlsx"));
    const body = post.mock.calls[0][1] as FormData;
    expect(body.has("site_issue_merge_token")).toBe(false);
    expect(body.has("force_takeover")).toBe(false);
  });
});
