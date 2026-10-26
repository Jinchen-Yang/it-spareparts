import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { getWorkBadge } from "../../../api/workItems";
import { refreshWorkBadges, useWorkBadge } from "../useWorkBadge";

vi.mock("../../../api/workItems", () => ({ getWorkBadge: vi.fn() }));

function visibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", { configurable: true, value: state });
  act(() => { document.dispatchEvent(new Event("visibilitychange")); });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
  vi.mocked(getWorkBadge).mockResolvedValue({ assigned: 1, pool: 2, claimed: 3 });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
});

it("初始化读取三个计数，每 60 秒刷新，卸载后停止", async () => {
  const view = renderHook(useWorkBadge);
  await act(async () => {});
  expect(view.result.current).toEqual({ assigned: 1, pool: 2, claimed: 3 });
  await act(async () => { vi.advanceTimersByTime(59_999); });
  expect(getWorkBadge).toHaveBeenCalledTimes(1);
  await act(async () => { vi.advanceTimersByTime(1); });
  expect(getWorkBadge).toHaveBeenCalledTimes(2);
  view.unmount();
  await act(async () => { vi.advanceTimersByTime(120_000); });
  expect(getWorkBadge).toHaveBeenCalledTimes(2);
});

it("页面隐藏时停止请求，再次可见时立即刷新并恢复轮询", async () => {
  renderHook(useWorkBadge);
  await act(async () => {});
  visibility("hidden");
  await act(async () => { vi.advanceTimersByTime(180_000); refreshWorkBadges(); });
  expect(getWorkBadge).toHaveBeenCalledTimes(1);
  visibility("visible");
  await act(async () => {});
  expect(getWorkBadge).toHaveBeenCalledTimes(2);
  await act(async () => { vi.advanceTimersByTime(60_000); });
  expect(getWorkBadge).toHaveBeenCalledTimes(3);
});

it("在隐藏页面挂载不请求，动作刷新信号会更新全部角标使用方", async () => {
  visibility("hidden");
  const first = renderHook(useWorkBadge);
  const second = renderHook(useWorkBadge);
  expect(getWorkBadge).not.toHaveBeenCalled();
  visibility("visible");
  await act(async () => {});
  expect(getWorkBadge).toHaveBeenCalledTimes(2);
  vi.mocked(getWorkBadge).mockResolvedValue({ assigned: 0, pool: 1, claimed: 4 });
  await act(async () => { refreshWorkBadges(); });
  expect(getWorkBadge).toHaveBeenCalledTimes(4);
  expect(first.result.current).toEqual({ assigned: 0, pool: 1, claimed: 4 });
  expect(second.result.current).toEqual(first.result.current);
});

it("轮询失败保留上次计数，隐藏时取消的迟到响应不覆盖计数", async () => {
  let resolve!: (value: { assigned: number; pool: number; claimed: number }) => void;
  const view = renderHook(useWorkBadge);
  await act(async () => {});
  vi.mocked(getWorkBadge).mockRejectedValueOnce(new Error("网络错误"));
  await act(async () => { vi.advanceTimersByTime(60_000); });
  expect(view.result.current).toEqual({ assigned: 1, pool: 2, claimed: 3 });
  vi.mocked(getWorkBadge).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
  await act(async () => { vi.advanceTimersByTime(60_000); });
  const calls = vi.mocked(getWorkBadge).mock.calls;
  const signal = calls[calls.length - 1]?.[0];
  visibility("hidden");
  expect(signal?.aborted).toBe(true);
  await act(async () => { resolve({ assigned: 99, pool: 99, claimed: 99 }); });
  expect(view.result.current).toEqual({ assigned: 1, pool: 2, claimed: 3 });
});
