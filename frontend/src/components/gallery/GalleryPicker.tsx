import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, App, Button, Checkbox, Empty, Image, Modal, Progress, Space, Spin } from "antd";
import { getCover, invalidateCover, type CoverGallery } from "../../api/galleryCovers";
import {
  MAX_GALLERY_BYTES, MAX_GALLERY_IMAGES, saveGalleryDownload,
  type GalleryDownloadItem,
} from "./galleryDownloads";

interface GalleryPart {
  partId: number;
  pn: string;
}

interface Props {
  partIds?: number[];
  items?: GalleryPart[];
  mode?: "inline" | "modal";
  onClose?: () => void;
}

interface Group extends GalleryPart {
  status: "loading" | "ready" | "error";
  gallery?: CoverGallery;
}

const imageKey = (item: GalleryDownloadItem) => `${item.partId}:${item.image.id}`;

function sizeLabel(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function groupImages(group: Group): GalleryDownloadItem[] {
  return [...(group.gallery?.images || [])]
    .sort((left, right) => left.sort_order - right.sort_order)
    .map((image, index) => ({ partId: group.partId, pn: group.pn, image, index: index + 1 }));
}

/** 通用图库选择器：图片下载和 ZIP 组装均在浏览器完成。 */
export default function GalleryPicker({ partIds, items, mode = "inline", onClose }: Props) {
  const { message } = App.useApp();
  const token = localStorage.getItem("token");
  // 调用方可直接传数组字面量；仅内容变化才重置选择与进行中的下载。
  const partsKey = JSON.stringify(items ?? (partIds || []).map((partId) => ({ partId, pn: "" })));
  const parts = useMemo<GalleryPart[]>(() => {
    const seen = new Set<number>();
    return (JSON.parse(partsKey) as GalleryPart[]).filter(({ partId }) => {
      if (!Number.isInteger(partId) || partId <= 0 || seen.has(partId)) return false;
      seen.add(partId);
      return true;
    });
  }, [partsKey]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const anchor = useRef<string | null>(null);
  const generation = useRef(0);
  const previewGeneration = useRef(0);
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(true);
  const [downloading, setDownloading] = useState(false);
  const [progress, setProgress] = useState({ completed: 0, total: 0 });
  const [failed, setFailed] = useState<string[]>([]);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [cancelled, setCancelled] = useState(false);
  const download = useRef<AbortController | null>(null);

  useEffect(() => {
    const current = ++generation.current;
    previewGeneration.current += 1;
    download.current?.abort();
    download.current = null;
    setGroups(parts.map((part) => ({ ...part, status: token ? "loading" : "error" })));
    setSelected(new Set());
    anchor.current = null;
    setPreviewKey(null);
    setDownloading(false);
    setProgress({ completed: 0, total: 0 });
    setFailed([]);
    setDownloadError(null);
    setCancelled(false);
    if (token) {
      for (const part of parts) {
        void getCover(part.partId, token).then((gallery) => {
          if (!Array.isArray(gallery.images)) throw new Error("图库响应无效");
          if (generation.current !== current) return;
          setGroups((previous) => previous.map((group) => group.partId === part.partId
            ? { ...group, pn: part.pn || gallery.pn_display, gallery, status: "ready" } : group));
        }).catch(() => {
          if (generation.current !== current) return;
          setGroups((previous) => previous.map((group) => group.partId === part.partId
            ? { ...group, status: "error" } : group));
        });
      }
    }
    return () => {
      generation.current += 1;
      previewGeneration.current += 1;
      download.current?.abort();
      download.current = null;
    };
  }, [parts, token]);

  const allImages = groups.flatMap(groupImages);
  const chosen = allImages.filter((item) => selected.has(imageKey(item)));
  const totalBytes = chosen.reduce((total, item) => total + Math.max(0, item.image.bytes || 0), 0);
  const overLimit = chosen.length > MAX_GALLERY_IMAGES || totalBytes > MAX_GALLERY_BYTES;

  const selectImage = (item: GalleryDownloadItem, checked: boolean, shiftKey: boolean) => {
    const key = imageKey(item);
    const start = allImages.findIndex((image) => imageKey(image) === anchor.current);
    const end = allImages.findIndex((image) => imageKey(image) === key);
    const range = shiftKey && start >= 0
      ? allImages.slice(Math.min(start, end), Math.max(start, end) + 1) : [item];
    setSelected((previous) => {
      const next = new Set(previous);
      for (const image of range) {
        if (checked) next.add(imageKey(image)); else next.delete(imageKey(image));
      }
      return next;
    });
    anchor.current = key;
  };

  const retry = async (partId: number) => {
    if (!token) return;
    const current = generation.current;
    invalidateCover(partId);
    setGroups((previous) => previous.map((group) => group.partId === partId
      ? { ...group, status: "loading" } : group));
    try {
      const gallery = await getCover(partId, token);
      if (!Array.isArray(gallery.images)) throw new Error("图库响应无效");
      if (generation.current !== current) return;
      setGroups((previous) => previous.map((group) => group.partId === partId
        ? { ...group, pn: group.pn || gallery.pn_display, gallery, status: "ready" } : group));
    } catch {
      if (generation.current === current) setGroups((previous) => previous.map((group) => group.partId === partId
        ? { ...group, status: "error" } : group));
    }
  };

  const openPreview = async (item: GalleryDownloadItem) => {
    const current = generation.current;
    const request = ++previewGeneration.current;
    if (token) {
      try {
        // 与 PartThumb 一样，打开前经过签名缓存，过期时复用列表接口刷新。
        const gallery = await getCover(item.partId, token);
        if (generation.current !== current || previewGeneration.current !== request) return;
        if (Array.isArray(gallery.images)) {
          if (!gallery.images.some((image) => image.id === item.image.id)) {
            message.info("该图片已不在图库中，请重新选择");
            return;
          }
          setGroups((previous) => previous.map((group) => group.partId === item.partId
            ? { ...group, gallery } : group));
        }
      } catch {
        // 刷新失败仍可尝试已有地址，保持与 PartThumb 的预览行为一致。
      }
    }
    if (generation.current === current && previewGeneration.current === request) setPreviewKey(imageKey(item));
  };

  const startDownload = async (images: GalleryDownloadItem[], kind: "single" | "zip") => {
    if (!token || download.current || !images.length || (kind === "zip" && overLimit)) return;
    const controller = new AbortController();
    download.current = controller;
    const current = generation.current;
    const isCurrent = () => generation.current === current && download.current === controller;
    setDownloading(true);
    setProgress({ completed: 0, total: images.length });
    setFailed([]);
    setDownloadError(null);
    setCancelled(false);
    try {
      const result = await saveGalleryDownload({
        items: images, kind, token, signal: controller.signal,
        onProgress: (completed, total) => { if (isCurrent()) setProgress({ completed, total }); },
      });
      if (!isCurrent()) return;
      setFailed(result.failed);
      if (result.downloaded > 0) message.success(`已下载 ${result.downloaded} 张图片`);
    } catch (error: unknown) {
      if (!isCurrent()) return;
      if (controller.signal.aborted || (error instanceof Error && error.name === "AbortError")) setCancelled(true);
      else setDownloadError(error instanceof Error ? error.message : "下载失败，请稍后重试");
    } finally {
      if (isCurrent()) {
        download.current = null;
        setDownloading(false);
      }
    }
  };

  const close = () => {
    generation.current += 1;
    previewGeneration.current += 1;
    download.current?.abort();
    download.current = null;
    setDownloading(false);
    setPreviewKey(null);
    setModalOpen(false);
    onClose?.();
  };

  const content = (
    <div aria-label="图库多选下载">
      <Space wrap style={{ marginBottom: 12 }}>
        <Button disabled={downloading || !allImages.length} onClick={() => {
          setSelected(new Set(allImages.map(imageKey)));
          anchor.current = null;
        }}>全选</Button>
        <Button disabled={downloading || !chosen.length} onClick={() => {
          setSelected(new Set());
          anchor.current = null;
        }}>清空</Button>
        <span role="status">已选 {chosen.length} 张 · 预估 {sizeLabel(totalBytes)}</span>
        <Button type="primary" disabled={downloading || !chosen.length || overLimit || !token}
          onClick={() => void startDownload(chosen, "zip")}>打包下载</Button>
      </Space>
      {overLimit && <Alert type="warning" showIcon message="单次最多 500 张或 2 GB，请分批下载" style={{ marginBottom: 12 }} />}
      {downloading && <div style={{ marginBottom: 12 }}>
        <div role="status">下载进度：{progress.completed} / {progress.total}</div>
        <Progress percent={progress.total ? Math.round(progress.completed / progress.total * 100) : 0} />
        <Button onClick={() => download.current?.abort()}>取消下载</Button>
      </div>}
      {cancelled && <Alert type="info" message="已取消下载" style={{ marginBottom: 12 }} />}
      {downloadError && <Alert type="error" showIcon message={downloadError} style={{ marginBottom: 12 }} />}
      {failed.length > 0 && <Alert type="warning" showIcon message={`以下 ${failed.length} 张图片下载失败，已跳过`}
        description={<ul>{failed.map((filename, index) => <li key={`${filename}:${index}`}>{filename}</li>)}</ul>}
        style={{ marginBottom: 12 }} />}
      {!parts.length && <Empty description="请选择型号" />}
      {groups.map((group) => {
        const images = groupImages(group);
        const selectedCount = images.filter((item) => selected.has(imageKey(item))).length;
        return <section key={group.partId} aria-label={`${group.pn || `型号 ${group.partId}`} 图库`} style={{ marginBottom: 20 }}>
          <Space wrap style={{ marginBottom: 8 }}>
            <strong>{group.pn || `型号 ${group.partId}`}</strong>
            <span>{images.length} 张</span>
            <Checkbox disabled={downloading || !images.length} checked={images.length > 0 && selectedCount === images.length}
              indeterminate={selectedCount > 0 && selectedCount < images.length}
              aria-label={`全选本型号 ${group.pn || group.partId}`}
              onChange={(event) => {
                setSelected((previous) => {
                  const next = new Set(previous);
                  images.forEach((item) => event.target.checked ? next.add(imageKey(item)) : next.delete(imageKey(item)));
                  return next;
                });
                anchor.current = null;
              }}>全选本型号</Checkbox>
          </Space>
          {group.status === "loading" && <div role="status"><Spin size="small" /> 图库加载中</div>}
          {group.status === "error" && <Alert type="error" showIcon
            message={token ? "图库加载失败，请重试" : "登录状态已失效，请重新登录"}
            action={token && <Button size="small" onClick={() => void retry(group.partId)}>重试</Button>} />}
          {group.status === "ready" && !images.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无图片" />}
          {group.status === "ready" && <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))", gap: 12 }}>
            {images.map((item) => <div key={item.image.id} style={{ border: "1px solid var(--mb-border-soft)", borderRadius: 6, padding: 8 }}>
              <button type="button" aria-label={`预览 ${group.pn} 第 ${item.index} 张`}
                style={{ width: "100%", height: 112, display: "block", padding: 0, border: 0, cursor: "pointer", background: "var(--mb-inset)" }}
                onClick={() => void openPreview(item)}>
                <img src={item.image.thumb_url || item.image.url} alt={`${group.pn} 第 ${item.index} 张`}
                  style={{ width: "100%", height: "100%", objectFit: "contain" }} />
              </button>
              <Space style={{ marginTop: 8, width: "100%", justifyContent: "space-between" }}>
                <Checkbox checked={selected.has(imageKey(item))} disabled={downloading}
                  aria-label={`选择 ${group.pn} 第 ${item.index} 张`}
                  onChange={(event) => selectImage(item, event.target.checked, (event.nativeEvent as MouseEvent).shiftKey)}>
                  第 {item.index} 张
                </Checkbox>
                <Button size="small" type="link" disabled={downloading || !token} aria-label={`下载 ${group.pn} 第 ${item.index} 张`}
                  onClick={() => void startDownload([item], "single")}>下载</Button>
              </Space>
            </div>)}
          </div>}
        </section>;
      })}
      {allImages.length > 0 && <Image.PreviewGroup
        items={allImages.map((item) => ({ src: item.image.url, alt: `${item.pn} 第 ${item.index} 张` }))}
        preview={{
          visible: previewKey !== null,
          current: Math.max(0, allImages.findIndex((item) => imageKey(item) === previewKey)),
          onVisibleChange: (visible) => { if (!visible) { previewGeneration.current += 1; setPreviewKey(null); } },
          onChange: (index) => { if (allImages[index]) void openPreview(allImages[index]); },
        }}
      />}
    </div>
  );

  return mode === "modal"
    ? <Modal open={modalOpen} title="图库多选下载" width={960} footer={null} onCancel={close}>{content}</Modal>
    : content;
}
