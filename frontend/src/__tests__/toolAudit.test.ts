import { describe, expect, it } from "vitest";

import { summarizeToolAudit } from "../toolAudit";

describe("content-free tool audit UI", () => {
  it("renders counts and artifact IDs without ever rendering argument values", () => {
    const sentinel = "CUSTOMER-FRONTEND-SECRET-f807";
    const rendered = summarizeToolAudit({
      outcome: "success",
      arg_count: 3,
      arg_keys: ["queries", sentinel],
      query_count: 2,
      artifact_ids: ["01234567-89ab-4cde-8fab-0123456789ab"],
    });

    expect(rendered).toContain("2 个查询");
    expect(rendered).toContain("01234567…");
    expect(rendered).not.toContain(sentinel);
  });

  it("falls back to an argument count rather than values", () => {
    expect(summarizeToolAudit({
      outcome: "recorded",
      arg_count: 4,
      arg_keys: ["query"],
    })).toBe("4 项参数");
  });
});
