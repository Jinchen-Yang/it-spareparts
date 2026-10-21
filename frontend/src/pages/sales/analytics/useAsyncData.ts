// 页签内取数的最小封装：依赖变化即重拉，迟到的旧响应按序号作废（与页内既有
// summarySeq / ordersSeq 同一防竞态语义）。错误文案由调用方给的 toMessage 生成。
import { useEffect, useRef, useState } from "react";

export interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

export function useAsyncData<T>(
  load: () => Promise<{ data: T }>,
  deps: unknown[],
  toMessage: (err: unknown) => string,
  enabled = true,
): AsyncState<T> & { retry: () => void } {
  const [state, setState] = useState<AsyncState<T>>({ data: null, loading: enabled, error: null });
  const [tick, setTick] = useState(0);
  const seq = useRef(0);
  useEffect(() => {
    if (!enabled) return;
    const mine = ++seq.current;
    setState((s) => ({ ...s, loading: true, error: null }));
    load()
      .then(({ data }) => { if (mine === seq.current) setState({ data, loading: false, error: null }); })
      .catch((err) => { if (mine === seq.current) setState({ data: null, loading: false, error: toMessage(err) }); });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 调用方显式给出依赖
  }, [...deps, tick, enabled]);
  return { ...state, retry: () => setTick((t) => t + 1) };
}
