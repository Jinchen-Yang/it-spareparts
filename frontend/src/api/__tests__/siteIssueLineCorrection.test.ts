import { beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ patch: vi.fn(), post: vi.fn() }));
vi.mock("../../api", () => ({ api: { patch: mocks.patch, post: mocks.post } }));
import * as operations from "../maintenanceOperations";
beforeEach(() => vi.clearAllMocks());
it("单行更正使用稳定行 URL，明确发送 null 且不发送整单 lines", async () => {
  const input = { expected_issue_version: 7, expected_line_version: 2, changes: { no_return: null }, reason: "按规则判断", idempotency_key: "test-key" };
  await operations.correctSiteIssueLine("issue/a", "line/a", input);
  expect(mocks.patch).toHaveBeenCalledWith("/maintenance/site-issues/issue%2Fa/lines/line%2Fa", input);
});

it("单头更正使用独立 URL 与单据 CAS，地址明确空字符串不会被省略", async () => {
  const input = { expected_issue_version: 7, changes: { site_location: "" }, reason: "核对地址",
    idempotency_key: "header-key" };
  await operations.correctSiteIssueHeader("issue/a", input);
  expect(mocks.patch).toHaveBeenCalledWith("/maintenance/site-issues/issue%2Fa/header", input);
});

it("单据重读携带稳定 issue_id，不依赖可变单号或品类筛选", async () => {
  await operations.searchSiteIssues({ project_id: "p1", issue_id: "issue/a" });
  expect(mocks.post).toHaveBeenCalledWith("/maintenance/site-issues/search", {
    project_id: "p1", issue_id: "issue/a", workflow_statuses: ["draft", "confirmed", "corrected", "void"],
    page: 1, page_size: 20,
  });
});
