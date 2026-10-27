import { afterEach, beforeEach, expect, it, vi } from "vitest";

const { post } = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock("../../api", () => ({ default: { post } }));
import { auditGalleryDownload } from "../gallery";

const fetchAudit = vi.fn<typeof fetch>();

beforeEach(() => {
  post.mockReset();
  fetchAudit.mockReset().mockResolvedValue({ ok: true, status: 204 } as Response);
  vi.stubGlobal("fetch", fetchAudit);
  localStorage.setItem("token", "current-session-token");
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

it.each(["single", "zip"] as const)("图库 %s 下载审计使用独立接口和 Bearer 登录态，只提交型号与图片 ID", async (kind) => {
  const items = [{ part_id: 42, image_ids: ["image-a", "image-b"] }, { part_id: 43, image_ids: ["image-c"] }];

  await expect(auditGalleryDownload(items, kind, "bound-gallery-token")).resolves.toBeUndefined();

  expect(fetchAudit).toHaveBeenCalledExactlyOnceWith("/api/parts/gallery/download-audit", {
    method: "POST",
    headers: { Authorization: "Bearer bound-gallery-token", "Content-Type": "application/json" },
    body: JSON.stringify({ items, kind }),
  });
  expect(JSON.parse(String(fetchAudit.mock.calls[0][1]?.body))).toEqual({ items, kind });
  expect(String(fetchAudit.mock.calls[0][1]?.body)).not.toMatch(/https?:|url|signature/);
  expect(post).not.toHaveBeenCalled();
});

it("审计网络错误交给下载调用方处理", async () => {
  fetchAudit.mockRejectedValue(new Error("audit unavailable"));
  await expect(auditGalleryDownload([{ part_id: 42, image_ids: ["image-a"] }], "single", "token"))
    .rejects.toThrow("audit unavailable");
});

it("审计 401 不进入全局认证拦截器，也不清除现有账号 token", async () => {
  fetchAudit.mockResolvedValue({ ok: false, status: 401 } as Response);
  await expect(auditGalleryDownload([{ part_id: 42, image_ids: ["image-a"] }], "zip", "expired-gallery-token"))
    .rejects.toThrow();
  expect(post).not.toHaveBeenCalled();
  expect(localStorage.getItem("token")).toBe("current-session-token");
});
