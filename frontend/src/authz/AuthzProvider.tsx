import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Alert, Button, Spin } from "antd";
import {
  getAuthzSnapshot,
  validateAuthzSnapshot,
  type AuthzSnapshot,
} from "../api/authz";
import { AUTHZ_SESSION_INVALIDATED } from "./events";
import { actionAllowed, ruleAllowed, type Rule } from "./rules";

export interface AuthorizationCapture {
  token: string;
  snapshotVersion: string;
  generation: number;
}
interface AuthzState {
  token: string | null;
  generation: number;
  signal?: AbortSignal;
  isCurrent: (capture: AuthorizationCapture) => boolean;
  invalidate: () => void;
  status: "loading" | "ready" | "error";
  snapshot: AuthzSnapshot | null;
  refresh: () => Promise<void>;
}
const AuthzContext = createContext<AuthzState>({
  token: null,
  generation: 0,
  isCurrent: () => false,
  invalidate: () => {},
  status: "loading",
  snapshot: null,
  refresh: async () => {},
});
export function useAuthz(): AuthzState {
  const value = useContext(AuthzContext);
  // Also check on consumer renders: a same-tab silent token replacement must not
  // lend the previous account's snapshot to a newly mounted route.
  return localStorage.getItem("token") === value.token
    ? value
    : { ...value, status: "loading", snapshot: null };
}

export function AuthzProvider({
  token,
  children,
}: {
  token: string | null;
  children: ReactNode;
}) {
  const [state, setState] = useState<{
    token: string | null;
    status: AuthzState["status"];
    snapshot: AuthzSnapshot | null;
  }>({ token, status: "loading", snapshot: null });
  const sequence = useRef(0);
  const active = useRef<AuthzSnapshot | null>(null);
  const owner = useRef(token);
  owner.current = token;
  const controller = useRef<AbortController>();
  const refresh = useCallback(async () => {
    const request = ++sequence.current;
    active.current = null;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setState({ token, status: "loading", snapshot: null });
    if (!token || localStorage.getItem("token") !== token) return;
    try {
      const { data } = await getAuthzSnapshot(token, abort.signal);
      const snapshot = validateAuthzSnapshot(data);
      if (
        request !== sequence.current ||
        localStorage.getItem("token") !== token
      )
        return;
      active.current = snapshot;
      setState({ token, status: "ready", snapshot });
    } catch {
      if (
        request !== sequence.current ||
        localStorage.getItem("token") !== token
      )
        return;
      setState({ token, status: "error", snapshot: null });
    }
  }, [token]);
  const invalidate = useCallback(() => {
    ++sequence.current;
    active.current = null;
    controller.current?.abort();
    setState({ token, status: "error", snapshot: null });
  }, [token]);
  const isCurrent = useCallback((capture: AuthorizationCapture) =>
    capture.token === owner.current && capture.token === localStorage.getItem("token") &&
    capture.generation === sequence.current && active.current?.version === capture.snapshotVersion &&
    !controller.current?.signal.aborted, []);
  useEffect(() => {
    void refresh();
    const visible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    const invalidated = (event: Event) => {
      if (
        (event as CustomEvent<{ token: string | null }>).detail.token !== token
      )
        return;
      invalidate();
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", visible);
    window.addEventListener(AUTHZ_SESSION_INVALIDATED, invalidated);
    return () => {
      ++sequence.current;
      active.current = null;
      controller.current?.abort();
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", visible);
      window.removeEventListener(AUTHZ_SESSION_INVALIDATED, invalidated);
    };
  }, [refresh, token, invalidate]);
  const current =
    state.token === token && localStorage.getItem("token") === token;
  return (
    <AuthzContext.Provider
      value={{
        token,
        generation: sequence.current,
        signal: controller.current?.signal,
        isCurrent,
        invalidate,
        status: current ? state.status : "loading",
        snapshot: current ? state.snapshot : null,
        refresh,
      }}
    >
      {children}
    </AuthzContext.Provider>
  );
}

export function AuthzUnavailable() {
  const { status, refresh } = useAuthz();
  return status === "loading" ? (
    <div role="status" style={{ padding: 40 }}>
      <Spin /> 正在确认访问权限
    </div>
  ) : (
    <Alert
      type="error"
      showIcon
      message="访问权限暂不可用"
      description="未确认有效权限，已停止受控模块请求。"
      action={<Button onClick={() => void refresh()}>重试权限检查</Button>}
    />
  );
}

/** Consumers never infer actor capabilities from a target's configuration or role. */
export function actorAllowed(
  snapshot: AuthzSnapshot | null,
  capability: import("../api/authz").ActorCapability,
): boolean {
  return (
    snapshot?.schema_version === 1 &&
    snapshot.actor_capabilities?.[capability]?.effect === "allow"
  );
}
export function resourceAllowed(
  snapshot: AuthzSnapshot | null,
  resource: string,
  action: string,
): boolean {
  return actionAllowed(snapshot, resource, action);
}
export function resourceUsesLegacy(
  snapshot: AuthzSnapshot | null,
  resource: string,
): boolean {
  const state = snapshot?.resources?.[resource];
  return (
    snapshot?.schema_version === 1 &&
    !!state &&
    state.runtime_state === "legacy" &&
    state.migration_state !== "retired"
  );
}
export function salesAccess(
  snapshot: AuthzSnapshot | null,
):
  | (Omit<AuthzSnapshot["sales_analysis"], "customer_count"> & {
      customer_count: number | null;
    })
  | null {
  if (!snapshot || snapshot.schema_version !== 1) return null;
  if (snapshot.authorization_mode === "legacy_compat")
    return snapshot.sales_analysis;
  const r = snapshot.resources.sales_analysis;
  if (!r) return null;
  return {
    enabled: !resourceUsesLegacy(snapshot, "sales_analysis"),
    read: ["read_groups", "read_trend", "read_orders", "read_summary"].some(a => resourceAllowed(snapshot, "sales_analysis", a)),
    export: resourceAllowed(snapshot, "sales_analysis", "export"),
    scope:
      r.scope_kind === "all" || r.scope_kind === "assigned_customers"
        ? r.scope_kind
        : "none",
    customer_count: null,
    reason: "未获得销售分析权限",
    field_permissions: r.field_permissions,
  };
}
export function AuthorizationBoundary({
  resource,
  action,
  rule,
  children,
  legacy,
}: {
  resource?: string;
  action?: string;
  rule?: Rule;
  children: ReactNode;
  legacy?: ReactNode;
}) {
  const { snapshot, status } = useAuthz();
  if (status !== "ready") return <AuthzUnavailable />;
  if (resource && resourceUsesLegacy(snapshot, resource) && legacy !== undefined)
    return <>{legacy}</>;
  return (rule ? ruleAllowed(snapshot, rule) : !!resource && !!action && resourceAllowed(snapshot, resource, action)) ? (
    <>{children}</>
  ) : (
    <Alert type="warning" showIcon message="没有此模块操作权限" />
  );
}
