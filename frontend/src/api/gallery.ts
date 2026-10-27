import api from "../api";

/** shop 图库图片（itsp 只做代理，不落地图片字节）。 */
export interface GalleryImage {
  file_no: number;
  filename: string;
  id: string;
  pn_id: string;
  url: string;
  url_expires_at?: string | null;
  thumb_url: string | null;
  width: number;
  height: number;
  bytes: number;
  content_type: string;
  source: "phone" | "upload" | "crawl" | "legacy";
  status: "gallery" | "candidate" | "deleted";
  sort_order: number;
  is_defect: boolean;
  has_foreign_watermark: boolean;
  thumbnail_status: "pending" | "ready" | "failed";
  created_at: string;
}

export interface GalleryList {
  part_id: number;
  pn_id: string;
  pn_display: string;
  gallery_revision: number;
  has_image: boolean;
  main_image: GalleryImage | null;
  images: GalleryImage[];
  candidates: GalleryImage[];
}

export interface GalleryUploadTicket {
  upload_id: string;
  method: "POST";
  url: string;
  fields: Record<string, string>;
  headers: Record<string, string>;
  expires_at: string;
  max_bytes: number;
}

export interface GalleryRegistration {
  image: GalleryImage;
  gallery_revision: number;
  duplicate: boolean;
  restored: boolean;
}

export interface GalleryMutation {
  image: GalleryImage;
  gallery_revision: number;
}

export interface GalleryUpdate {
  baseline_gallery_revision: number;
  sort_order?: number;
  is_defect?: boolean;
  status?: "gallery";
}

const auth = (token: string) => ({ Authorization: `Bearer ${token}` });

/** 每次操作一个幂等号；同一次失败重试必须复用，不能重新生成。 */
export function newGalleryRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (char) => {
    const value = Math.floor(Math.random() * 16);
    return (char === "x" ? value : (value & 0x3) | 0x8).toString(16);
  });
}

export async function fetchPartGallery(partId: number, token: string): Promise<GalleryList> {
  const { data } = await api.get<GalleryList>(`/parts/${partId}/gallery`, {
    headers: auth(token),
  });
  return data;
}

export async function auditGalleryDownload(
  items: { part_id: number; image_ids: string[] }[],
  kind: "single" | "zip",
  token: string,
): Promise<void> {
  // 审计失败不能触发 axios 的全局 401 reload，避免中断已开始的 OSS 下载。
  const response = await fetch("/api/parts/gallery/download-audit", {
    method: "POST",
    headers: { ...auth(token), "Content-Type": "application/json" },
    body: JSON.stringify({ items, kind }),
  });
  if (!response.ok) throw new Error(`下载审计失败（${response.status}）`);
}

export async function signGalleryUpload(
  partId: number, file: File, token: string, requestId: string,
): Promise<GalleryUploadTicket> {
  const { data } = await api.post<GalleryUploadTicket>(
    `/parts/${partId}/gallery/uploads/sign`,
    { filename: file.name, bytes: file.size, content_type: file.type },
    { headers: { ...auth(token), "X-Request-Id": requestId } },
  );
  return data;
}

export async function registerGalleryImage(
  partId: number, uploadId: string, token: string, requestId: string,
): Promise<GalleryRegistration> {
  const { data } = await api.post<GalleryRegistration>(
    `/parts/${partId}/gallery/images`,
    { upload_id: uploadId, source: "upload", is_defect: false },
    { headers: { ...auth(token), "X-Request-Id": requestId } },
  );
  return data;
}

export async function updateGalleryImage(
  partId: number, imageId: string, update: GalleryUpdate, token: string, requestId: string,
): Promise<GalleryMutation> {
  const { data } = await api.patch<GalleryMutation>(
    `/parts/${partId}/gallery/images/${imageId}`,
    update,
    { headers: { ...auth(token), "X-Request-Id": requestId } },
  );
  return data;
}

export async function deleteGalleryImage(
  partId: number, imageId: string, revision: number, token: string, requestId: string,
): Promise<void> {
  await api.delete(`/parts/${partId}/gallery/images/${imageId}`, {
    headers: { ...auth(token), "X-Request-Id": requestId },
    params: { baseline_gallery_revision: revision },
  });
}

/**
 * 浏览器直传 OSS：只使用 shop 签发的字段，不携带 itsp 登录态。
 * XHR 而不是 fetch，因为需要每张图片的原生上传进度。
 */
export function uploadGalleryFileToOss(
  file: File,
  ticket: GalleryUploadTicket,
  onProgress: (percent: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (file.size > ticket.max_bytes) {
      reject(new Error("图片超过本次上传允许的大小，请重新选择"));
      return;
    }
    const xhr = new XMLHttpRequest();
    let settled = false;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      if (error) reject(error); else resolve();
    };
    xhr.open(ticket.method, ticket.url);
    xhr.withCredentials = false;
    xhr.timeout = 120_000;
    Object.entries(ticket.headers).forEach(([key, value]) => xhr.setRequestHeader(key, value));
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(100);
        finish();
      } else {
        finish(new Error("图片直传失败，请重试"));
      }
    };
    xhr.onerror = () => finish(new Error("图片直传失败，请检查网络后重试"));
    xhr.ontimeout = () => finish(new Error("图片上传超时，请检查网络后重试"));
    xhr.onabort = () => finish(new Error("图片上传已取消"));
    const form = new FormData();
    Object.entries(ticket.fields).forEach(([key, value]) => form.append(key, value));
    form.append("file", file);
    xhr.send(form);
  });
}
