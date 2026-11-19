// Cache lifetime is one mounted, authenticated dashboard. Failed reads are retryable.
import { useEffect, useRef, useState } from "react";
import { sessionStale } from "../../../utils/session";

export function makeReadCache<T>(capacity = 24, ttl = 60_000) {
  const entries = new Map<string, { until: number; result: Promise<T> }>();
  return {
    clear: () => entries.clear(),
    get(key: string, load: () => Promise<T>) {
      const now = Date.now();
      const prior = entries.get(key);
      if (prior && prior.until > now) {
        entries.delete(key);
        entries.set(key, prior);
        return prior.result;
      }
      const result = load();
      entries.set(key, { until: now + ttl, result });
      if (entries.size > capacity) entries.delete(entries.keys().next().value!);
      void result.catch(() => {
        if (entries.get(key)?.result === result) entries.delete(key);
      });
      return result;
    },
  };
}

export function useDashboardRead<T>(
  key: string | null,
  read: () => Promise<T>,
  owner: string | null,
  stale: () => void,
) {
  const [snapshot, setSnapshot] = useState<{ key: string; data?: T; error?: string }>();
  const callbacks = useRef({ read, stale });
  callbacks.current = { read, stale };
  useEffect(() => {
    if (key === null) return;
    if (sessionStale(owner)) {
      callbacks.current.stale();
      return;
    }
    let active = true;
    const accept = () => {
      if (!active) return false;
      if (!sessionStale(owner)) return true;
      callbacks.current.stale();
      return false;
    };
    void callbacks.current
      .read()
      .then((data) => {
        if (accept()) setSnapshot({ key, data });
      })
      .catch((error) => {
        if (accept())
          setSnapshot({
            key,
            error:
              typeof error?.response?.data?.detail === "string"
                ? error.response.data.detail
                : "加载失败，请重试",
          });
      });
    return () => {
      active = false;
    };
  }, [key, owner]);
  return snapshot?.key === key && !sessionStale(owner) ? snapshot : undefined;
}
