import { Zip, ZipPassThrough } from "fflate";
import { auditGalleryDownload, type GalleryImage } from "../../api/gallery";
import { getCover, invalidateCover, type CoverGallery } from "../../api/galleryCovers";

export const MAX_GALLERY_IMAGES = 500;
export const MAX_GALLERY_BYTES = 2 * 1024 ** 3;
const MAX_FETCHES = 4;

export interface GalleryDownloadItem {
  partId: number;
  pn: string;
  image: GalleryImage;
  index: number;
}

interface DownloadOptions {
  items: GalleryDownloadItem[];
  kind: "single" | "zip";
  token: string;
  signal: AbortSignal;
  onProgress?: (completed: number, total: number) => void;
}

function safePn(pn: string): string {
  const name = pn.replace(/[<>:"/\\|?*\u0000-\u001f\u007f]/g, "_").replace(/[. ]+$/g, (end) => "_".repeat(end.length));
  if (!name) return "图片";
  return /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name) ? `_${name}` : name;
}

export function galleryFilename(pn: string, index: number, image: GalleryImage): string {
  const extensions: Record<string, string> = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
    "image/avif": "avif", "image/heic": "heic", "image/heif": "heif", "image/tiff": "tiff", "image/bmp": "bmp",
  };
  const sourceExtension = image.filename.split(".").pop()?.toLowerCase();
  const extension = extensions[image.content_type.toLowerCase()]
    || (sourceExtension && /^(jpe?g|png|webp|gif|avif|heic|heif|tiff?|bmp)$/.test(sourceExtension) ? sourceExtension : "bin");
  return `${safePn(pn)}_${index}.${extension}`;
}

export function galleryZipFilename(date = new Date()): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `图片_${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}.zip`;
}

function checkAbort(signal: AbortSignal): void {
  if (signal.aborted) throw new DOMException("下载已取消", "AbortError");
}

function abortable<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  checkAbort(signal);
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(new DOMException("下载已取消", "AbortError"));
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", onAbort));
  });
}

function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  try { link.click(); } finally {
    link.remove();
    // 留出浏览器接管下载的时间；无论组件是否卸载都回收地址。
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

export async function saveGalleryDownload(options: DownloadOptions): Promise<{ downloaded: number; failed: string[] }> {
  checkAbort(options.signal);
  const controller = new AbortController();
  const cancel = () => controller.abort();
  options.signal.addEventListener("abort", cancel, { once: true });
  try {
    return await runGalleryDownload({ ...options, signal: controller.signal });
  } catch (error) {
    // 组装或保存失败也停止其它在途图片，避免退出后继续占用网络和内存。
    controller.abort();
    throw error;
  } finally {
    options.signal.removeEventListener("abort", cancel);
  }
}

async function runGalleryDownload({ items, kind, token, signal, onProgress }: DownloadOptions): Promise<{ downloaded: number; failed: string[] }> {
  checkAbort(signal);
  if (!items.length || (kind === "single" && items.length !== 1)) throw new Error("请选择要下载的图片");
  if (kind === "zip" && (items.length > MAX_GALLERY_IMAGES || items.reduce((sum, item) => sum + item.image.bytes, 0) > MAX_GALLERY_BYTES)) {
    throw new Error("请分批下载");
  }
  const audit = new Map<number, string[]>();
  for (const item of items) audit.set(item.partId, [...(audit.get(item.partId) || []), item.image.id]);
  // 审计独立于下载，失败也不能阻断浏览器向 OSS 取图。
  void auditGalleryDownload(Array.from(audit, ([part_id, image_ids]) => ({ part_id, image_ids })), kind, token).catch(() => {});

  const initialRefreshes = new Map<number, Promise<CoverGallery>>();
  const retryRefreshes = new Map<number, Promise<CoverGallery>>();
  const latestGalleries = new Map<number, CoverGallery>();
  const refresh = async (item: GalleryDownloadItem, refreshes: Map<number, Promise<CoverGallery>>) => {
    checkAbort(signal);
    let pending = refreshes.get(item.partId);
    if (!pending) {
      invalidateCover(item.partId);
      pending = getCover(item.partId, token);
      refreshes.set(item.partId, pending);
      const clear = () => {
        if (refreshes.get(item.partId) === pending) refreshes.delete(item.partId);
      };
      // 只合并在途请求；长任务下一批失败时必须能重新签名。
      void pending.then(clear, clear);
    }
    const gallery = await abortable(pending, signal);
    latestGalleries.set(item.partId, gallery);
    const image = gallery.images.find((candidate) => candidate.id === item.image.id);
    if (!image) throw new Error("图片已不在图库中");
    return image;
  };
  const fetchImage = async (item: GalleryDownloadItem): Promise<Blob> => {
    let image = latestGalleries.get(item.partId)?.images.find((candidate) => candidate.id === item.image.id) || item.image;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        checkAbort(signal);
        if (attempt) image = await refresh(item, retryRefreshes);
        else if (image.url_expires_at && (!Number.isFinite(Date.parse(image.url_expires_at)) || Date.parse(image.url_expires_at) - Date.now() < 60_000)) {
          image = await refresh(item, initialRefreshes);
        }
        checkAbort(signal);
        const response = await fetch(image.url, { signal, credentials: "omit", referrerPolicy: "no-referrer" });
        if (!response.ok) throw new Error(`图片下载失败（${response.status}）`);
        const blob = await response.blob();
        checkAbort(signal);
        return blob;
      } catch (error) {
        checkAbort(signal);
        if (attempt) throw error;
      }
    }
    throw new Error("图片下载失败");
  };

  const failed: string[] = [];
  let downloaded = 0;
  let completed = 0;
  let next = 0;
  let zipError: Error | null = null;
  const chunks: BlobPart[] = [];
  const zip = kind === "zip" ? new Zip((error, data) => {
    if (error) zipError = error;
    else chunks.push(new Uint8Array(data));
  }) : null;
  // 不同 PN 清洗后可能同名，用型号 ID 消除目录冲突，避免 ZIP 解压覆盖。
  const directories = new Map<number, string>();
  const usedDirectories = new Set<string>();
  for (const item of items) {
    if (directories.has(item.partId)) continue;
    let directory = safePn(item.pn);
    while (usedDirectories.has(directory.toLowerCase())) directory += `_${item.partId}`;
    usedDirectories.add(directory.toLowerCase());
    directories.set(item.partId, directory);
  }
  onProgress?.(0, items.length);
  const worker = async () => {
    while (next < items.length) {
      checkAbort(signal);
      const item = items[next++];
      const filename = galleryFilename(item.pn, item.index, item.image);
      let blob: Blob;
      try { blob = await fetchImage(item); } catch {
        checkAbort(signal);
        failed.push(`${directories.get(item.partId)}/${filename}`);
        completed += 1;
        onProgress?.(completed, items.length);
        continue;
      }
      checkAbort(signal);
      if (zip) {
        // 每张取完才加入 ZIP，失败重试不会留下损坏的半张图片；最多暂存四张。
        const entry = new ZipPassThrough(`${directories.get(item.partId)}/${filename}`);
        zip.add(entry);
        for (let offset = 0; offset < blob.size; offset += 64 * 1024) {
          const data = new Uint8Array(await blob.slice(offset, offset + 64 * 1024).arrayBuffer());
          checkAbort(signal);
          entry.push(data);
        }
        entry.push(new Uint8Array(), true);
        if (zipError) throw zipError;
      } else saveBlob(blob, filename);
      downloaded += 1;
      completed += 1;
      onProgress?.(completed, items.length);
    }
  };
  const terminate = () => { zip?.terminate(); chunks.length = 0; };
  signal.addEventListener("abort", terminate, { once: true });
  try {
    await Promise.all(Array.from({ length: Math.min(MAX_FETCHES, items.length) }, worker));
    checkAbort(signal);
    if (zip && downloaded) {
      zip.end();
      if (zipError) throw zipError;
      saveBlob(new Blob(chunks, { type: "application/zip" }), galleryZipFilename());
    }
    return { downloaded, failed };
  } finally {
    signal.removeEventListener("abort", terminate);
    terminate();
  }
}
