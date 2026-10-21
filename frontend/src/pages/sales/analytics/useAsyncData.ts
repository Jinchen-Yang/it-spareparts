// 页签内取数的最小封装：依赖变化即重拉，迟到的旧响应按序号作废（与页内既有
// summarySeq / ordersSeq 同一防竞态语义），并在落地前比对 localStorage token
// （DL-010：账号切换/登出后的迟到响应不落地，不只判断 mounted）。
// 所属会话守卫（DL-010 返工）：owner 失效时请求前直接拒发并清空旧会话数据
// （错误文案用会话失效专用话术，与加载失败分开）。
// 落地清理（DL-010 落地矩阵）：检测到 owner 失效不能只 return 丢弃迟到响应——
// 还要经 onStale 通知所属页清旧上下文（页级幂等清理），否则旧会话数据会一直
// 留在数据面上冒充当前结果。
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
  /** 页级过期通知（DL-010 落地清理）：本入口检测到 owner 失效（请求前拒发或
   * 迟到响应落地前）时回调，由所属页清旧上下文。幂等；未传则只作本地清空。 */
  onStale?: () => void,
): AsyncState<T> & { retry: () => void } {
  const [state, setState] = useState<AsyncState<T>>({ data: null, loading: enabled, error: null });
  const [tick, setTick] = useState(0);
  const seq = useRef(0);
  // 未下传 owner 的调用方：以本组件挂载会话兜底（owner 语义仍是“首次拥有时”）
  const ownMount = useRef(readSessionToken());
  const sessionOwner = owner !== undefined ? owner : ownMount.current;
  // onStale 存 ref：回调每次渲染都换新引用，不能进 effect 依赖（会引发重复请求）；
  // 落地时读 ref 即取最新闭包，与 DetailDrawer 的 onCloseRef 同一模式
  const onStaleRef = useRef(onStale);
  onStaleRef.current = onStale;
  useEffect(() => {
    if (!enabled) return;
    const mine = ++seq.current;
    // 请求前比对所属会话：失效即拒发并清旧数据（不动序号——迟到的在途响应
    // 仍由下方 sessionStale(token) 作废），同时通知页级清理
    if (sessionStale(sessionOwner)) {
      setState({ data: null, loading: false, error: SESSION_EXPIRED_MESSAGE });
      onStaleRef.current?.();
      return;
    }
    const token = readSessionToken();
    setState((s) => ({ ...s, loading: true, error: null }));
    load()
      .then(({ data }) => {
        if (mine !== seq.current) return;
        // 落地前 owner 失效：丢弃 + 通知页级清旧上下文（不只 return）
        if (sessionStale(token)) { onStaleRef.current?.(); return; }
        setState({ data, loading: false, error: null });
      })
      .catch((err) => {
        if (mine !== seq.current) return;
        if (sessionStale(token)) { onStaleRef.current?.(); return; }
        setState({ data: null, loading: false, error: toMessage(err) });
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 调用方显式给出依赖
  }, [...deps, tick, enabled, sessionOwner]);
  return { ...state, retry: () => setTick((t) => t + 1) };
}
