import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  MAINTENANCE_CHANGED_EVENT, MAINTENANCE_CHANGED_STORAGE_KEY,
  publishMaintenanceChange, useVisibleMaintenanceRefresh,
} from "../maintenanceRefresh";

let visibility: DocumentVisibilityState;
beforeEach(() => {
  vi.useFakeTimers(); visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); localStorage.clear(); });
const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

describe("维保分析可见时刷新", () => {
  it("每30秒读回，隐藏时停止，隐藏期间的通知在可见后合并成一次刷新", async () => {
    const refresh = vi.fn().mockResolvedValue(true);
    renderHook(() => useVisibleMaintenanceRefresh(refresh));
    await advance(29_999); expect(refresh).not.toHaveBeenCalled();
    await advance(1); expect(refresh).toHaveBeenCalledTimes(1);
    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    publishMaintenanceChange();
    window.dispatchEvent(new Event("focus"));
    await advance(90_000); expect(refresh).toHaveBeenCalledTimes(1);
    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    window.dispatchEvent(new Event("focus"));
    await advance(0); expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("正在读回时合并全部通知，只排一次后续刷新，不产生并发", async () => {
    let resolve!: (value: boolean) => void;
    const refresh = vi.fn().mockImplementationOnce(() => new Promise<boolean>((done) => { resolve = done; })).mockResolvedValue(true);
    renderHook(() => useVisibleMaintenanceRefresh(refresh));
    publishMaintenanceChange();
    await advance(0); expect(refresh).toHaveBeenCalledTimes(1);
    publishMaintenanceChange(); publishMaintenanceChange();
    window.dispatchEvent(new StorageEvent("storage", { key: MAINTENANCE_CHANGED_STORAGE_KEY, newValue: "123" }));
    await advance(90_000); expect(refresh).toHaveBeenCalledTimes(1);
    await act(async () => resolve(true));
    await advance(0); expect(refresh).toHaveBeenCalledTimes(2);
    await advance(29_999); expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("前台请求忙碌时稍后重试，并使用最新筛选所对应的读回函数", async () => {
    const busy = vi.fn().mockResolvedValue(false);
    const ready = vi.fn().mockResolvedValue(true);
    const { rerender } = renderHook(({ refresh }) => useVisibleMaintenanceRefresh(refresh), { initialProps: { refresh: busy } });
    publishMaintenanceChange(); await advance(0);
    expect(busy).toHaveBeenCalledTimes(1);
    rerender({ refresh: ready });
    await advance(999); expect(ready).not.toHaveBeenCalled();
    await advance(1); expect(ready).toHaveBeenCalledTimes(1);
  });

  it("不相关的跨tab存储事件不触发读回，匹配事件会触发", async () => {
    const refresh = vi.fn().mockResolvedValue(true);
    renderHook(() => useVisibleMaintenanceRefresh(refresh));
    window.dispatchEvent(new StorageEvent("storage", { key: "other", newValue: "1" }));
    await advance(0); expect(refresh).not.toHaveBeenCalled();
    window.dispatchEvent(new StorageEvent("storage", { key: MAINTENANCE_CHANGED_STORAGE_KEY, newValue: "1" }));
    await advance(0); expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("storage被禁用时仍发送同页信号，不影响业务成功；通知只包含时间戳", () => {
    const listener = vi.fn();
    window.addEventListener(MAINTENANCE_CHANGED_EVENT, listener);
    const storage = vi.spyOn(Storage.prototype, "setItem");
    publishMaintenanceChange();
    expect(storage).toHaveBeenLastCalledWith(MAINTENANCE_CHANGED_STORAGE_KEY, expect.stringMatching(/^\d+$/));
    storage.mockImplementation(() => { throw new Error("storage disabled"); });
    expect(publishMaintenanceChange).not.toThrow();
    expect(listener).toHaveBeenCalledTimes(2);
    window.removeEventListener(MAINTENANCE_CHANGED_EVENT, listener);
  });

  it("卸载后清理监听、计时器，进行中的请求完成也不重建轮询", async () => {
    let resolve!: (value: boolean) => void;
    const refresh = vi.fn(() => new Promise<boolean>((done) => { resolve = done; }));
    const { unmount } = renderHook(() => useVisibleMaintenanceRefresh(refresh));
    publishMaintenanceChange(); await advance(0);
    unmount();
    publishMaintenanceChange(); window.dispatchEvent(new Event("focus"));
    await act(async () => resolve(true));
    await advance(90_000);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });
});
