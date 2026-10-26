import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanupAntd, fireEvent, render, screen, waitFor, within } from "../../../test/antdRender";
import type { GalleryImage } from "../../../api/gallery";
import { getCover, invalidateCover, type CoverGallery } from "../../../api/galleryCovers";
import { saveGalleryDownload } from "../galleryDownloads";
import GalleryPicker from "../GalleryPicker";

vi.mock("../../../api/galleryCovers", () => ({ getCover: vi.fn(), invalidateCover: vi.fn() }));
vi.mock("../galleryDownloads", () => ({
  MAX_GALLERY_IMAGES: 500,
  MAX_GALLERY_BYTES: 2 * 1024 ** 3,
  saveGalleryDownload: vi.fn(),
}));

function image(id: string, sortOrder: number, bytes = 1024): GalleryImage {
  return {
    file_no: sortOrder + 1, filename: `${id}.jpg`, id, pn_id: "pn-id",
    url: `https://oss.example/${id}.jpg`, thumb_url: `https://oss.example/${id}-thumb.jpg`,
    width: 800, height: 600, bytes, content_type: "image/jpeg", source: "upload",
    status: "gallery", sort_order: sortOrder, is_defect: false, has_foreign_watermark: false,
    thumbnail_status: "ready", created_at: "2026-10-25T00:00:00Z",
  };
}

function gallery(partId = 42, pn = "PN-42", images = [image("third", 2), image("first", 0), image("second", 1)]): CoverGallery {
  return {
    part_id: partId, pn_id: `pn-${partId}`, pn_display: pn, gallery_revision: 1,
    has_image: Boolean(images.length), main_image: images[0] || null, images, candidates: [],
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

const select = (pn: string, index: number, shiftKey = false) => {
  fireEvent.click(screen.getByRole("checkbox", { name: `选择 ${pn} 第 ${index} 张` }), { shiftKey });
};

beforeEach(() => {
  localStorage.setItem("token", "picker-token");
  vi.mocked(getCover).mockReset().mockResolvedValue(gallery());
  vi.mocked(invalidateCover).mockReset();
  vi.mocked(saveGalleryDownload).mockReset().mockResolvedValue({ downloaded: 1, failed: [] });
});

afterEach(async () => {
  await cleanupAntd();
  vi.restoreAllMocks();
});

it("按 sort_order 稳定排列，单选显示计数和总 bytes，并把正确序号交给单张下载", async () => {
  render(<GalleryPicker items={[{ partId: 42, pn: "指定-PN" }]} />);
  expect(await screen.findByAltText("指定-PN 第 1 张")).toHaveAttribute("src", "https://oss.example/first-thumb.jpg");
  select("指定-PN", 1);
  select("指定-PN", 3);
  expect(screen.getByText("已选 2 张 · 预估 2.0 KB")).toBeInTheDocument();
  select("指定-PN", 1);
  expect(screen.getByText("已选 1 张 · 预估 1.0 KB")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "下载 指定-PN 第 2 张" }));
  await waitFor(() => expect(saveGalleryDownload).toHaveBeenCalledTimes(1));
  expect(saveGalleryDownload).toHaveBeenCalledWith(expect.objectContaining({
    kind: "single", token: "picker-token", signal: expect.any(AbortSignal),
    items: [expect.objectContaining({ partId: 42, pn: "指定-PN", index: 2, image: expect.objectContaining({ id: "second" }) })],
  }));
});

it("支持 Shift 连选与取消范围，再用顶部全选和清空重置", async () => {
  render(<GalleryPicker partIds={[42]} />);
  await screen.findByAltText("PN-42 第 1 张");
  select("PN-42", 1);
  select("PN-42", 3, true);
  expect(screen.getByText("已选 3 张 · 预估 3.0 KB")).toBeInTheDocument();
  select("PN-42", 1, true);
  expect(screen.getByText("已选 0 张 · 预估 0 B")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /^全\s*选$/ }));
  expect(screen.getByText("已选 3 张 · 预估 3.0 KB")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /^清\s*空$/ }));
  expect(screen.getByText("已选 0 张 · 预估 0 B")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "打包下载" })).toBeDisabled();
});

it("跨型号分组与选择独立，重复图片 id 不冲突，Shift 可跨组选择", async () => {
  vi.mocked(getCover).mockImplementation(async (id) => id === 42 ? gallery()
    : gallery(43, "PN-43", [image("first", 0), image("fourth", 1)]));
  render(<GalleryPicker partIds={[42, 43, 42]} />);
  await screen.findByAltText("PN-43 第 1 张");
  expect(getCover).toHaveBeenCalledTimes(2);
  const firstGroup = screen.getByRole("region", { name: "PN-42 图库" });
  const secondGroup = screen.getByRole("region", { name: "PN-43 图库" });
  fireEvent.click(within(secondGroup).getByRole("checkbox", { name: "全选本型号 PN-43" }));
  expect(screen.getByText("已选 2 张 · 预估 2.0 KB")).toBeInTheDocument();
  expect(within(firstGroup).getByRole("checkbox", { name: "全选本型号 PN-42" })).not.toBeChecked();
  fireEvent.click(within(secondGroup).getByRole("checkbox", { name: "全选本型号 PN-43" }));
  select("PN-42", 2);
  select("PN-43", 1, true);
  expect(screen.getByText("已选 3 张 · 预估 3.0 KB")).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "选择 PN-42 第 1 张" })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: "选择 PN-43 第 1 张" })).toBeChecked();
});

it("打包只传入选中的图片，并在执行期间显示完成进度和禁用选择", async () => {
  const pending = deferred<{ downloaded: number; failed: string[] }>();
  vi.mocked(saveGalleryDownload).mockImplementation((options) => {
    options.onProgress?.(1, options.items.length);
    return pending.promise;
  });
  render(<GalleryPicker partIds={[42]} />);
  await screen.findByAltText("PN-42 第 1 张");
  select("PN-42", 1);
  select("PN-42", 2);
  fireEvent.click(screen.getByRole("button", { name: "打包下载" }));
  expect(await screen.findByText("下载进度：1 / 2")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "打包下载" })).toBeDisabled();
  expect(screen.getByRole("checkbox", { name: "选择 PN-42 第 1 张" })).toBeDisabled();
  expect(saveGalleryDownload).toHaveBeenCalledWith(expect.objectContaining({
    kind: "zip", items: [expect.objectContaining({ index: 1 }), expect.objectContaining({ index: 2 })],
  }));
  await act(async () => pending.resolve({ downloaded: 2, failed: [] }));
  expect(screen.queryByText("下载进度：1 / 2")).toBeNull();
  expect(screen.getByRole("button", { name: "打包下载" })).toBeEnabled();
});

it("取消下载会 abort，并显示取消结果，之后可以重试", async () => {
  vi.mocked(saveGalleryDownload).mockImplementation((options) => new Promise((_done, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  render(<GalleryPicker partIds={[42]} />);
  await screen.findByAltText("PN-42 第 1 张");
  select("PN-42", 1);
  fireEvent.click(screen.getByRole("button", { name: "打包下载" }));
  const signal = vi.mocked(saveGalleryDownload).mock.calls[0][0].signal;
  fireEvent.click(screen.getByRole("button", { name: "取消下载" }));
  expect(signal.aborted).toBe(true);
  expect(await screen.findByText("已取消下载")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "打包下载" })).toBeEnabled();
});

it("部分失败时呈现完整失败清单，后续下载会清除旧清单", async () => {
  vi.mocked(saveGalleryDownload).mockResolvedValueOnce({ downloaded: 1, failed: ["PN-42_2.jpg", "PN-42_3.jpg"] });
  render(<GalleryPicker partIds={[42]} />);
  await screen.findByAltText("PN-42 第 1 张");
  fireEvent.click(screen.getByRole("button", { name: /^全\s*选$/ }));
  fireEvent.click(screen.getByRole("button", { name: "打包下载" }));
  expect(await screen.findByText("以下 2 张图片下载失败，已跳过")).toBeInTheDocument();
  expect(screen.getByText("PN-42_2.jpg")).toBeInTheDocument();
  expect(screen.getByText("PN-42_3.jpg")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "打包下载" }));
  await waitFor(() => expect(saveGalleryDownload).toHaveBeenCalledTimes(2));
  expect(screen.queryByText("PN-42_2.jpg")).toBeNull();
});

it("异常以 AntD 提示展示，不阻止用户重新下载", async () => {
  vi.mocked(saveGalleryDownload).mockRejectedValueOnce(new Error("网络不可用，请稍后重试"));
  render(<GalleryPicker partIds={[42]} />);
  await screen.findByAltText("PN-42 第 1 张");
  fireEvent.click(screen.getByRole("button", { name: "下载 PN-42 第 1 张" }));
  expect(await screen.findByText("网络不可用，请稍后重试")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "下载 PN-42 第 1 张" })).toBeEnabled();
});

it("选中总大小恰为 2 GB 可打包，超出后禁用且提示分批下载", async () => {
  vi.mocked(getCover).mockResolvedValue(gallery(42, "PN-42", [image("large", 0, 2 * 1024 ** 3), image("small", 1, 1)]));
  render(<GalleryPicker partIds={[42]} />);
  await screen.findByAltText("PN-42 第 1 张");
  select("PN-42", 1);
  expect(screen.getByRole("button", { name: "打包下载" })).toBeEnabled();
  select("PN-42", 2);
  expect(screen.getByText(/请分批下载/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "打包下载" })).toBeDisabled();
  expect(saveGalleryDownload).not.toHaveBeenCalled();
  select("PN-42", 2);
  expect(screen.getByRole("button", { name: "打包下载" })).toBeEnabled();
});

it("超出 500 张禁用打包，回到 500 张恢复可用", async () => {
  vi.mocked(getCover).mockResolvedValue(gallery(42, "PN-42", Array.from({ length: 501 }, (_, index) => image(`img-${index}`, index, 1))));
  render(<GalleryPicker partIds={[42]} />);
  await screen.findByAltText("PN-42 第 501 张");
  fireEvent.click(screen.getByRole("button", { name: /^全\s*选$/ }));
  expect(screen.getByRole("button", { name: "打包下载" })).toBeDisabled();
  select("PN-42", 501);
  expect(screen.getByRole("button", { name: "打包下载" })).toBeEnabled();
}, 20_000);

it("缩略图打开原图的页内预览，刷新签名后可切换和右键另存", async () => {
  const data = gallery();
  const fresh = { ...data, images: data.images.map((item) => ({ ...item, url: `${item.url}?fresh` })) };
  vi.mocked(getCover).mockResolvedValue(fresh).mockResolvedValueOnce(data);
  render(<GalleryPicker partIds={[42]} />);
  await screen.findByAltText("PN-42 第 1 张");
  fireEvent.click(screen.getByRole("button", { name: "预览 PN-42 第 2 张" }));
  await waitFor(() => expect(document.querySelector(".ant-image-preview-img")).toHaveAttribute("src", "https://oss.example/second.jpg?fresh"));
  expect(getCover).toHaveBeenCalledTimes(2);
  const original = document.querySelector(".ant-image-preview-img")!;
  expect(fireEvent.contextMenu(original)).toBe(true);
  fireEvent.click(document.querySelector(".ant-image-preview-switch-right")!);
  await waitFor(() => expect(document.querySelector(".ant-image-preview-img")).toHaveAttribute("src", "https://oss.example/third.jpg?fresh"));
});

it("预览前刷新失败时沿用已有地址，不打断选择", async () => {
  vi.mocked(getCover).mockResolvedValueOnce(gallery()).mockRejectedValueOnce(new Error("shop unavailable"));
  render(<GalleryPicker partIds={[42]} />);
  await screen.findByAltText("PN-42 第 1 张");
  select("PN-42", 1);
  fireEvent.click(screen.getByRole("button", { name: "预览 PN-42 第 1 张" }));
  await waitFor(() => expect(document.querySelector(".ant-image-preview-img")).toHaveAttribute("src", "https://oss.example/first.jpg"));
  expect(screen.getByText("已选 1 张 · 预估 1.0 KB")).toBeInTheDocument();
});

it("预览左右切换到其他型号时也先刷新该型号的签名", async () => {
  const next = gallery(43, "PN-43", [image("next", 0)]);
  let nextRequests = 0;
  vi.mocked(getCover).mockImplementation(async (partId) => {
    if (partId === 42) return gallery(42, "PN-42", [image("first", 0)]);
    nextRequests += 1;
    return nextRequests === 1 ? next
      : { ...next, images: [{ ...next.images[0], url: "https://oss.example/next-fresh.jpg" }] };
  });
  render(<GalleryPicker partIds={[42, 43]} />);
  await screen.findByAltText("PN-43 第 1 张");
  fireEvent.click(screen.getByRole("button", { name: "预览 PN-42 第 1 张" }));
  await waitFor(() => expect(document.querySelector(".ant-image-preview-img")).toHaveAttribute("src", "https://oss.example/first.jpg"));
  fireEvent.click(document.querySelector(".ant-image-preview-switch-right")!);
  await waitFor(() => expect(document.querySelector(".ant-image-preview-img")).toHaveAttribute("src", "https://oss.example/next-fresh.jpg"));
  expect(nextRequests).toBe(2);
});

it("无型号、无图、加载中和失败重试都有对应状态", async () => {
  const pending = deferred<CoverGallery>();
  vi.mocked(getCover).mockReturnValueOnce(pending.promise).mockResolvedValueOnce(gallery(42, "PN-42", []));
  const view = render(<GalleryPicker partIds={[]} />);
  expect(screen.getByText("请选择型号")).toBeInTheDocument();
  view.rerender(<GalleryPicker partIds={[42]} />);
  expect(screen.getByText("图库加载中")).toBeInTheDocument();
  await act(async () => pending.reject(new Error("unavailable")));
  expect(await screen.findByText("图库加载失败，请重试")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /^重\s*试$/ }));
  expect(await screen.findByText("暂无图片")).toBeInTheDocument();
  expect(invalidateCover).toHaveBeenCalledWith(42);
});

it("没有登录态时不拉图库，显示明确提示", async () => {
  localStorage.removeItem("token");
  render(<GalleryPicker partIds={[42]} />);
  expect(await screen.findByText("登录状态已失效，请重新登录")).toBeInTheDocument();
  expect(getCover).not.toHaveBeenCalled();
});

it("modal 关闭会取消进行中的下载并调用 onClose", async () => {
  const pending = deferred<{ downloaded: number; failed: string[] }>();
  vi.mocked(saveGalleryDownload).mockReturnValue(pending.promise);
  const onClose = vi.fn();
  render(<GalleryPicker partIds={[42]} mode="modal" onClose={onClose} />);
  expect(screen.getByRole("dialog", { name: "图库多选下载" })).toBeInTheDocument();
  await screen.findByAltText("PN-42 第 1 张");
  fireEvent.click(screen.getByRole("button", { name: "下载 PN-42 第 1 张" }));
  const signal = vi.mocked(saveGalleryDownload).mock.calls[0][0].signal;
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(onClose).toHaveBeenCalledTimes(1);
  expect(signal.aborted).toBe(true);
  await act(async () => pending.resolve({ downloaded: 1, failed: ["late.jpg"] }));
  expect(screen.queryByText("late.jpg")).toBeNull();
  expect(screen.queryByRole("dialog", { name: "图库多选下载" })).toBeNull();
});

it("modal 未传 onClose 时也可以自行关闭", async () => {
  render(<GalleryPicker partIds={[42]} mode="modal" />);
  await screen.findByAltText("PN-42 第 1 张");
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "图库多选下载" })).toBeNull());
});

it("换型号时清空选择并 abort，迟到的下载与图库响应均被丢弃", async () => {
  const lateGallery = deferred<CoverGallery>();
  const lateDownload = deferred<{ downloaded: number; failed: string[] }>();
  vi.mocked(getCover).mockImplementation((id) => id === 42 ? Promise.resolve(gallery())
    : id === 43 ? lateGallery.promise : Promise.resolve(gallery(44, "PN-44", [image("new", 0)])));
  vi.mocked(saveGalleryDownload).mockReturnValue(lateDownload.promise);
  const view = render(<GalleryPicker partIds={[42]} />);
  await screen.findByAltText("PN-42 第 1 张");
  select("PN-42", 1);
  fireEvent.click(screen.getByRole("button", { name: "打包下载" }));
  const signal = vi.mocked(saveGalleryDownload).mock.calls[0][0].signal;
  view.rerender(<GalleryPicker partIds={[43]} />);
  expect(signal.aborted).toBe(true);
  view.rerender(<GalleryPicker partIds={[44]} />);
  await screen.findByAltText("PN-44 第 1 张");
  await act(async () => {
    lateGallery.resolve(gallery(43, "PN-43"));
    lateDownload.resolve({ downloaded: 1, failed: ["old.jpg"] });
  });
  expect(screen.getByText("已选 0 张 · 预估 0 B")).toBeInTheDocument();
  expect(screen.queryByAltText("PN-43 第 1 张")).toBeNull();
  expect(screen.queryByText("old.jpg")).toBeNull();
});

it("同内容数组重渲染保持选择，换 token 会重新请求并清空", async () => {
  const view = render(<GalleryPicker partIds={[42]} />);
  await screen.findByAltText("PN-42 第 1 张");
  select("PN-42", 1);
  view.rerender(<GalleryPicker partIds={[42]} />);
  expect(screen.getByText("已选 1 张 · 预估 1.0 KB")).toBeInTheDocument();
  expect(getCover).toHaveBeenCalledTimes(1);
  localStorage.setItem("token", "next-token");
  view.rerender(<GalleryPicker partIds={[42]} />);
  await waitFor(() => expect(getCover).toHaveBeenLastCalledWith(42, "next-token"));
  expect(screen.getByText("已选 0 张 · 预估 0 B")).toBeInTheDocument();
});

it("卸载时 abort 下载，忽略迟到的预览刷新", async () => {
  const preview = deferred<CoverGallery>();
  const pending = deferred<{ downloaded: number; failed: string[] }>();
  vi.mocked(getCover).mockResolvedValueOnce(gallery()).mockReturnValueOnce(preview.promise);
  vi.mocked(saveGalleryDownload).mockReturnValueOnce(pending.promise);
  const view = render(<GalleryPicker partIds={[42]} />);
  await screen.findByAltText("PN-42 第 1 张");
  fireEvent.click(screen.getByRole("button", { name: "预览 PN-42 第 1 张" }));
  fireEvent.click(screen.getByRole("button", { name: "下载 PN-42 第 1 张" }));
  const signal = vi.mocked(saveGalleryDownload).mock.calls[0][0].signal;
  view.unmount();
  expect(signal.aborted).toBe(true);
  await act(async () => {
    preview.resolve(gallery());
    pending.resolve({ downloaded: 1, failed: ["late.jpg"] });
  });
  expect(document.querySelector(".ant-image-preview-img")).toBeNull();
});
