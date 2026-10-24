import { useCallback, useEffect, useRef, useState } from "react";
import { useAuthz, type AuthorizationCapture } from "./AuthzProvider";
import type { AuthzSnapshot } from "../api/authz";
import type { AuthorizationContext } from "./contextTypes";

export interface AuthorizedRequest extends AuthorizationCapture {
  signal: AbortSignal;
  queryKey: string;
  authorizationContext?: AuthorizationContext;
}
export class AuthorizationCancelled extends Error {
  constructor() { super("访问权限或请求已变化"); this.name = "AuthorizationCancelled"; }
}
export function isUnavailable(error: unknown): boolean {
  return (error as { response?: { status?: number } })?.response?.status === 503;
}
function linkAbort(parent: AbortSignal | undefined, child: AbortController) {
  const abort = () => child.abort();
  if (parent?.aborted) abort();
  parent?.addEventListener("abort", abort, { once: true });
  return () => parent?.removeEventListener("abort", abort);
}
/** Shared lifecycle only. Public callers must enter through access or a registered context adapter. */
export function useCapturedRunner(lifetimeKey?: string) {
  const authz = useAuthz();
  const controllers = useRef(new Set<AbortController>());
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; controllers.current.forEach(c => c.abort()); };
  }, []);
  useEffect(() => () => controllers.current.forEach(c => c.abort()), [lifetimeKey]);
  return useCallback(async function runCaptured<T>(options: {
    admit: (snapshot: AuthzSnapshot) => boolean;
    queryKey?: string;
    isInputCurrent?: () => boolean;
    request: (capture: AuthorizedRequest, current: () => boolean) => Promise<T>;
    commit?: (value: T) => void;
  }): Promise<T> {
    if (authz.status !== "ready" || !authz.token || !authz.snapshot || !options.admit(authz.snapshot))
      throw new AuthorizationCancelled();
    const capture: AuthorizationCapture = {
      token: authz.token, snapshotVersion: authz.snapshot.version, generation: authz.generation,
    };
    const controller = new AbortController(), unlink = linkAbort(authz.signal, controller);
    controllers.current.add(controller);
    const current = () => mounted.current && !controller.signal.aborted && authz.isCurrent(capture) &&
      (options.isInputCurrent?.() ?? true);
    try {
      if (!current()) throw new AuthorizationCancelled();
      const result = await options.request({ ...capture, signal: controller.signal, queryKey: options.queryKey || "command" }, current);
      if (!current()) throw new AuthorizationCancelled();
      options.commit?.(result);
      return result;
    } catch (error) {
      if (!current()) throw new AuthorizationCancelled();
      if (isUnavailable(error)) authz.invalidate();
      throw error;
    } finally { unlink(); controllers.current.delete(controller); }
  }, [authz.token, authz.status, authz.snapshot, authz.generation, authz.signal, authz.isCurrent, authz.invalidate]);
}
/** One implementation of first-render projection, cancellation and stale completion handling. */
export function useCapturedQuery<T>({ active, identityKey, queryKey, query, onStale, refreshKey, retainWhileRefreshing = false }: {
  active: boolean;
  identityKey: unknown;
  queryKey: readonly unknown[];
  query: (capture: AuthorizedRequest) => Promise<T>;
  onStale?: () => void;
  refreshKey?: unknown;
  retainWhileRefreshing?: boolean;
}) {
  const authz = useAuthz();
  const [attempt, setAttempt] = useState(0);
  const nextAttempt = useRef(attempt);
  const retryWaiters = useRef(new Map<string, ((success: boolean) => void)[]>());
  const mounted = useRef(true);
  const settleRetry = (key: string, success: boolean) => {
    retryWaiters.current.get(key)?.forEach(resolve => resolve(success));
    retryWaiters.current.delete(key);
  };
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      retryWaiters.current.forEach(waiters => waiters.forEach(resolve => resolve(false)));
      retryWaiters.current.clear();
    };
  }, []);
  const requestKey = JSON.stringify(queryKey);
  const baseIdentity = JSON.stringify([authz.token, authz.snapshot?.version, authz.generation, requestKey, identityKey]);
  const identity = JSON.stringify([baseIdentity, attempt, refreshKey]);
  const [state, setState] = useState<{ identity: string; baseIdentity: string; data: T | null; error: unknown; loading: boolean }>();
  const latest = useRef({ identity, active, query, onStale });
  latest.current = { identity, active, query, onStale };
  useEffect(() => {
    for (const key of retryWaiters.current.keys()) if (!active || key !== identity) settleRetry(key, false);
  }, [identity, active]);
  useEffect(() => {
    if (!active || authz.status !== "ready" || !authz.token || !authz.snapshot) return;
    const capture: AuthorizationCapture = {
      token: authz.token, snapshotVersion: authz.snapshot.version, generation: authz.generation,
    };
    const controller = new AbortController(), unlink = linkAbort(authz.signal, controller);
    const current = () => mounted.current && !controller.signal.aborted && latest.current.active &&
      latest.current.identity === identity && authz.isCurrent(capture);
    const discard = () => {
      if (!controller.signal.aborted && latest.current.identity === identity &&
          localStorage.getItem("token") !== capture.token) {
        setState({ identity: "invalid", baseIdentity: "invalid", data: null, error: null, loading: false });
        latest.current.onStale?.();
      }
    };
    setState(previous => ({ identity, baseIdentity, data: retainWhileRefreshing && previous?.baseIdentity === baseIdentity ? previous.data : null, error: null, loading: true }));
    void Promise.resolve().then(() => {
      if (!current()) throw new AuthorizationCancelled();
      return latest.current.query({ ...capture, signal: controller.signal, queryKey: requestKey });
    }).then(data => {
      if (current()) { setState({ identity, baseIdentity, data, error: null, loading: false }); settleRetry(identity, true); }
      else { discard(); settleRetry(identity, false); }
    }, error => {
      if (!current()) { discard(); settleRetry(identity, false); return; }
      setState({ identity, baseIdentity, data: null, error, loading: false });
      settleRetry(identity, false);
      if (isUnavailable(error)) authz.invalidate();
    });
    return () => { controller.abort(); unlink(); settleRetry(identity, false); };
  }, [identity, active]);
  const refreshing = retainWhileRefreshing && state?.baseIdentity === baseIdentity && state?.identity !== identity;
  const visible = active && authz.status === "ready" && (state?.identity === identity || refreshing) ? state : undefined;
  const retry = useCallback(() => {
      if (!mounted.current || !latest.current.active) return Promise.resolve(false);
      retryWaiters.current.forEach(waiters => waiters.forEach(resolve => resolve(false)));
      retryWaiters.current.clear();
      const next = ++nextAttempt.current;
      const key = JSON.stringify([baseIdentity, next, refreshKey]);
      const promise = new Promise<boolean>(resolve => retryWaiters.current.set(key, [resolve]));
      setAttempt(next);
      return promise;
    }, [baseIdentity, refreshKey]);
  return {
    data: visible?.data ?? null,
    error: refreshing ? null : visible?.error ?? null,
    loading: active && (refreshing || (visible?.loading ?? true)),
    retry,
  };
}
/** Preserve a server business error without treating it as an authorization grant. */
export function authorizationErrorText(error: unknown, fallback: string): string {
  if (isUnavailable(error)) return "权限服务暂不可用，请重新检查访问权限";
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string") return detail.message;
  return error instanceof Error ? error.message : fallback;
}
