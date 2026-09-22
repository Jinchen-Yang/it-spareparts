import { useEffect, useRef } from "react";

export const MAINTENANCE_CHANGED_EVENT = "maintenance-data-changed";
export const MAINTENANCE_CHANGED_STORAGE_KEY = "maintenance:last-change";
let lastChange = 0;

/** 只发送变化信号，不把项目、账号或业务内容写入跨页通知。 */
export function publishMaintenanceChange(): void {
  lastChange = Math.max(Date.now(), lastChange + 1);
  window.dispatchEvent(new Event(MAINTENANCE_CHANGED_EVENT));
  try {
    window.localStorage.setItem(MAINTENANCE_CHANGED_STORAGE_KEY, String(lastChange));
  } catch {
    // 禁用 storage 只影响跨 tab 的即时提示，不能让已成功的业务操作报错。
  }
}

/** false 表示已有前台请求，稍后重试；请求失败由页面展示，返回 true 防止密集重试。 */
export function useVisibleMaintenanceRefresh(refresh: () => Promise<boolean>): void {
  const latest = useRef(refresh);
  latest.current = refresh;

  useEffect(() => {
    let active = true;
    let running = false;
    let pending = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const visible = () => document.visibilityState !== "hidden";
    const clearTimer = () => { if (timer !== undefined) clearTimeout(timer); timer = undefined; };
    const schedule = (delay = 30_000) => {
      clearTimer();
      if (active && visible()) timer = setTimeout(() => { void run(); }, delay);
    };
    const run = async () => {
      if (!active || !visible()) { pending = true; return; }
      if (running) { pending = true; return; }
      clearTimer();
      running = true;
      pending = false;
      let busy = false;
      try { busy = !(await latest.current()); }
      catch { /* 页面负责错误态；意外拒绝也不产生未处理 Promise。 */ }
      finally {
        running = false;
        if (active) schedule(busy ? 1_000 : pending ? 0 : 30_000);
      }
    };
    const request = () => {
      pending = true;
      if (!running) schedule(0); // 合并同一轮 focus、visibility 和 storage 通知。
    };
    const onVisibility = () => {
      if (visible()) request();
      else clearTimer();
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === MAINTENANCE_CHANGED_STORAGE_KEY && event.newValue) request();
    };
    window.addEventListener("focus", request);
    window.addEventListener(MAINTENANCE_CHANGED_EVENT, request);
    window.addEventListener("storage", onStorage);
    document.addEventListener("visibilitychange", onVisibility);
    schedule();
    return () => {
      active = false;
      clearTimer();
      window.removeEventListener("focus", request);
      window.removeEventListener(MAINTENANCE_CHANGED_EVENT, request);
      window.removeEventListener("storage", onStorage);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);
}
