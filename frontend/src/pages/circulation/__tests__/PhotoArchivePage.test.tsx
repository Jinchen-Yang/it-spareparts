/** 循环拍照建档页行为测试（D-6/D-24 交互契约）。 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const getArchive = vi.fn();
const uploadArchiveAttachment = vi.fn();

vi.mock("../../../api/circulation", () => ({
  getArchive: (...args: unknown[]) => getArchive(...args),
  uploadArchiveAttachment: (...args: unknown[]) => uploadArchiveAttachment(...args),
}));

import PhotoArchivePage from "../PhotoArchivePage";

function renderPage() {
  return render(<PhotoArchivePage />);
}

async function locate(pn: string) {
  const input = screen.getByTestId("pn-input");
  fireEvent.change(input, { target: { value: pn } });
  fireEvent.keyDown(input, { key: "Enter" });
  await waitFor(() => expect(screen.queryByText("查找失败：网络或服务异常，请重试")).toBeNull());
}

function pickPhoto(name: string) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  Object.defineProperty(input, "files", {
    value: [new File([new Uint8Array([1, 2, 3])], name, { type: "image/png" })],
  });
  fireEvent.change(input);
}

const ARCHIVE_WITH_PHOTOS = {
  archive_id: "a1", pn_std: "PNSYN-0001", part_id: null,
  listing_status: "pending", requirements_met: false,
  photo_count: 2, report_count: 0, force_listed_by: null, force_listed_at: null,
  force_reason: null,
  attachments: [
    { attachment_id: "at1", kind: "photo", original_filename: "a.png", mime_type: "image/png",
      size_bytes: 3, sha256: "aa", uploaded_by: "甲", uploaded_at: "2026-10-08T10:00:00" },
    { attachment_id: "at2", kind: "photo", original_filename: "b.png", mime_type: "image/png",
      size_bytes: 3, sha256: "bb", uploaded_by: "甲", uploaded_at: "2026-10-08T10:01:00" },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(cleanup);

describe("循环拍照建档（D-24 交互契约）", () => {
  it("已有照片的 PN：提示无需重复拍摄，不进入引导拍照", async () => {
    getArchive.mockResolvedValue(ARCHIVE_WITH_PHOTOS);
    renderPage();
    await locate("PNSYN-0001");
    expect(getArchive).toHaveBeenCalledWith("PNSYN-0001");
    expect(await screen.findByText(/已有 2 张照片，无需重复拍摄/)).toBeTruthy();
    expect(screen.queryByText("② 引导拍照")).toBeNull();          // 不走下一步
    expect(screen.queryByRole("button", { name: /确认上传/ })).toBeNull();
  });

  it("档案存在但无照片：进入引导拍照，上传后确认并调用接口", async () => {
    getArchive.mockResolvedValue({ ...ARCHIVE_WITH_PHOTOS, photo_count: 0, attachments: [] });
    uploadArchiveAttachment.mockResolvedValue({ ...ARCHIVE_WITH_PHOTOS, photo_count: 1 });
    renderPage();
    await locate("PNSYN-0001");
    expect(await screen.findByText("② 引导拍照")).toBeTruthy();
    pickPhoto("front.png");
    const btn = await screen.findByRole("button", { name: /确认上传（1 张）/ });
    fireEvent.click(btn);
    await waitFor(() => expect(uploadArchiveAttachment).toHaveBeenCalledTimes(1));
    expect(uploadArchiveAttachment).toHaveBeenCalledWith("PNSYN-0001", "photo", expect.anything());
    expect(await screen.findByText("建档照片已上传")).toBeTruthy();
  });

  it("尚无档案的 PN：提示自动建档并同样进入引导拍照", async () => {
    getArchive.mockResolvedValue(null);
    renderPage();
    await locate("PNSYN-0999");
    expect(await screen.findByText(/尚无循环档案，上传照片后将自动建档/)).toBeTruthy();
    expect(screen.getByText("② 引导拍照")).toBeTruthy();
  });

  it("空输入回车：提示先输入 PN，不发起查询", async () => {
    renderPage();
    await locate("");
    expect(getArchive).not.toHaveBeenCalled();
    expect(screen.getByText("请先输入 PN 或扫入序列号")).toBeTruthy();
  });
});
