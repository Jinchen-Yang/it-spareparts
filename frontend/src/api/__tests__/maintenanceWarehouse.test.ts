import { beforeEach, describe, expect, it, vi } from "vitest";

const { post, get } = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));
vi.mock("../../api", () => ({ api: { post, get } }));

import {
  applyWarehouseImport,
  previewWarehouseImport,
  searchWarehouseAmbiguities,
  searchWarehouseDocuments,
} from "../maintenanceWarehouse";


describe("maintenance warehouse API contract", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps searches in POST bodies and sends the same file through preview/apply", () => {
    const file = new File(["synthetic"], "synthetic.xlsx");
    previewWarehouseImport(file);
    const previewBody = post.mock.calls[0][1] as FormData;
    expect(post.mock.calls[0][0]).toBe("/maintenance/warehouse-imports/preview");
    expect(previewBody.get("file")).toBe(file);

    const preview = {
      import_id: "00000000-0000-0000-0000-000000000209",
      preview_token: "x".repeat(43),
    } as never;
    applyWarehouseImport(preview, file, "合成导入理由");
    const applyBody = post.mock.calls[1][1] as FormData;
    expect(post.mock.calls[1][0]).toContain("/warehouse-imports/00000000-0000-0000-0000-000000000209/apply");
    expect(applyBody.get("file")).toBe(file);
    expect(applyBody.get("preview_token")).toBe("x".repeat(43));
    expect(applyBody.get("reason")).toBe("合成导入理由");

    searchWarehouseDocuments({ q: "SYN-DOC", page: 2, page_size: 50 });
    searchWarehouseAmbiguities({ q: "SYN-DOC", status: "open", page: 1, page_size: 50 });
    expect(post).toHaveBeenNthCalledWith(3, "/maintenance/warehouse-documents/search", {
      q: "SYN-DOC", page: 2, page_size: 50,
    });
    expect(post).toHaveBeenNthCalledWith(4, "/maintenance/warehouse-ambiguities/search", {
      q: "SYN-DOC", status: "open", page: 1, page_size: 50,
    });
    expect(get).not.toHaveBeenCalled();
  });
});
