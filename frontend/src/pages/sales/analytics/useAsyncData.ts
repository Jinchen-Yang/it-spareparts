// 页签内取数的最小封装：依赖变化即重拉，迟到的旧响应按序号作废（与页内既有
// summarySeq / ordersSeq 同一防竞态语义），并在落地前比对 localStorage token
// （DL-010：账号切换/登出后的迟到响应不落地，不只判断 mounted）。
// 所属会话守卫（DL-010 返工）：owner 失效时请求前直接拒发并清空旧会话数据
// （错误文案用会话失效专用话术，与加载失败分开）。
// 错误文案由调用方给的 toMessage 生成。
import { useEffect, useRef, useState } from "react";
import {
  readSessionToken,
  SESSION_EXPIRED_MESSAGE,
  sessionStale,
} from "../../../utils/session";

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
  /** 所属会话 owner（页级下传）；未给则取本组件挂载会话。 */
  owner?: string | null,
): AsyncState<T> & { retry: () => void } {
  const [state, setState] = useState<AsyncState<T>>({ data: null, loading: enabled, error: null });
  const [tick, setTick] = useState(0);
  const seq = useRef(0);
  // 未下传 owner 的调用方：以本组件挂载会话兜底（owner 语义仍是“首次拥有时”）
  const ownMount = useRef(readSessionToken());
  const sessionOwner = owner !== undefined ? owner : ownMount.current;
  useEffect(() => {
    if (!enabled) return;
    const mine = ++seq.current;
    // 请求前比对所属会话：失效即拒发并清旧数据（不动序号——迟到的在途响应
    // 仍由下方 sessionStale(token) 作废）
    if (sessionStale(sessionOwner)) {
      setState({ data: null, loading: false, error: SESSION_EXPIRED_MESSAGE });
      return;
    }
    const token = readSessionToken();
    setState((s) => ({ ...s, loading: true, error: null }));
    load()
      .then(({ data }) => {
        if (mine !== seq.current || sessionStale(token)) return;
        setState({ data, loading: false, error: null });
      })
      .catch((err) => {
        if (mine !== seq.current || sessionStale(token)) return;
        setState({ data: null, loading: false, error: toMessage(err) });
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 调用方显式给出依赖
  }, [...deps, tick, enabled, sessionOwner]);
  return { ...state, retry: () => setTick((t) => t + 1) };
}
