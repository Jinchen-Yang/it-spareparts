import { beforeEach, describe, expect, it, vi } from "vitest";

const { post, get } = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));

vi.mock("../../api", () => ({ api: { post, get } }));

import { getWbddMissing, uploadWbdd } from "../maintenanceWbddImport";

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
});
