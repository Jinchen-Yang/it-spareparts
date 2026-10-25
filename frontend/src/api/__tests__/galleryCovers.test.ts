import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { GalleryList } from "../gallery";

const { fetchPartGallery } = vi.hoisted(() => ({ fetchPartGallery: vi.fn() }));
vi.mock("../gallery", () => ({ fetchPartGallery }));

let covers: typeof import("../galleryCovers");
const now = Date.parse("2026-10-25T12:00:00Z");

function gallery(partId = 42): GalleryList {
  return {
    part_id: partId, pn_id: "pn-id", pn_display: `PN-${partId}`, gallery_revision: 1,
    has_image: false, main_image: null, images: [], candidates: [],
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

beforeEach(async () => {
  vi.resetModules();
  fetchPartGallery.mockReset();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(now);
  covers = await import("../galleryCovers");
});

afterEach(() => vi.useRealTimers());

it("同一 token 和 partId 的并发请求复用同一个 Promise", async () => {
  const pending = deferred<GalleryList>();
  fetchPartGallery.mockReturnValue(pending.promise);
  const first = covers.getCover(42, "token-a");
  const second = covers.getCover(42, "token-a");
  expect(first).toBe(second);
  await Promise.resolve();
  expect(fetchPartGallery).toHaveBeenCalledExactlyOnceWith(42, "token-a");
  pending.resolve(gallery());
  await expect(first).resolves.toEqual(gallery());
});

it("全局最多 4 个请求，完成一个才启动下一个排队请求", async () => {
  const pending = Array.from({ length: 7 }, () => deferred<GalleryList>());
  let running = 0;
  let peak = 0;
  fetchPartGallery.mockImplementation((partId: number) => {
    running += 1;
    peak = Math.max(peak, running);
    return pending[partId - 1].promise.then((value) => { running -= 1; return value; });
  });
  const requests = pending.map((_, index) => covers.getCover(index + 1, index % 2 ? "token-b" : "token-a"));
  await Promise.resolve();
  expect(fetchPartGallery).toHaveBeenCalledTimes(4);
  pending[0].resolve(gallery(1));
  await requests[0];
  await Promise.resolve();
  expect(fetchPartGallery).toHaveBeenCalledTimes(5);
  for (let index = 1; index < pending.length; index += 1) pending[index].resolve(gallery(index + 1));
  await Promise.all(requests);
  expect(fetchPartGallery).toHaveBeenCalledTimes(7);
  expect(peak).toBe(4);
});

it("有效期内命中缓存，缺少签名到期字段时最多保留 5 分钟", async () => {
  fetchPartGallery.mockResolvedValue(gallery());
  const first = await covers.getCover(42, "token-a");
  vi.setSystemTime(now + 299_999);
  expect(await covers.getCover(42, "token-a")).toBe(first);
  expect(fetchPartGallery).toHaveBeenCalledTimes(1);
  vi.setSystemTime(now + 300_000);
  await covers.getCover(42, "token-a");
  expect(fetchPartGallery).toHaveBeenCalledTimes(2);
});

it("按所有图片中最早的签名有效期提前 60 秒失效", async () => {
  fetchPartGallery.mockResolvedValue({
    ...gallery(),
    images: [
      { url_expires_at: new Date(now + 240_000).toISOString() },
      { url_expires_at: new Date(now + 180_000).toISOString() },
    ],
  });
  await covers.getCover(42, "token-a");
  vi.setSystemTime(now + 119_999);
  await covers.getCover(42, "token-a");
  expect(fetchPartGallery).toHaveBeenCalledTimes(1);
  vi.setSystemTime(now + 120_000);
  await covers.getCover(42, "token-a");
  expect(fetchPartGallery).toHaveBeenCalledTimes(2);
});

it("签名有效期较长时依然在 5 分钟后重新请求", async () => {
  fetchPartGallery.mockResolvedValue({
    ...gallery(), images: [{ url_expires_at: new Date(now + 3_600_000).toISOString() }],
  });
  await covers.getCover(42, "token-a");
  vi.setSystemTime(now + 300_000);
  await covers.getCover(42, "token-a");
  expect(fetchPartGallery).toHaveBeenCalledTimes(2);
});

it("invalidateCover 清除所有 token 下该备件的缓存并通知订阅者", async () => {
  fetchPartGallery.mockResolvedValue(gallery());
  await covers.getCover(42, "token-a");
  await covers.getCover(42, "token-b");
  const listener = vi.fn();
  const unsubscribe = covers.subscribeCover(42, listener);
  covers.invalidateCover(42);
  expect(listener).toHaveBeenCalledTimes(1);
  await covers.getCover(42, "token-a");
  await covers.getCover(42, "token-b");
  expect(fetchPartGallery).toHaveBeenCalledTimes(4);
  unsubscribe();
  covers.invalidateCover(42);
  expect(listener).toHaveBeenCalledTimes(1);
});

it("失效期间旧请求不会回填缓存或覆盖新的请求", async () => {
  const old = deferred<GalleryList>();
  const fresh = deferred<GalleryList>();
  fetchPartGallery.mockReturnValueOnce(old.promise).mockReturnValueOnce(fresh.promise);
  const before = covers.getCover(42, "token-a");
  covers.invalidateCover(42);
  const after = covers.getCover(42, "token-a");
  expect(after).not.toBe(before);
  fresh.resolve({ ...gallery(), gallery_revision: 2 });
  await after;
  old.resolve(gallery());
  await before;
  expect((await covers.getCover(42, "token-a")).gallery_revision).toBe(2);
  expect(fetchPartGallery).toHaveBeenCalledTimes(2);
});

it("token 变化后不复用前一个账号的缓存或进行中的请求", async () => {
  const old = deferred<GalleryList>();
  fetchPartGallery.mockReturnValueOnce(old.promise).mockResolvedValue(gallery());
  const first = covers.getCover(42, "token-a");
  await covers.getCover(42, "token-b");
  expect(fetchPartGallery).toHaveBeenCalledTimes(2);
  old.resolve(gallery());
  await first;
  await covers.getCover(42, "token-c");
  expect(fetchPartGallery).toHaveBeenCalledTimes(3);
});

it("请求失败不缓存错误，排队请求仍可继续执行", async () => {
  const pending = Array.from({ length: 4 }, () => deferred<GalleryList>());
  fetchPartGallery.mockImplementation((partId: number) => (
    pending[partId - 1]?.promise ?? Promise.resolve(gallery(partId))
  ));
  const requests = Array.from({ length: 5 }, (_, index) => covers.getCover(index + 1, "token-a"));
  const rejection = expect(requests[0]).rejects.toThrow("unavailable");
  await Promise.resolve();
  expect(fetchPartGallery).toHaveBeenCalledTimes(4);
  pending[0].reject(new Error("unavailable"));
  await rejection;
  await expect(requests[4]).resolves.toEqual(gallery(5));
  expect(fetchPartGallery).toHaveBeenCalledTimes(5);
  for (let index = 1; index < pending.length; index += 1) pending[index].resolve(gallery(index + 1));
  await Promise.all(requests.slice(1));
  fetchPartGallery.mockResolvedValue(gallery(1));
  await expect(covers.getCover(1, "token-a")).resolves.toEqual(gallery(1));
  expect(fetchPartGallery).toHaveBeenCalledTimes(6);
});

it("响应结构无效时拒绝请求并释放并发槽位，不悬挂排队请求", async () => {
  const pending = Array.from({ length: 4 }, () => deferred<unknown>());
  fetchPartGallery.mockImplementation((partId: number) => (
    pending[partId - 1]?.promise ?? Promise.resolve(gallery(partId))
  ));
  const requests = Array.from({ length: 5 }, (_, index) => covers.getCover(index + 1, "token-a"));
  const rejection = expect(requests[0]).rejects.toThrow("图库响应无效");
  await Promise.resolve();
  expect(fetchPartGallery).toHaveBeenCalledTimes(4);
  pending[0].resolve({ categories: [] });
  await rejection;
  await expect(requests[4]).resolves.toEqual(gallery(5));
  for (let index = 1; index < pending.length; index += 1) pending[index].resolve(gallery(index + 1));
  await Promise.all(requests.slice(1));
  fetchPartGallery.mockResolvedValue(gallery(1));
  await expect(covers.getCover(1, "token-a")).resolves.toEqual(gallery(1));
  expect(fetchPartGallery).toHaveBeenCalledTimes(6);
});
