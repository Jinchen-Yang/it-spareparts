import { fetchPartGallery, type GalleryImage, type GalleryList } from "./gallery";

type CoverImage = GalleryImage & { url_expires_at?: string | null };
export type CoverGallery = Omit<GalleryList, "images"> & {
  main_image_id?: string | null;
  images: CoverImage[];
};

interface CachedCover {
  gallery: CoverGallery;
  expiresAt: number;
}

// token 隔离缓存和进行中的请求，避免换账号后复用前一个账号的签名地址。
const caches = new Map<string, Map<number, CachedCover>>();
const requests = new Map<string, Map<number, Promise<CoverGallery>>>();
const listeners = new Map<number, Set<() => void>>();
const queue: Array<() => void> = [];
const MAX_REQUESTS = 4;
let active = 0;

function drain() {
  while (active < MAX_REQUESTS && queue.length) queue.shift()!();
}

function expiresAt(gallery: CoverGallery): number {
  let expires = Date.now() + 5 * 60_000;
  const images: CoverImage[] = [
    ...gallery.images, ...gallery.candidates,
    ...(gallery.main_image ? [gallery.main_image] : []),
  ];
  for (const image of images) {
    if (!image.url_expires_at) continue;
    const signedExpiry = Date.parse(image.url_expires_at);
    if (Number.isFinite(signedExpiry)) expires = Math.min(expires, signedExpiry - 60_000);
  }
  return expires;
}

export function getCover(partId: number, token: string): Promise<CoverGallery> {
  let cache = caches.get(token);
  if (!cache) {
    cache = new Map();
    caches.set(token, cache);
  }
  const cached = cache.get(partId);
  if (cached && cached.expiresAt > Date.now()) return Promise.resolve(cached.gallery);
  cache.delete(partId);

  let pending = requests.get(token);
  if (!pending) {
    pending = new Map();
    requests.set(token, pending);
  }
  const existing = pending.get(partId);
  if (existing) return existing;

  let resolve!: (gallery: CoverGallery) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<CoverGallery>((done, fail) => { resolve = done; reject = fail; });
  pending.set(partId, promise);
  const finish = () => {
    if (pending.get(partId) === promise) pending.delete(partId);
    active -= 1;
    drain();
  };
  queue.push(() => {
    active += 1;
    void Promise.resolve().then(() => fetchPartGallery(partId, token)).then((gallery) => {
      try {
        if (!gallery || !Array.isArray(gallery.images)) throw new Error("图库响应无效");
        // invalidate 后的旧请求仍可结束，但不能重新填回已经失效的缓存。
        if (pending.get(partId) === promise) {
          cache.set(partId, { gallery, expiresAt: expiresAt(gallery) });
        }
        resolve(gallery);
      } catch (error: unknown) {
        reject(error);
      } finally {
        finish();
      }
    }, (error: unknown) => {
      reject(error);
      finish();
    });
  });
  drain();
  return promise;
}

export function invalidateCover(partId: number): void {
  for (const cache of caches.values()) cache.delete(partId);
  for (const pending of requests.values()) pending.delete(partId);
  listeners.get(partId)?.forEach((listener) => listener());
}

/** 让已挂载的列表缩略图在编辑抽屉关闭后同步刷新。 */
export function subscribeCover(partId: number, listener: () => void): () => void {
  let subscribed = listeners.get(partId);
  if (!subscribed) {
    subscribed = new Set();
    listeners.set(partId, subscribed);
  }
  subscribed.add(listener);
  return () => {
    subscribed.delete(listener);
    if (!subscribed.size) listeners.delete(partId);
  };
}
