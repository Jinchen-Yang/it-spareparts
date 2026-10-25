import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Alert, App, Button, Empty, Progress, Space, Spin, Tag, Tooltip } from "antd";
import {
  deleteGalleryImage, fetchPartGallery, newGalleryRequestId, registerGalleryImage,
  signGalleryUpload, updateGalleryImage, uploadGalleryFileToOss,
  type GalleryImage, type GalleryList, type GalleryUpdate, type GalleryUploadTicket,
} from "../../api/gallery";

const MAX_FILES = 10;
const ACCEPT = "image/jpeg,image/png,image/webp";

interface Props {
  partId: number;
  pn: string;
  /** 与后端 page_master_data 同一权限；false = 只读。 */
  canWrite: boolean;
  onChanged?: () => void;
}

type UploadStatus = "signing" | "uploading" | "registering" | "done" | "failed";

interface UploadItem {
  key: string;
  file: File;
  progress: number;
  status: UploadStatus;
  error?: string;
  /** 签名/登记各一个幂等号：重试必须复用，不能重新生成。 */
  signRequestId: string;
  registerRequestId: string;
  ticket?: GalleryUploadTicket;
  uploaded: boolean;
}

function errorMessage(error: any, fallback: string): string {
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (typeof detail?.message === "string") return detail.message;
  if (!error?.response) return error?.message || "无法连接服务器，请稍后重试";
  return fallback;
}

export default function PartGalleryPanel({ partId, pn, canWrite, onChanged }: Props) {
  const { message, modal } = App.useApp();
  const [gallery, setGallery] = useState<GalleryList | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyImage, setBusyImage] = useState<string | null>(null);
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState<string | null>(null);
  const token = useRef(localStorage.getItem("token"));
  const mounted = useRef(true);
  const loadSeq = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);

  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      ++loadSeq.current;
    };
  }, [partId]);

  const load = async () => {
    const authToken = token.current;
    if (!authToken) {
      setLoadError("登录状态已失效，请重新登录");
      return;
    }
    const seq = ++loadSeq.current;
    setLoading(true);
    setLoadError(null);
    try {
      const data = await fetchPartGallery(partId, authToken);
      if (!mounted.current || seq !== loadSeq.current) return;
      setGallery(data);
    } catch (error: any) {
      if (mounted.current && seq === loadSeq.current) {
        setLoadError(errorMessage(error, "图库加载失败，请稍后重试"));
      }
    } finally {
      if (mounted.current && seq === loadSeq.current) setLoading(false);
    }
  };

  useEffect(() => {
    setGallery(null);
    setUploads([]);
    void load();
    // partId 变化必须重新取图库；load 只使用当前 partId。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [partId]);

  const patchUpload = (key: string, patch: Partial<UploadItem>) => {
    setUploads((current) => current.map((item) => (
      item.key === key ? { ...item, ...patch } : item
    )));
  };

  const handleWriteError = (error: any, fallback: string): boolean => {
    if (error?.response?.status === 409) {
      message.warning("图库已被他人修改");
      void load();
      return true;
    }
    message.error(errorMessage(error, fallback));
    return false;
  };

  const runUpload = async (item: UploadItem) => {
    const authToken = token.current;
    if (!authToken) return;
    patchUpload(item.key, { status: item.ticket ? (item.uploaded ? "registering" : "uploading") : "signing", error: undefined });
    try {
      let ticket = item.ticket;
      if (!ticket) {
        ticket = await signGalleryUpload(partId, item.file, authToken, item.signRequestId);
        patchUpload(item.key, { ticket, status: "uploading" });
      }
      if (!item.uploaded) {
        await uploadGalleryFileToOss(item.file, ticket, (percent) => {
          patchUpload(item.key, { progress: percent });
        });
        patchUpload(item.key, { uploaded: true, progress: 100, status: "registering" });
      }
      const result = await registerGalleryImage(
        partId, ticket.upload_id, authToken, item.registerRequestId,
      );
      onChanged?.();
      if (!mounted.current) return;
      patchUpload(item.key, { status: "done", progress: 100 });
      if (result.duplicate || result.restored) message.info("图片已存在，已复用商城图库记录");
      else message.success("图片已上传到图库");
      await load();
    } catch (error: any) {
      if (!mounted.current) return;
      const conflict = error?.response?.status === 409;
      patchUpload(item.key, {
        status: "failed",
        error: errorMessage(error, conflict ? "图库已被他人修改" : "上传失败，请重试"),
      });
      if (conflict) {
        message.warning("图库已被他人修改");
        void load();
      }
    }
  };

  const chooseFiles = (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length) return;
    if (files.length > MAX_FILES) {
      message.warning(`单次最多上传 ${MAX_FILES} 张图片`);
      return;
    }
    const accepted = files.filter((file) => ACCEPT.split(",").includes(file.type));
    if (accepted.length !== files.length) {
      message.warning("仅支持 JPEG / PNG / WebP 图片");
    }
    if (!accepted.length) return;
    const stamp = Date.now();
    const items: UploadItem[] = accepted.map((file, index) => ({
      key: `${stamp}-${index}-${file.name}`,
      file,
      progress: 0,
      status: "signing",
      signRequestId: newGalleryRequestId(),
      registerRequestId: newGalleryRequestId(),
      uploaded: false,
    }));
    setUploads((current) => [...items, ...current]);
    items.forEach((item) => { void runUpload(item); });
  };

  const mutateImage = async (
    image: GalleryImage, update: Omit<GalleryUpdate, "baseline_gallery_revision">,
    successText: string,
  ) => {
    const authToken = token.current;
    if (!canWrite || !authToken || !gallery || busyImage) return;
    setBusyImage(image.id);
    try {
      await updateGalleryImage(partId, image.id, {
        ...update, baseline_gallery_revision: gallery.gallery_revision,
      }, authToken, newGalleryRequestId());
      onChanged?.();
      if (!mounted.current) return;
      message.success(successText);
      await load();
    } catch (error: any) {
      if (mounted.current) handleWriteError(error, "图库修改失败，请重试");
    } finally {
      if (mounted.current) setBusyImage(null);
    }
  };

  const moveImage = (image: GalleryImage, target: number) => {
    if (!gallery) return;
    if (target < 0 || target >= gallery.images.length) return;
    const from = gallery.images.findIndex((item) => item.id === image.id);
    if (from < 0 || from === target) return;
    void mutateImage(image, { sort_order: target }, "排序已更新");
  };

  const confirmDelete = (image: GalleryImage) => {
    const authToken = token.current;
    const revision = gallery?.gallery_revision;
    if (!canWrite || !authToken || revision === undefined) return;
    modal.confirm({
      title: "删除图片",
      content: `确定删除「${image.filename}」？删除后图库顺序会重新排列。`,
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        setBusyImage(image.id);
        try {
          await deleteGalleryImage(partId, image.id, revision, authToken, newGalleryRequestId());
          onChanged?.();
          if (!mounted.current) return;
          message.success("图片已删除");
          await load();
        } catch (error: any) {
          if (mounted.current) handleWriteError(error, "删除失败，请重试");
        } finally {
          if (mounted.current) setBusyImage(null);
        }
      },
    });
  };

  const writable = canWrite;
  const busy = busyImage !== null;
  const images = gallery?.images || [];
  const mainId = gallery?.main_image?.id;

  return (
    <section aria-label="型号图库" className="part-section">
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                    gap: 12, flexWrap: "wrap", marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>图库</h3>
        {writable ? (
          <Space size={8}>
            <input
              ref={fileInput}
              type="file"
              accept={ACCEPT}
              multiple
              aria-label="选择图库图片"
              style={{ display: "none" }}
              onChange={chooseFiles}
            />
            <Button onClick={() => fileInput.current?.click()}>上传图片</Button>
          </Space>
        ) : (
          <span style={{ color: "var(--mb-text-3)", fontSize: 12 }}>
            只读：需要「备件主数据」权限才能维护图库
          </span>
        )}
      </div>
      <div style={{ color: "var(--mb-text-3)", fontSize: 12, marginBottom: 12 }}>
        {pn} · 图片由商城图库统一保存；支持 JPEG/PNG/WebP，单张不超过 20MB，单次最多 {MAX_FILES} 张。
      </div>

      {loadError && (
        <Alert
          type="error" showIcon message={loadError} style={{ marginBottom: 12 }}
          action={writable || !gallery ? <Button size="small" onClick={() => void load()}>重试</Button> : undefined}
        />
      )}

      <Spin spinning={loading}>
        {images.length === 0 && !loading ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={writable ? "暂无图片，可上传" : "暂无图片"}
          />
        ) : (
          <div
            aria-label="图库缩略图"
            style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(168px, 1fr))", gap: 12 }}
          >
            {images.map((image, index) => {
              const isMain = image.id === mainId;
              return (
                <div
                  key={image.id}
                  draggable={writable && !busy}
                  onDragStart={(event) => {
                    setDragging(image.id);
                    event.dataTransfer.effectAllowed = "move";
                  }}
                  onDragOver={(event) => {
                    if (writable && !busy) event.preventDefault();
                  }}
                  onDrop={(event) => {
                    event.preventDefault();
                    if (!dragging || dragging === image.id) return;
                    const source = images.findIndex((item) => item.id === dragging);
                    setDragging(null);
                    if (source >= 0) moveImage(images[source], index);
                  }}
                  onDragEnd={() => setDragging(null)}
                  style={{
                    border: "1px solid var(--mb-border-soft)", borderRadius: 8, overflow: "hidden",
                    background: "var(--mb-surface)", opacity: dragging === image.id ? 0.6 : 1,
                  }}
                >
                  <div style={{ position: "relative", background: "var(--mb-inset)", aspectRatio: "4 / 3" }}>
                    <img
                      src={image.thumb_url || image.url}
                      alt={image.filename}
                      draggable={false}
                      style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
                    />
                    <Space size={4} style={{ position: "absolute", top: 6, left: 6 }}>
                      {isMain && <Tag color="blue" style={{ marginInlineEnd: 0 }}>主图</Tag>}
                      {image.is_defect && <Tag color="red" style={{ marginInlineEnd: 0 }}>瑕疵</Tag>}
                    </Space>
                  </div>
                  <div style={{ padding: "8px 10px" }}>
                    <Tooltip title={image.filename}>
                      <div style={{
                        fontSize: 12, overflow: "hidden", textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}>
                        {image.filename}
                      </div>
                    </Tooltip>
                    {writable && (
                      <Space size={2} wrap style={{ marginTop: 6 }}>
                        <Button
                          size="small" disabled={busy || index === 0}
                          aria-label={`上移 ${image.filename}`}
                          onClick={() => moveImage(image, index - 1)}
                        >上移</Button>
                        <Button
                          size="small" disabled={busy || index === images.length - 1}
                          aria-label={`下移 ${image.filename}`}
                          onClick={() => moveImage(image, index + 1)}
                        >下移</Button>
                        <Button
                          size="small" disabled={busy || image.is_defect || isMain}
                          aria-label={`设为 ${image.filename} 的主图`}
                          onClick={() => mutateImage(image, { sort_order: 0 }, "已设为主图")}
                        >设为主图</Button>
                        <Button
                          size="small" disabled={busy}
                          aria-label={`${image.is_defect ? "取消" : "标记"} ${image.filename} 瑕疵`}
                          onClick={() => mutateImage(
                            image, { is_defect: !image.is_defect },
                            image.is_defect ? "已取消瑕疵标记" : "已标记为瑕疵",
                          )}
                        >{image.is_defect ? "取消瑕疵" : "标记瑕疵"}</Button>
                        <Button
                          size="small" danger disabled={busy}
                          aria-label={`删除 ${image.filename}`}
                          onClick={() => confirmDelete(image)}
                        >删除</Button>
                      </Space>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Spin>

      {uploads.length > 0 && (
        <div aria-label="上传队列" style={{ marginTop: 14 }}>
          <div style={{ fontWeight: 500, marginBottom: 6 }}>本次上传</div>
          {uploads.map((item) => (
            <div key={item.key} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{
                  fontSize: 12, overflow: "hidden", textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}>
                  {item.file.name}
                </div>
                <Progress
                  percent={item.progress} size="small" showInfo={false}
                  status={item.status === "failed" ? "exception"
                    : item.status === "done" ? "success" : "active"}
                />
                {item.error && <div role="alert" style={{ color: "var(--mb-danger)", fontSize: 12 }}>{item.error}</div>}
              </div>
              {item.status === "failed" && (
                <Button size="small" onClick={() => { void runUpload(item); }}>重试</Button>
              )}
              {item.status === "done" && <Tag color="green">已登记</Tag>}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
