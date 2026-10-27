import { strFromU8, unzipSync } from "fflate";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GalleryImage } from "../../../api/gallery";
import type { CoverGallery } from "../../../api/galleryCovers";
import {
  galleryFilename, galleryZipFilename, MAX_GALLERY_BYTES, MAX_GALLERY_IMAGES,
  saveGalleryDownload, type GalleryDownloadItem,
} from "../galleryDownloads";

const { auditGalleryDownload, getCover, invalidateCover } = vi.hoisted(() => ({
  auditGalleryDownload: vi.fn(), getCover: vi.fn(), invalidateCover: vi.fn(),
}));
vi.mock("../../../api/gallery", () => ({ auditGalleryDownload }));
vi.mock("../../../api/galleryCovers", () => ({ getCover, invalidateCover }));

const now = Date.parse("2026-10-25T12:00:00Z");
const fetchImage = vi.fn<typeof fetch>();
const savedBlobs: Blob[] = [];
const savedNames: string[] = [];
const createObjectURL = vi.fn((blob: Blob) => {
  savedBlobs.push(blob);
  return `blob:gallery-${savedBlobs.length}`;
});
const revokeObjectURL = vi.fn();
const NativeURL = URL;
const { Blob: NodeBlob } = await vi.importActual<{ Blob: typeof Blob }>("node:buffer");

function image(id = "image-1", overrides: Partial<GalleryImage> = {}): GalleryImage {
  return {
    id, file_no: 1, filename: `${id}.jpg`, pn_id: "pn-1",
    url: `https://oss.example/${id}?signature=secret`,
    url_expires_at: new Date(now + 300_000).toISOString(), thumb_url: null,
    width: 100, height: 100, bytes: 10, content_type: "image/jpeg",
    source: "upload", status: "gallery", sort_order: 0, is_defect: false,
    has_foreign_watermark: false, thumbnail_status: "ready", created_at: "2026-10-25T00:00:00Z",
    ...overrides,
  };
}

function item(id = "image-1", overrides: Partial<GalleryDownloadItem> = {}): GalleryDownloadItem {
  return { partId: 42, pn: "PN-42", image: image(id), index: 1, ...overrides };
}

function gallery(images: GalleryImage[], partId = 42): CoverGallery {
  return {
    part_id: partId, pn_id: `pn-${partId}`, pn_display: `PN-${partId}`, gallery_revision: 2,
    has_image: Boolean(images.length), main_image: images[0] || null, images, candidates: [],
  };
}

function response(content = "图片内容", status = 200): Response {
  return { ok: status >= 200 && status < 300, status, blob: async () => new Blob([content]) } as Response;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function download(items: GalleryDownloadItem[], options: {
  kind?: "single" | "zip"; signal?: AbortSignal; onProgress?: (completed: number, total: number) => void;
} = {}) {
  return saveGalleryDownload({
    items, kind: options.kind || "zip", token: "gallery-token",
    signal: options.signal || new AbortController().signal, onProgress: options.onProgress,
  });
}

async function unpackSavedZip() {
  expect(savedBlobs).toHaveLength(1);
  expect(savedBlobs[0].type).toBe("application/zip");
  return unzipSync(new Uint8Array(await savedBlobs[0].arrayBuffer()));
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
  vi.setSystemTime(now);
  vi.clearAllMocks();
  auditGalleryDownload.mockReset().mockResolvedValue(undefined);
  getCover.mockReset();
  fetchImage.mockReset().mockResolvedValue(response());
  savedBlobs.length = 0;
  savedNames.length = 0;
  // jsdom 的 Blob 缺少 arrayBuffer，使用 Node 原生 Blob 保留真实字节及切片语义。
  vi.stubGlobal("Blob", NodeBlob);
  vi.stubGlobal("fetch", fetchImage);
  vi.stubGlobal("URL", class extends NativeURL {
    static createObjectURL = createObjectURL;
    static revokeObjectURL = revokeObjectURL;
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    expect(this.isConnected).toBe(true);
    expect(this.href).toMatch(/^blob:gallery-/);
    savedNames.push(this.download);
  });
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("图库下载文件名", () => {
  it("替换 PN 的路径、控制及 Windows 非法字符，保留中文和从 1 开始的序号", () => {
    expect(galleryFilename('中/文\\PN:<>"|?*\u0000\u001f\u007f', 3, image()))
      .toBe("中_文_PN___________3.jpg");
  });

  it.each([
    ["", "图片_1.jpg"], ["..", "___1.jpg"], ["PN. ", "PN___1.jpg"],
    ["CON", "_CON_1.jpg"], ["lpt9.txt", "_lpt9.txt_1.jpg"],
  ])("处理无法直接作目录名的 PN %j", (pn, expected) => {
    expect(galleryFilename(pn, 1, image())).toBe(expected);
  });

  it.each([
    ["image/jpeg", "misleading.png", "jpg"], ["IMAGE/PNG", "source.jpg", "png"],
    ["image/webp", "source.jpg", "webp"], ["image/gif", "source.jpg", "gif"],
    ["image/avif", "source.jpg", "avif"], ["application/octet-stream", "source.TIFF", "tiff"],
    ["application/octet-stream", "source.exe", "bin"], ["application/octet-stream", "source", "bin"],
  ])("根据内容类型或安全原扩展名生成 %s / %s", (content_type, filename, extension) => {
    expect(galleryFilename("PN", 12, image("image-1", { content_type, filename })))
      .toBe(`PN_12.${extension}`);
  });

  it("ZIP 名使用本地年月日和小时分钟，并补齐两位", () => {
    expect(galleryZipFilename(new Date(2026, 0, 2, 3, 4, 59))).toBe("图片_20260102-0304.zip");
  });
});

it("单张用 blob 保存并使用给定的图库序号，OSS 请求不携带登录态", async () => {
  const bytes = new Uint8Array([0, 1, 127, 128, 255]);
  fetchImage.mockResolvedValue({ ok: true, blob: async () => new Blob([bytes], { type: "image/png" }) } as Response);
  const selected = item("single", { pn: "PN/中文", index: 4, image: image("single", { content_type: "image/png" }) });
  const controller = new AbortController();
  const progress = vi.fn();

  await expect(download([selected], { kind: "single", signal: controller.signal, onProgress: progress }))
    .resolves.toEqual({ downloaded: 1, failed: [] });

  expect(fetchImage).toHaveBeenCalledExactlyOnceWith(selected.image.url, {
    signal: expect.any(AbortSignal), credentials: "omit", referrerPolicy: "no-referrer",
  });
  expect(fetchImage.mock.calls[0][1]?.signal?.aborted).toBe(false);
  expect(savedNames).toEqual(["PN_中文_4.png"]);
  expect(Array.from(new Uint8Array(await savedBlobs[0].arrayBuffer()))).toEqual(Array.from(bytes));
  expect(progress.mock.calls).toEqual([[0, 1], [1, 1]]);
  expect(document.querySelector("a[download]")).toBeNull();
  expect(revokeObjectURL).not.toHaveBeenCalled();
  vi.advanceTimersByTime(1000);
  expect(revokeObjectURL).toHaveBeenCalledExactlyOnceWith("blob:gallery-1");
});

it("真实 fflate ZIP 按 PN 分目录，保留大于一个流式切片的全部字节", async () => {
  const largeContent = "A".repeat(140_000) + "图片尾部";
  fetchImage.mockImplementation(async (url) => response(String(url).includes("large") ? largeContent : "第二个型号"));

  await expect(download([
    item("large", { pn: "PN/一", index: 2 }),
    item("other", { partId: 43, pn: "PN-二", image: image("other", { content_type: "image/webp" }) }),
  ])).resolves.toEqual({ downloaded: 2, failed: [] });

  const entries = await unpackSavedZip();
  expect(Object.keys(entries).sort()).toEqual(["PN-二/PN-二_1.webp", "PN_一/PN_一_2.jpg"]);
  expect(strFromU8(entries["PN_一/PN_一_2.jpg"])).toBe(largeContent);
  expect(strFromU8(entries["PN-二/PN-二_1.webp"])).toBe("第二个型号");
  expect(savedNames).toEqual([galleryZipFilename()]);
});

it("PN 清洗后重名时使用型号 ID 区分目录，解压不覆盖同名图片", async () => {
  await download([
    item("one", { pn: "PN/A" }),
    item("two", { pn: "PN:A", partId: 43 }),
    item("three", { pn: "pn_a", partId: 44 }),
  ]);
  const entries = await unpackSavedZip();
  expect(Object.keys(entries).sort()).toEqual(["PN_A/PN_A_1.jpg", "PN_A_43/PN_A_1.jpg", "pn_a_44/pn_a_1.jpg"]);
});

it("最多四个并发 fetch，完成一张后继续排队，进度从零递增到总数", async () => {
  const requests = Array.from({ length: 9 }, () => deferred<Response>());
  let running = 0;
  let peak = 0;
  fetchImage.mockImplementation((_url, options) => {
    const index = fetchImage.mock.calls.length - 1;
    expect(options?.signal?.aborted).toBe(false);
    running += 1;
    peak = Math.max(peak, running);
    return requests[index].promise.finally(() => { running -= 1; });
  });
  const progress = vi.fn();
  const pending = download(requests.map((_, index) => item(`image-${index}`, { index: index + 1 })), { onProgress: progress });
  expect(fetchImage).toHaveBeenCalledTimes(4);
  expect(progress.mock.calls).toEqual([[0, 9]]);

  requests[0].resolve(response("first"));
  await vi.waitFor(() => expect(fetchImage).toHaveBeenCalledTimes(5));
  expect(progress).toHaveBeenLastCalledWith(1, 9);
  for (let index = 1; index < requests.length; index += 1) requests[index].resolve(response(`image-${index}`));
  await expect(pending).resolves.toEqual({ downloaded: 9, failed: [] });

  expect(peak).toBe(4);
  expect(progress.mock.calls).toEqual(Array.from({ length: 10 }, (_, completed) => [completed, 9]));
  expect(Object.keys(await unpackSavedZip())).toHaveLength(9);
});

it("取消会中止所有进行中的 fetch，停止排队和重试，且不保存 ZIP", async () => {
  const signals: AbortSignal[] = [];
  const aborted: AbortSignal[] = [];
  fetchImage.mockImplementation((_url, options) => new Promise((_resolve, reject) => {
    const signal = options!.signal!;
    signals.push(signal);
    signal.addEventListener("abort", () => {
      aborted.push(signal);
      reject(new DOMException("aborted", "AbortError"));
    }, { once: true });
  }));
  const controller = new AbortController();
  const pending = download(Array.from({ length: 8 }, (_, index) => item(`image-${index}`)), { signal: controller.signal });
  const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
  expect(fetchImage).toHaveBeenCalledTimes(4);
  controller.abort();
  await rejected;

  expect(signals).toHaveLength(4);
  expect(aborted).toHaveLength(4);
  expect(signals.every((signal) => signal.aborted)).toBe(true);
  expect(fetchImage).toHaveBeenCalledTimes(4);
  expect(getCover).not.toHaveBeenCalled();
  expect(savedBlobs).toHaveLength(0);
  expect(savedNames).toHaveLength(0);
});

it("签名刷新尚未返回时取消立即结束，刷新迟到后也不继续 fetch", async () => {
  const refresh = deferred<CoverGallery>();
  getCover.mockReturnValue(refresh.promise);
  const selected = item("expired", { image: image("expired", { url_expires_at: new Date(now).toISOString() }) });
  const controller = new AbortController();
  const pending = download([selected], { signal: controller.signal });
  const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
  controller.abort();
  await rejected;
  refresh.resolve(gallery([image("expired")]));
  await Promise.resolve();
  expect(fetchImage).not.toHaveBeenCalled();
  expect(savedNames).toHaveLength(0);
});

it("已有图片进入 ZIP 后取消仍丢弃整个压缩包，并中止余下请求", async () => {
  const controller = new AbortController();
  const pendingFetches: AbortSignal[] = [];
  fetchImage.mockImplementation((_url, options) => {
    if (fetchImage.mock.calls.length === 1) return Promise.resolve(response("completed"));
    const signal = options!.signal!;
    pendingFetches.push(signal);
    return new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
    });
  });
  const progress = vi.fn();
  const pending = download(Array.from({ length: 5 }, (_, index) => item(`image-${index}`, { index: index + 1 })), {
    signal: controller.signal, onProgress: progress,
  });
  const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
  await vi.waitFor(() => expect(progress).toHaveBeenCalledWith(1, 5));
  controller.abort();
  await rejected;
  expect(pendingFetches).toHaveLength(4);
  expect(pendingFetches.every((signal) => signal.aborted)).toBe(true);
  expect(savedNames).toHaveLength(0);
});

it("读取 ZIP 图片切片出现致命错误时中止其它请求，不保存半成品", async () => {
  const brokenBlob = new Blob(["bytes"]);
  const chunk = new Blob(["bytes"]);
  vi.spyOn(chunk, "arrayBuffer").mockRejectedValue(new Error("slice read failed"));
  vi.spyOn(brokenBlob, "slice").mockReturnValue(chunk);
  const remainingSignals: AbortSignal[] = [];
  fetchImage.mockImplementation((_url, options) => {
    if (fetchImage.mock.calls.length === 1) {
      return Promise.resolve({ ok: true, blob: async () => brokenBlob } as Response);
    }
    const signal = options!.signal!;
    remainingSignals.push(signal);
    return new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
    });
  });
  await expect(download(Array.from({ length: 4 }, (_, index) => item(`image-${index}`, { index: index + 1 }))))
    .rejects.toThrow("slice read failed");
  expect(remainingSignals).toHaveLength(3);
  expect(remainingSignals.every((signal) => signal.aborted)).toBe(true);
  expect(fetchImage).toHaveBeenCalledTimes(4);
  expect(savedNames).toHaveLength(0);
});

it("某张失败后刷新签名重试一次，仍失败列入清单，成功图片继续打包", async () => {
  const bad = image("bad");
  const refreshedBad = image("bad", { url: "https://oss.example/bad-refreshed?signature=new" });
  getCover.mockResolvedValue(gallery([refreshedBad]));
  fetchImage.mockImplementation(async (url) => (
    String(url).includes("bad") ? response("denied", 403) : response("good-bytes")
  ));
  const progress = vi.fn();

  await expect(download([
    item("bad", { image: bad, index: 2 }), item("good", { index: 3 }),
  ], { onProgress: progress })).resolves.toEqual({ downloaded: 1, failed: ["PN-42/PN-42_2.jpg"] });

  expect(fetchImage.mock.calls.filter(([url]) => String(url).includes("bad")).map(([url]) => url))
    .toEqual([bad.url, refreshedBad.url]);
  expect(getCover).toHaveBeenCalledExactlyOnceWith(42, "gallery-token");
  expect(invalidateCover).toHaveBeenCalledExactlyOnceWith(42);
  expect(progress.mock.calls).toEqual([[0, 2], [1, 2], [2, 2]]);
  const entries = await unpackSavedZip();
  expect(Object.keys(entries)).toEqual(["PN-42/PN-42_3.jpg"]);
  expect(strFromU8(entries["PN-42/PN-42_3.jpg"])).toBe("good-bytes");
  expect(auditGalleryDownload).toHaveBeenCalledTimes(1);
});

it("网络错误后刷新签名，重试成功时仍保留原文件名和序号", async () => {
  getCover.mockResolvedValue(gallery([image("retry", { url: "https://oss.example/fresh" })]));
  fetchImage.mockRejectedValueOnce(new TypeError("network error")).mockResolvedValueOnce(response("retried"));
  await expect(download([item("retry", { index: 7 })])).resolves.toEqual({ downloaded: 1, failed: [] });
  expect(fetchImage).toHaveBeenCalledTimes(2);
  expect(fetchImage.mock.calls[1][0]).toBe("https://oss.example/fresh");
  expect(strFromU8((await unpackSavedZip())["PN-42/PN-42_7.jpg"])).toBe("retried");
});

it("同型号同时失败只共享一次签名刷新，每张最多两次 fetch", async () => {
  const refresh = deferred<CoverGallery>();
  getCover.mockReturnValue(refresh.promise);
  fetchImage.mockResolvedValue(response("denied", 403));
  const selected = Array.from({ length: 4 }, (_, index) => item(`bad-${index}`, { index: index + 1 }));
  const pending = download(selected);
  await vi.waitFor(() => expect(getCover).toHaveBeenCalledTimes(1));
  refresh.resolve(gallery(selected.map((selectedItem) => selectedItem.image)));
  await expect(pending).resolves.toMatchObject({ downloaded: 0 });
  expect(fetchImage).toHaveBeenCalledTimes(8);
  expect(invalidateCover).toHaveBeenCalledTimes(1);
  expect(savedNames).toHaveLength(0);
});

it.each([59_999, 0, -1])("剩余 %i 毫秒时先刷新签名再直接拉取 OSS", async (remaining) => {
  const refreshed = image("near-expiry", { url: "https://oss.example/fresh-signed" });
  getCover.mockResolvedValue(gallery([refreshed]));
  await download([item("near-expiry", { image: image("near-expiry", { url_expires_at: new Date(now + remaining).toISOString() }) })]);
  expect(invalidateCover).toHaveBeenCalledExactlyOnceWith(42);
  expect(getCover).toHaveBeenCalledExactlyOnceWith(42, "gallery-token");
  expect(fetchImage).toHaveBeenCalledTimes(1);
  expect(fetchImage.mock.calls[0][0]).toBe(refreshed.url);
  expect(getCover.mock.invocationCallOrder[0]).toBeLessThan(fetchImage.mock.invocationCallOrder[0]);
});

it.each([60_000, 60_001, 300_000])("剩余 %i 毫秒的有效签名不刷新", async (remaining) => {
  const original = image("valid", { url_expires_at: new Date(now + remaining).toISOString() });
  await download([item("valid", { image: original })]);
  expect(getCover).not.toHaveBeenCalled();
  expect(invalidateCover).not.toHaveBeenCalled();
  expect(fetchImage.mock.calls[0][0]).toBe(original.url);
});

it("同型号多张临期图片共享一次下载前刷新", async () => {
  const selected = ["first", "second", "third"].map((id, index) => item(id, {
    index: index + 1, image: image(id, { url_expires_at: new Date(now).toISOString() }),
  }));
  getCover.mockResolvedValue(gallery(selected.map(({ image: selectedImage }) => ({
    ...selectedImage, url: `${selectedImage.url}&fresh=1`,
  }))));
  await expect(download(selected)).resolves.toEqual({ downloaded: 3, failed: [] });
  expect(getCover).toHaveBeenCalledTimes(1);
  expect(invalidateCover).toHaveBeenCalledTimes(1);
  expect(fetchImage.mock.calls.every(([url]) => String(url).endsWith("&fresh=1"))).toBe(true);
});

it("长任务跨过已刷新签名的安全有效期后，后续同型号图片再次真实刷新", async () => {
  const selected = Array.from({ length: 8 }, (_, index) => item(`image-${index}`, {
    index: index + 1, image: image(`image-${index}`, { url_expires_at: new Date(now).toISOString() }),
  }));
  const initialGallery = gallery(selected.map(({ image: selectedImage }) => ({
    ...selectedImage, url: `https://oss.example/first-${selectedImage.id}`,
    url_expires_at: new Date(now + 65_000).toISOString(),
  })));
  const refreshedGallery = gallery(selected.map(({ image: selectedImage }) => ({
    ...selectedImage, url: `https://oss.example/second-${selectedImage.id}`,
    url_expires_at: new Date(now + 180_000).toISOString(),
  })));
  getCover.mockResolvedValueOnce(initialGallery).mockResolvedValue(refreshedGallery);
  const requests = Array.from({ length: 8 }, () => deferred<Response>());
  fetchImage.mockImplementation(() => requests[fetchImage.mock.calls.length - 1].promise);
  const pending = download(selected);
  await vi.waitFor(() => expect(fetchImage).toHaveBeenCalledTimes(4));
  expect(getCover).toHaveBeenCalledTimes(1);

  vi.setSystemTime(now + 10_000);
  requests[0].resolve(response());
  await vi.waitFor(() => expect(fetchImage).toHaveBeenCalledTimes(5));
  expect(getCover).toHaveBeenCalledTimes(2);
  expect(fetchImage.mock.calls[4][0]).toBe("https://oss.example/second-image-4");
  for (let index = 1; index < requests.length; index += 1) requests[index].resolve(response());
  await expect(pending).resolves.toEqual({ downloaded: 8, failed: [] });
  expect(getCover).toHaveBeenCalledTimes(2);
});

it("缺少到期字段时沿用当前签名；无效到期时间保守刷新", async () => {
  await download([item("no-expiry", { image: image("no-expiry", { url_expires_at: null }) })]);
  expect(getCover).not.toHaveBeenCalled();
  getCover.mockResolvedValue(gallery([image("invalid-expiry", { url: "https://oss.example/refreshed" })]));
  await download([item("invalid-expiry", { image: image("invalid-expiry", { url_expires_at: "not-a-date" }) })]);
  expect(getCover).toHaveBeenCalledTimes(1);
  expect(fetchImage.mock.calls[1][0]).toBe("https://oss.example/refreshed");
});

it("审计开始时只调用一次，按型号归组且完全不含签名 URL", async () => {
  const firstFetch = deferred<Response>();
  fetchImage.mockReturnValue(firstFetch.promise);
  const selected = [item("one"), item("two", { index: 2 }), item("three", { partId: 43 })];
  const pending = download(selected);
  expect(auditGalleryDownload).toHaveBeenCalledExactlyOnceWith([
    { part_id: 42, image_ids: ["one", "two"] }, { part_id: 43, image_ids: ["three"] },
  ], "zip", "gallery-token");
  const auditPayload = JSON.stringify(auditGalleryDownload.mock.calls);
  expect(auditPayload).not.toMatch(/https?:|signature|oss\.example/);
  expect(auditGalleryDownload.mock.invocationCallOrder[0]).toBeLessThan(fetchImage.mock.invocationCallOrder[0]);
  firstFetch.resolve(response());
  await pending;
});

it.each(["single", "zip"] as const)("%s 审计失败不阻断下载", async (kind) => {
  auditGalleryDownload.mockRejectedValue(new Error("audit unavailable"));
  await expect(download([item()], { kind })).resolves.toEqual({ downloaded: 1, failed: [] });
  expect(auditGalleryDownload).toHaveBeenCalledExactlyOnceWith([
    { part_id: 42, image_ids: ["image-1"] },
  ], kind, "gallery-token");
  expect(savedNames).toHaveLength(1);
});

it("审计迟迟未返回也不阻断下载", async () => {
  const audit = deferred<void>();
  auditGalleryDownload.mockReturnValue(audit.promise);
  await expect(download([item()])).resolves.toEqual({ downloaded: 1, failed: [] });
  expect(savedNames).toHaveLength(1);
  audit.resolve();
});

it.each([
  ["超过 500 张", () => Array.from({ length: MAX_GALLERY_IMAGES + 1 }, (_, index) => item(`image-${index}`))],
  ["超过 2 GB", () => [item("large", { image: image("large", { bytes: MAX_GALLERY_BYTES + 1 }) })]],
] as const)("%s 时拒绝打包，不调用 OSS 或审计", async (_description, selected) => {
  await expect(download(selected())).rejects.toThrow("请分批下载");
  expect(fetchImage).not.toHaveBeenCalled();
  expect(auditGalleryDownload).not.toHaveBeenCalled();
  expect(savedNames).toHaveLength(0);
});

it("恰好 500 张且预估大小恰好 2 GB 时允许打包", async () => {
  const selected = Array.from({ length: MAX_GALLERY_IMAGES }, (_, index) => item(`image-${index}`, {
    index: index + 1, image: image(`image-${index}`, { bytes: index ? 0 : MAX_GALLERY_BYTES }),
  }));
  await expect(download(selected)).resolves.toEqual({ downloaded: 500, failed: [] });
  expect(fetchImage).toHaveBeenCalledTimes(500);
  expect(Object.keys(await unpackSavedZip())).toHaveLength(500);
});

it("全部失败时返回完整失败清单，不生成空 ZIP", async () => {
  const selected = [item("bad-1"), item("bad-2", { index: 2 })];
  fetchImage.mockResolvedValue(response("denied", 403));
  getCover.mockResolvedValue(gallery(selected.map(({ image: selectedImage }) => selectedImage)));
  const result = await download(selected);
  expect(result.downloaded).toBe(0);
  expect(result.failed.sort()).toEqual(["PN-42/PN-42_1.jpg", "PN-42/PN-42_2.jpg"]);
  expect(fetchImage).toHaveBeenCalledTimes(4);
  expect(savedNames).toHaveLength(0);
  expect(createObjectURL).not.toHaveBeenCalled();
});

it("预先取消的下载立即终止，不请求或审计", async () => {
  const controller = new AbortController();
  controller.abort();
  await expect(download([item()], { signal: controller.signal })).rejects.toMatchObject({ name: "AbortError" });
  expect(fetchImage).not.toHaveBeenCalled();
  expect(auditGalleryDownload).not.toHaveBeenCalled();
});

it("没有选择或单张模式误传多张时拒绝下载", async () => {
  await expect(download([])).rejects.toThrow("请选择要下载的图片");
  await expect(download([item("one"), item("two")], { kind: "single" })).rejects.toThrow("请选择要下载的图片");
  expect(fetchImage).not.toHaveBeenCalled();
  expect(auditGalleryDownload).not.toHaveBeenCalled();
});
