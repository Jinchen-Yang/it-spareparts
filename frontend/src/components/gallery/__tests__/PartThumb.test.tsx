import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanupAntd, fireEvent, render, screen, waitFor } from "../../../test/antdRender";
import { fetchPartGallery, type GalleryImage, type GalleryList } from "../../../api/gallery";
import { invalidateCover, type CoverGallery } from "../../../api/galleryCovers";
import PartThumb from "../PartThumb";

vi.mock("../../../api/gallery", () => ({ fetchPartGallery: vi.fn() }));

const image: GalleryImage = {
  file_no: 1, filename: "front.jpg", id: "front", pn_id: "pn-id",
  url: "https://oss.example/front.jpg", thumb_url: "https://oss.example/front-thumb.jpg",
  width: 800, height: 600, bytes: 3, content_type: "image/jpeg", source: "upload",
  status: "gallery", sort_order: 0, is_defect: false, has_foreign_watermark: false,
  thumbnail_status: "ready", created_at: "2026-10-25T00:00:00Z",
};
const back = { ...image, id: "back", filename: "back.jpg", url: "https://oss.example/back.jpg", thumb_url: null };
const gallery: GalleryList = {
  part_id: 42, pn_id: "pn-id", pn_display: "PN-42", gallery_revision: 1,
  has_image: true, main_image: image, images: [image, back], candidates: [],
};

beforeEach(() => {
  localStorage.setItem("token", "thumb-token");
  vi.mocked(fetchPartGallery).mockReset();
  invalidateCover(42);
});

afterEach(async () => {
  await cleanupAntd();
  vi.unstubAllGlobals();
});

it("加载时显示与最终图片相同尺寸的灰色占位", async () => {
  let resolve!: (data: GalleryList) => void;
  vi.mocked(fetchPartGallery).mockReturnValue(new Promise((done) => { resolve = done; }));
  render(<PartThumb partId={42} pn="PN-42" />);
  expect(screen.getByRole("status", { name: "图片加载中" })).toBeInTheDocument();
  const thumb = screen.getByRole("button", { name: "PN-42 图片" });
  expect(thumb).toHaveStyle({ width: "48px", height: "48px" });
  await act(async () => resolve(gallery));
  expect(await screen.findByAltText("PN-42 图片")).toBeInTheDocument();
  expect(thumb).toHaveStyle({ width: "48px", height: "48px" });
});

it("有图时优先 main_image_id，显示张数角标和固定尺寸", async () => {
  const data: CoverGallery = { ...gallery, main_image_id: "back" };
  vi.mocked(fetchPartGallery).mockResolvedValue(data);
  render(<PartThumb partId={42} pn="PN-42" size={56} />);
  expect(await screen.findByAltText("PN-42 图片")).toHaveAttribute("src", back.url);
  expect(screen.getByLabelText("2 张图片")).toHaveTextContent("2");
  expect(screen.getByRole("button", { name: "PN-42 图片" })).toHaveStyle({ width: "56px", height: "56px" });
});

it("兼容现有 main_image 字段，无主图时取第一张", async () => {
  vi.mocked(fetchPartGallery).mockResolvedValueOnce({ ...gallery, main_image: back });
  const view = render(<PartThumb partId={42} pn="PN-42" />);
  expect(await screen.findByAltText("PN-42 图片")).toHaveAttribute("src", back.url);
  vi.mocked(fetchPartGallery).mockResolvedValueOnce({ ...gallery, main_image: null });
  act(() => invalidateCover(42));
  await waitFor(() => expect(screen.getByAltText("PN-42 图片")).toHaveAttribute("src", image.thumb_url));
  view.unmount();
});

it("无图时显示上传提示，点击和回车都调用 onEmptyClick", async () => {
  vi.mocked(fetchPartGallery).mockResolvedValue({ ...gallery, images: [], main_image: null, has_image: false });
  const onEmptyClick = vi.fn();
  render(<PartThumb partId={42} pn="PN-42" onEmptyClick={onEmptyClick} />);
  const thumb = screen.getByRole("button", { name: "PN-42 图片" });
  await waitFor(() => expect(thumb).toHaveAttribute("aria-busy", "false"));
  fireEvent.mouseOver(thumb);
  expect(await screen.findByText("暂无图片，点击上传")).toBeInTheDocument();
  fireEvent.click(thumb);
  fireEvent.keyDown(thumb, { key: "Enter" });
  expect(onEmptyClick).toHaveBeenCalledTimes(2);
  expect(screen.queryByAltText("PN-42 图片")).toBeNull();
});

it("图库请求失败时只显示静默错误占位，不调用上传回调", async () => {
  vi.mocked(fetchPartGallery).mockRejectedValue(new Error("unavailable"));
  const onEmptyClick = vi.fn();
  render(<PartThumb partId={42} pn="PN-42" onEmptyClick={onEmptyClick} />);
  const thumb = screen.getByRole("button", { name: "PN-42 图片" });
  await waitFor(() => expect(thumb).toHaveAttribute("aria-busy", "false"));
  fireEvent.mouseOver(thumb);
  expect(await screen.findByText("图库暂不可用")).toBeInTheDocument();
  fireEvent.click(thumb);
  expect(onEmptyClick).not.toHaveBeenCalled();
  expect(document.querySelector(".ant-message-notice")).toBeNull();
});

it("图片加载失败也回到静默错误占位", async () => {
  vi.mocked(fetchPartGallery).mockResolvedValue(gallery);
  render(<PartThumb partId={42} pn="PN-42" />);
  fireEvent.error(await screen.findByAltText("PN-42 图片"));
  const thumb = screen.getByRole("button", { name: "PN-42 图片" });
  expect(thumb).toHaveAttribute("aria-disabled", "true");
  fireEvent.mouseOver(thumb);
  expect(await screen.findByText("图库暂不可用")).toBeInTheDocument();
});

it("点击缩略图后用页面内 img 预览原图，可切换到下一张", async () => {
  vi.mocked(fetchPartGallery).mockResolvedValue(gallery);
  render(<PartThumb partId={42} pn="PN-42" />);
  await screen.findByAltText("PN-42 图片");
  fireEvent.click(screen.getByRole("button", { name: "PN-42 图片" }));
  await waitFor(() => expect(document.querySelector(".ant-image-preview")).toBeInTheDocument());
  expect(document.querySelector(".ant-image-preview-img")).toHaveAttribute("src", image.url);
  fireEvent.click(document.querySelector(".ant-image-preview-switch-right")!);
  await waitFor(() => expect(document.querySelector(".ant-image-preview-img")).toHaveAttribute("src", back.url));
});

it("回车可打开有图预览", async () => {
  vi.mocked(fetchPartGallery).mockResolvedValue(gallery);
  render(<PartThumb partId={42} pn="PN-42" />);
  await screen.findByAltText("PN-42 图片");
  fireEvent.keyDown(screen.getByRole("button", { name: "PN-42 图片" }), { key: "Enter" });
  await waitFor(() => expect(document.querySelector(".ant-image-preview")).toBeInTheDocument());
});

it("支持 IntersectionObserver 时只有进入可视区域才请求", async () => {
  let intersect!: IntersectionObserverCallback;
  const disconnect = vi.fn();
  vi.stubGlobal("IntersectionObserver", class {
    constructor(callback: IntersectionObserverCallback) { intersect = callback; }
    observe = vi.fn();
    disconnect = disconnect;
  });
  vi.mocked(fetchPartGallery).mockResolvedValue(gallery);
  render(<PartThumb partId={42} pn="PN-42" />);
  expect(fetchPartGallery).not.toHaveBeenCalled();
  act(() => intersect([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver));
  expect(fetchPartGallery).not.toHaveBeenCalled();
  act(() => intersect([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver));
  expect(await screen.findByAltText("PN-42 图片")).toBeInTheDocument();
  expect(fetchPartGallery).toHaveBeenCalledTimes(1);
  expect(disconnect).toHaveBeenCalled();
});

it("invalidateCover 会刷新已挂载的缩略图", async () => {
  vi.mocked(fetchPartGallery).mockResolvedValueOnce(gallery).mockResolvedValueOnce({
    ...gallery, images: [back], main_image: back,
  });
  render(<PartThumb partId={42} pn="PN-42" />);
  expect(await screen.findByAltText("PN-42 图片")).toHaveAttribute("src", image.thumb_url);
  act(() => invalidateCover(42));
  await waitFor(() => expect(screen.getByAltText("PN-42 图片")).toHaveAttribute("src", back.url));
  expect(fetchPartGallery).toHaveBeenCalledTimes(2);
});

it("切换备件后慢旧响应不会覆盖当前缩略图", async () => {
  let resolve!: (data: GalleryList) => void;
  vi.mocked(fetchPartGallery).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
  vi.mocked(fetchPartGallery).mockResolvedValueOnce({ ...gallery, part_id: 43, images: [back], main_image: back });
  const view = render(<PartThumb partId={42} pn="PN-42" />);
  await waitFor(() => expect(fetchPartGallery).toHaveBeenCalledTimes(1));
  view.rerender(<PartThumb partId={43} pn="PN-43" />);
  expect(await screen.findByAltText("PN-43 图片")).toHaveAttribute("src", back.url);
  await act(async () => resolve(gallery));
  expect(screen.getByAltText("PN-43 图片")).toHaveAttribute("src", back.url);
});

it("token 改变并重渲染时重新请求，不使用旧账号的图片", async () => {
  vi.mocked(fetchPartGallery).mockResolvedValueOnce(gallery).mockResolvedValueOnce({
    ...gallery, images: [back], main_image: back,
  });
  const view = render(<PartThumb partId={42} pn="PN-42" />);
  expect(await screen.findByAltText("PN-42 图片")).toHaveAttribute("src", image.thumb_url);
  localStorage.setItem("token", "new-thumb-token");
  view.rerender(<PartThumb partId={42} pn="PN-42" />);
  await waitFor(() => expect(screen.getByAltText("PN-42 图片")).toHaveAttribute("src", back.url));
  expect(fetchPartGallery).toHaveBeenLastCalledWith(42, "new-thumb-token");
});

it("签名链接过期后点击预览会重新取图库并用新链接", async () => {
  const soon = new Date(Date.now() + 61_000).toISOString();
  const stale = { ...gallery, images: [{ ...image, url_expires_at: soon }, back] } as CoverGallery;
  const freshImage = { ...image, url: "https://oss.example/front-fresh.jpg" };
  vi.mocked(fetchPartGallery).mockResolvedValueOnce(stale).mockResolvedValueOnce({ ...gallery, images: [freshImage, back] });
  render(<PartThumb partId={42} pn="PN-42" />);
  await screen.findByAltText("PN-42 图片");
  const realNow = Date.now;
  vi.spyOn(Date, "now").mockImplementation(() => realNow() + 5_000);
  fireEvent.click(screen.getByRole("button", { name: "PN-42 图片" }));
  await waitFor(() => expect(document.querySelector(".ant-image-preview-img")).toHaveAttribute("src", freshImage.url));
  expect(fetchPartGallery).toHaveBeenCalledTimes(2);
  vi.restoreAllMocks();
});

it("预览前刷新失败时仍用已有链接预览，缩略图不变成错误态", async () => {
  const soon = new Date(Date.now() + 61_000).toISOString();
  const stale = { ...gallery, images: [{ ...image, url_expires_at: soon }, back] } as CoverGallery;
  vi.mocked(fetchPartGallery).mockResolvedValueOnce(stale).mockRejectedValueOnce(new Error("shop down"));
  render(<PartThumb partId={42} pn="PN-42" />);
  await screen.findByAltText("PN-42 图片");
  const realNow = Date.now;
  vi.spyOn(Date, "now").mockImplementation(() => realNow() + 5_000);
  fireEvent.click(screen.getByRole("button", { name: "PN-42 图片" }));
  await waitFor(() => expect(document.querySelector(".ant-image-preview-img")).toHaveAttribute("src", image.url));
  expect(screen.getByAltText("PN-42 图片")).toBeInTheDocument();
  vi.restoreAllMocks();
});

it("换型号后迟到的预览刷新结果被丢弃", async () => {
  const soon = new Date(Date.now() + 61_000).toISOString();
  const stale = { ...gallery, images: [{ ...image, url_expires_at: soon }, back] } as CoverGallery;
  let resolveLate!: (data: GalleryList) => void;
  const other = { ...gallery, part_id: 43, images: [{ ...image, id: "other", url: "https://oss.example/other.jpg" }] };
  vi.mocked(fetchPartGallery).mockImplementation((partId: number) => {
    if (partId === 43) return Promise.resolve(other);
    return vi.mocked(fetchPartGallery).mock.calls.filter(([id]) => id === 42).length === 1
      ? Promise.resolve(stale) : new Promise((done) => { resolveLate = done; });
  });
  const { rerender } = render(<PartThumb partId={42} pn="PN-42" />);
  await screen.findByAltText("PN-42 图片");
  const realNow = Date.now;
  vi.spyOn(Date, "now").mockImplementation(() => realNow() + 5_000);
  fireEvent.click(screen.getByRole("button", { name: "PN-42 图片" }));
  rerender(<PartThumb partId={43} pn="PN-43" />);
  await screen.findByAltText("PN-43 图片");
  await act(async () => resolveLate({ ...gallery, images: [{ ...image, url: "https://oss.example/late.jpg" }] }));
  expect(document.querySelector('.ant-image-preview-img[src="https://oss.example/late.jpg"]')).toBeNull();
  vi.restoreAllMocks();
  invalidateCover(43);
});
