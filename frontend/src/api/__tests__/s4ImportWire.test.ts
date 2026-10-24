import { expect, it } from "vitest";
import wire from "../../../../docs/reviews/authz-rewrite-20261023/s4-import-wire-real-http.json";
import { type AuthzSnapshot, validateAuthzSnapshot } from "../authz";
import { validateAuthorizationResolution, validateContextualPayload } from "../../authz/contextProtocol";
import { batchDetail, batchList, jobResponse } from "../importProtocol";
import { normalizeExpenseVoidPreview, normalizeImportPrecheck } from "../imports";
it("consumes actual PG/PDP import read observations and payloads without repairing contexts", () => {
  const snapshot = validateAuthzSnapshot(wire.snapshot as unknown as AuthzSnapshot);
  for (const [entryId, resolution, payload] of [
    ["POST /api/import/precheck", wire.precheck_resolution, wire.precheck],
    ["GET /api/import/batches", wire.list_resolution, wire.list],
    ["GET /api/import/jobs/{job_id}", wire.partial_job_resolution, wire.partial_job],
    ["POST /api/import/expense-void-preview", wire.void_resolution, wire.void],
  ] as const) {
    const resolved = validateAuthorizationResolution(resolution, snapshot, { entryId });
    expect(validateContextualPayload(payload, snapshot, { entryId }, resolved.authorization_context)).toBe(payload);
  }
  expect(normalizeImportPrecheck(wire.precheck.content).contract).toBe("v2");
  expect(batchList(wire.list).items.length).toBeGreaterThan(0);
  expect(batchDetail(wire.report).report).not.toBeNull();
  expect(batchDetail(wire.status_only).report).toBeNull();
  expect(jobResponse(wire.partial_job).job.restricted).toBe(true);
  for (const preview of wire.void.previews) expect(normalizeExpenseVoidPreview(preview)).not.toBeNull();
});
