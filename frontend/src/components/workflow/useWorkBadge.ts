import { useEffect, useState } from "react";
import { getWorkBadge, type WorkBadge } from "../../api/workItems";

const BADGE_REFRESH_EVENT = "workflow:badge-refresh";

/** 各使用方共享刷新信号，待办动作完成后菜单角标也会更新。 */
export function refreshWorkBadges() {
  window.dispatchEvent(new Event(BADGE_REFRESH_EVENT));
}

export function useWorkBadge(): WorkBadge {
  const [counts, setCounts] = useState<WorkBadge>({ assigned: 0, pool: 0, claimed: 0 });

  useEffect(() => {
    let interval: ReturnType<typeof setInterval> | undefined;
    let controller: AbortController | undefined;

    const refresh = async () => {
      if (document.visibilityState === "hidden") return;
      controller?.abort();
      const request = new AbortController();
      controller = request;
      try {
        const data = await getWorkBadge(request.signal);
        if (!request.signal.aborted) setCounts(data);
      } catch {
        // 后台轮询失败保留上次计数，下一次可见或轮询时重试。
      }
    };
    const updatePolling = () => {
      clearInterval(interval);
      controller?.abort();
      if (document.visibilityState === "hidden") return;
      void refresh();
      interval = setInterval(() => void refresh(), 60_000);
    };

    updatePolling();
    document.addEventListener("visibilitychange", updatePolling);
    window.addEventListener(BADGE_REFRESH_EVENT, refresh);
    return () => {
      clearInterval(interval);
      controller?.abort();
      document.removeEventListener("visibilitychange", updatePolling);
      window.removeEventListener(BADGE_REFRESH_EVENT, refresh);
    };
  }, []);

  return counts;
}
