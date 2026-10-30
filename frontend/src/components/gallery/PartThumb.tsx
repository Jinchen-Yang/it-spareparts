import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Image, Tooltip } from "antd";
import { PictureOutlined } from "@ant-design/icons";
import { getCover, subscribeCover, type CoverGallery } from "../../api/galleryCovers";

interface Props {
  partId: number;
  pn: string;
  size?: number;
  onEmptyClick?: () => void;
}

type Status = "loading" | "ready" | "empty" | "error";

export default function PartThumb({ partId, pn, size = 48, onEmptyClick }: Props) {
  const element = useRef<HTMLSpanElement>(null);
  const [visible, setVisible] = useState(typeof IntersectionObserver === "undefined");
  const [revision, setRevision] = useState(0);
  const [status, setStatus] = useState<Status>("loading");
  const [gallery, setGallery] = useState<CoverGallery | null>(null);
  const [preview, setPreview] = useState(false);
  const [current, setCurrent] = useState(0);
  const token = localStorage.getItem("token");

  useEffect(() => subscribeCover(partId, () => setRevision((value) => value + 1)), [partId]);

  useEffect(() => {
    if (visible || !element.current || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        setVisible(true);
        observer.disconnect();
      }
    });
    observer.observe(element.current);
    return () => observer.disconnect();
  }, [visible]);

  // 每次重新加载（换型号 / 失效刷新 / 卸载）都换代，预览的迟到响应按代次丢弃。
  const viewGeneration = useRef(0);
  useEffect(() => () => { viewGeneration.current += 1; }, []);

  useEffect(() => {
    if (!visible) return;
    viewGeneration.current += 1;
    let disposed = false;
    setStatus("loading");
    setGallery(null);
    setPreview(false);
    if (!token) {
      setStatus("error");
      return;
    }
    void getCover(partId, token).then((data) => {
      if (disposed) return;
      if (!Array.isArray(data.images)) throw new Error("图库响应无效");
      setGallery(data);
      setStatus(data.images.length ? "ready" : "empty");
    }).catch(() => {
      if (!disposed) setStatus("error");
    });
    return () => { disposed = true; };
  }, [partId, token, visible, revision]);

  const images = gallery?.images || [];
  const mainId = gallery?.main_image_id ?? gallery?.main_image?.id;
  const mainIndex = Math.max(0, images.findIndex((image) => image.id === mainId));
  const main = images[mainIndex];
  const actionable = status === "ready" || (status === "empty" && Boolean(onEmptyClick));
  const open = () => {
    if (status === "ready") {
      const generation = viewGeneration.current;
      const showCurrent = () => { setCurrent(mainIndex); setPreview(true); };
      if (!token) { showCurrent(); return; }
      // 签名链接可能已过期：打开预览前经缓存取一次（过期会重新拉取）。
      // 刷新失败时退回用已有链接预览，不把正常缩略图打成错误态。
      void getCover(partId, token).then((data) => {
        if (generation !== viewGeneration.current) return;
        const fresh = Array.isArray(data.images) ? data.images : [];
        if (!fresh.length) { showCurrent(); return; }
        const freshMain = data.main_image_id ?? data.main_image?.id;
        setGallery(data);
        setCurrent(Math.max(0, fresh.findIndex((image) => image.id === freshMain)));
        setPreview(true);
      }).catch(() => {
        if (generation === viewGeneration.current) showCurrent();
      });
    } else if (status === "empty") onEmptyClick?.();
  };
  const style: CSSProperties = {
    display: "inline-flex", alignItems: "center", justifyContent: "center",
    position: "relative", width: size, height: size, minWidth: size, flexShrink: 0,
    verticalAlign: "middle", overflow: "hidden", boxSizing: "border-box",
    borderRadius: 6, border: "1px solid var(--mb-border-soft)",
    background: "var(--mb-inset)", color: "var(--mb-text-3)",
    cursor: actionable ? "pointer" : "default",
  };
  const title = status === "empty" ? (onEmptyClick ? "暂无图片，点击上传" : "暂无图片")
    : status === "error" ? "图库暂不可用" : undefined;

  return (
    <>
      <Tooltip title={title}>
        <span
          ref={element} role="button" tabIndex={0} aria-label={`${pn} 图片`}
          aria-disabled={!actionable} aria-busy={status === "loading"}
          style={style}
          onClick={(event) => { event.stopPropagation(); open(); }}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              event.stopPropagation();
              open();
            }
          }}
        >
          {status === "loading" ? (
            <span role="status" aria-label="图片加载中" style={{ width: "100%", height: "100%" }} />
          ) : status === "ready" && main ? (
            <>
              <img
                src={main.thumb_url || main.url} alt={`${pn} 图片`}
                style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
                onError={() => { setStatus("error"); setPreview(false); }}
              />
              {images.length > 1 && (
                <span aria-label={`${images.length} 张图片`} style={{
                  position: "absolute", right: 2, bottom: 2, padding: "0 4px",
                  borderRadius: 3, fontSize: 10, lineHeight: "16px",
                  color: "var(--mb-text-2)", background: "var(--mb-surface)",
                }}>{images.length}</span>
              )}
            </>
          ) : <PictureOutlined aria-hidden style={{ fontSize: Math.max(16, size / 2) }} />}
        </span>
      </Tooltip>
      {gallery && images.length > 0 && (
        <Image.PreviewGroup
          items={images.map((image) => ({ src: image.url, alt: `${pn} 图片` }))}
          preview={{ visible: preview, current, onVisibleChange: setPreview, onChange: setCurrent }}
        />
      )}
    </>
  );
}
