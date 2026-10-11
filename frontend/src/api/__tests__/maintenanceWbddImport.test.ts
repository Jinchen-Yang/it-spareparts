import { beforeEach, describe, expect, it, vi } from "vitest";

const { post, get } = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));

vi.mock("../../api", () => ({ api: { post, get } }));

import { getWbddMissing, reconcileWbdd, uploadWbdd } from "../maintenanceWbddImport";

describe("WBDD import API contract", () => {
  beforeEach(() => vi.clearAllMocks());

  it("upload posts multipart form with the idempotency key header", () => {
    const file = new File(["x"], "wbdd.xlsx");
    uploadWbdd(file, "wbdd-key-1");
    const [path, body, config] = post.mock.calls[0];
    expect(path).toBe("/maintenance/wbdd-imports");
    expect(body).toBeInstanceOf(FormData);
    expect((body as FormData).get("file")).toBe(file);
    expect(config).toEqual({ headers: { "Idempotency-Key": "wbdd-key-1" } });
  });

  it("missing diff list reads the latest snapshot endpoint (#265)", () => {
    getWbddMissing();
    expect(get).toHaveBeenCalledWith("/maintenance/wbdd-imports/latest/missing");
  });

  it("reconciles selected identities using the exact preview and idempotency key", () => {
    const payload = { expected_digest: "preview-digest", source_order_ids: ["source-a"],
      line_ids: [3], reason: "已核对原件", idempotency_key: "reconcile-key" };
    reconcileWbdd(17, payload);
    expect(post).toHaveBeenCalledWith("/maintenance/wbdd-imports/17/reconcile", payload);
  });
});
