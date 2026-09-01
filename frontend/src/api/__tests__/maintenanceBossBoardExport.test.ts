import { beforeEach, describe, expect, it, vi } from "vitest";

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));

vi.mock("../../api", () => ({ api: { get, post } }));

import {
  downloadBoardProjectsExport,
  getBoardProjectExportOptions,
} from "../maintenanceBossBoard";

beforeEach(() => vi.clearAllMocks());

describe("maintenance boss-board project export API", () => {
  it("loads the permission-filtered field catalog", () => {
    getBoardProjectExportOptions();
    expect(get).toHaveBeenCalledWith("/maintenance/boss-board/projects/export/options");
  });

  it("posts selected fields and filters as JSON and keeps the UTF-8 attachment name", async () => {
    const blob = new Blob(["xlsx"]);
    post.mockResolvedValue({
      data: blob,
      headers: {
        "content-disposition": [
          "attachment; filename=maintenance-projects-20260827.xlsx; ",
          "filename*=UTF-8''%E7%BB%B4%E4%BF%9D%E9%A1%B9%E7%9B%AE%E6%B8%85%E5%8D%95-20260827.xlsx",
        ].join(""),
      },
    });
    const body = {
      fields: ["project_name", "period_to"],
      q: "项目甲",
      lifecycle: "ongoing" as const,
      sort: "name" as const,
    };

    const result = await downloadBoardProjectsExport(body);

    expect(post).toHaveBeenCalledWith(
      "/maintenance/boss-board/projects/export",
      body,
      { responseType: "blob" },
    );
    expect(result).toEqual({ blob, filename: "维保项目清单-20260827.xlsx" });
  });
});
