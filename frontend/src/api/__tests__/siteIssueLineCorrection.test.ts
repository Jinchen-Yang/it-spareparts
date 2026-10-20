import { beforeEach, expect, it, vi } from "vitest";
const mocks = vi.hoisted(() => ({ patch: vi.fn() }));
vi.mock("../../api", () => ({ api: { patch: mocks.patch } }));
import * as operations from "../maintenanceOperations";
beforeEach(() => vi.clearAllMocks());
it("单行更正使用稳定行 URL，明确发送 null 且不发送整单 lines", async () => {
  const input = { expected_issue_version: 7, expected_line_version: 2, changes: { no_return: null }, reason: "按规则判断", idempotency_key: "test-key" };
  await operations.correctSiteIssueLine("issue/a", "line/a", input);
  expect(mocks.patch).toHaveBeenCalledWith("/maintenance/site-issues/issue%2Fa/lines/line%2Fa", input);
});
