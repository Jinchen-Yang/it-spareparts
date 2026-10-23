import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Alert, Button, Spin } from "antd";
import { getAuthzSnapshot, validateAuthzSnapshot, type AuthzSnapshot } from "../api/authz";
import { AUTHZ_SESSION_INVALIDATED } from "./events";

interface AuthzState {
  status: "loading" | "ready" | "error";
  snapshot: AuthzSnapshot | null;
  refresh: () => Promise<void>;
}
const AuthzContext = createContext<AuthzState & { token: string | null }>({ token: null, status: "loading", snapshot: null, refresh: async () => {} });
export function useAuthz(): AuthzState {
  const value = useContext(AuthzContext);
  // Also check on consumer renders: a same-tab silent token replacement must not
  // lend the previous account's snapshot to a newly mounted route.
  return localStorage.getItem("token") === value.token ? value
    : { status: "loading", snapshot: null, refresh: value.refresh };
}

export function AuthzProvider({ token, children }: { token: string | null; children: ReactNode }) {
  const [state, setState] = useState<{ token: string | null; status: AuthzState["status"]; snapshot: AuthzSnapshot | null }>({ token, status: "loading", snapshot: null });
  const sequence = useRef(0);
  const controller = useRef<AbortController>();
  const refresh = useCallback(async () => {
    const request = ++sequence.current;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setState({ token, status: "loading", snapshot: null });
    if (!token || localStorage.getItem("token") !== token) return;
    try {
      const { data } = await getAuthzSnapshot(token, abort.signal);
      const snapshot = validateAuthzSnapshot(data);
      if (request !== sequence.current || localStorage.getItem("token") !== token) return;
      setState({ token, status: "ready", snapshot });
    } catch {
      if (request !== sequence.current || localStorage.getItem("token") !== token) return;
      setState({ token, status: "error", snapshot: null });
    }
  }, [token]);
  useEffect(() => {
    void refresh();
    const visible = () => { if (document.visibilityState === "visible") void refresh(); };
    const invalidated = (event: Event) => {
      if ((event as CustomEvent<{ token: string | null }>).detail.token !== token) return;
      ++sequence.current;
      controller.current?.abort();
      setState({ token, status: "error", snapshot: null });
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", visible);
    window.addEventListener(AUTHZ_SESSION_INVALIDATED, invalidated);
    return () => {
      ++sequence.current;
      controller.current?.abort();
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", visible);
      window.removeEventListener(AUTHZ_SESSION_INVALIDATED, invalidated);
    };
  }, [refresh, token]);
  const current = state.token === token && localStorage.getItem("token") === token;
  return <AuthzContext.Provider value={{ token, status: current ? state.status : "loading", snapshot: current ? state.snapshot : null, refresh }}>{children}</AuthzContext.Provider>;
}

export function AuthzUnavailable() {
  const { status, refresh } = useAuthz();
  return status === "loading"
    ? <div role="status" style={{ padding: 40 }}><Spin /> 正在确认销售分析权限</div>
    : <Alert type="error" showIcon message="销售分析权限暂不可用"
      description="未确认有效权限，已停止销售分析请求。" action={<Button onClick={() => void refresh()}>重试权限检查</Button>} />;
}
