import { useCallback, useEffect, useRef, useState, type ComponentType, type ReactNode } from "react";
import { Alert, Button } from "antd";
import { AuthzUnavailable, useAuthz, type AuthorizationCapture } from "./AuthzProvider";
import { actionRule, routeRule, ruleAllowed, surfaceRule, type Rule } from "./rules";
import { NAV_ITEMS, DETAIL_ROUTES, readPermissionMap } from "../nav";

export interface AccessOptions { legacy?: Readonly<Record<string, boolean>> }
export function useAccess(rule: Rule, options: AccessOptions = {}) {
  const authz = useAuthz();
  return {
    ...authz,
    allowed: authz.status === "ready" && ruleAllowed(authz.snapshot, rule, options.legacy),
    rule,
    legacy: options.legacy,
  };
}
export function useSurface(id: string, options: AccessOptions = {}) {
  return useAccess(surfaceRule(id), options);
}
export function useSurfaceCheck() {
  const authz = useAuthz();
  return (id: string, oldAllowed = false) => {
    const rule = surfaceRule(id);
    const legacy = Object.fromEntries(rule.anyOf.flat().map(need => [need.resource_id, oldAllowed]));
    return authz.status === "ready" && ruleAllowed(authz.snapshot, rule, legacy);
  };
}
/** A convenience view over the same actor snapshot, not a second capability store. */
export function useModuleAccess(resource: string) {
  const authz = useAuthz();
  const legacy = authz.status === "ready" && authz.snapshot?.resources[resource]?.runtime_state === "legacy" &&
    authz.snapshot.resources[resource].migration_state !== "retired";
  const allowed = (rule: Rule, oldAllowed = false) => authz.status === "ready" &&
    ruleAllowed(authz.snapshot, rule, { [resource]: oldAllowed });
  return { ...authz, legacy,
    action: (id: string, oldAllowed = false) => allowed(actionRule(resource, id), oldAllowed),
    surface: (id: string, oldAllowed = false) => allowed(surfaceRule(`${resource}.${id}`), oldAllowed),
    field: (id: string, oldAllowed = false) => legacy ? oldAllowed : authz.status === "ready" &&
      authz.snapshot?.resources[resource]?.runtime_state === "enabled" &&
      authz.snapshot.resources[resource].field_permissions.includes(id) === true,
  };
}
/** Route/menu metadata stays in nav; capability rules come only from the generated manifest. */
export function legacyRouteAllowed(path: string): boolean {
  const route = [...NAV_ITEMS, ...DETAIL_ROUTES].find(item => item.path === path);
  if (!route) return false;
  const permissions = readPermissionMap();
  return localStorage.getItem("role") === "admin" ||
    ("visibleWhen" in route && route.visibleWhen ? route.visibleWhen() : route.perm ? permissions[route.perm] === true :
      route.anyPerm ? route.anyPerm.some(key => permissions[key] === true) : false);
}
export function useRouteAccess(path: string) {
  const rule = routeRule(path), oldAllowed = legacyRouteAllowed(path);
  return useAccess(rule, {legacy: Object.fromEntries(rule.anyOf.flat().map(need => [need.resource_id, oldAllowed]))});
}
/** Remount the page lifetime before painting any data from a prior authorization snapshot. */
export function withRouteAccess(Component: ComponentType, path: string) {
  return function ControlledPage() {
    const access = useRouteAccess(path);
    if (access.status !== "ready") return <AuthzUnavailable />;
    if (!access.allowed) return <Alert type="warning" message="没有此页面访问权限" />;
    return <Component key={`${access.token}:${access.generation}:${access.snapshot?.version}`} />;
  };
}
export function AccessBoundary({ rule, legacy, children, fallback = null }: {
  rule: Rule; legacy?: AccessOptions["legacy"]; children: ReactNode; fallback?: ReactNode;
}) {
  const access = useAccess(rule, { legacy });
  if (access.status !== "ready") return <AuthzUnavailable />;
  return <>{access.allowed ? children : fallback}</>;
}
export interface AuthorizedRequest extends AuthorizationCapture {
  signal: AbortSignal;
  queryKey: string;
}
export class AuthorizationCancelled extends Error {
  constructor() { super("访问权限或请求已变化"); this.name = "AuthorizationCancelled"; }
}
function isUnavailable(error: unknown): boolean {
  return (error as { response?: { status?: number } })?.response?.status === 503;
}
function linkAbort(parent: AbortSignal | undefined, child: AbortController) {
  const abort = () => child.abort();
  if (parent?.aborted) abort();
  parent?.addEventListener("abort", abort, { once: true });
  return () => parent?.removeEventListener("abort", abort);
}
/** Imperative writes/downloads have one attempt. All completion side effects belong in commit. */
export function useAuthorizedRunner() {
  const authz = useAuthz();
  const controllers = useRef(new Set<AbortController>());
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; controllers.current.forEach(c => c.abort()); };
  }, []);
  return useCallback(async function runAuthorized<T>(options: {
    rule: Rule;
    legacy?: AccessOptions["legacy"];
    queryKey?: string;
    request: (context: AuthorizedRequest) => Promise<T>;
    commit?: (value: T) => void;
  }): Promise<T> {
    if (authz.status !== "ready" || !authz.token || !authz.snapshot ||
        !ruleAllowed(authz.snapshot, options.rule, options.legacy)) throw new AuthorizationCancelled();
    const capture: AuthorizationCapture = {
      token: authz.token, snapshotVersion: authz.snapshot.version, generation: authz.generation,
    };
    const controller = new AbortController(), unlink = linkAbort(authz.signal, controller);
    controllers.current.add(controller);
    const current = () => mounted.current && !controller.signal.aborted && authz.isCurrent(capture);
    try {
      if (!current()) throw new AuthorizationCancelled();
      const result = await options.request({ ...capture, signal: controller.signal, queryKey: options.queryKey || "command" });
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
/** No previous data is returned while owner, version, query, rule or permission differs. */
export function useAuthorizedQuery<T>({ rule, legacy, queryKey, query, enabled = true, onStale }: {
  rule: Rule;
  legacy?: AccessOptions["legacy"];
  queryKey: readonly unknown[];
  query: (context: AuthorizedRequest) => Promise<T>;
  enabled?: boolean;
  onStale?: () => void;
}) {
  const access = useAccess(rule, { legacy });
  const [attempt, setAttempt] = useState(0);
  const requestKey = JSON.stringify(queryKey);
  const ruleKey = JSON.stringify([rule, legacy]);
  const identity = JSON.stringify([access.token, access.snapshot?.version, access.generation, requestKey, ruleKey, attempt]);
  const active = enabled && access.allowed;
  const [state, setState] = useState<{ identity: string; data: T | null; error: unknown; loading: boolean }>();
  const latest = useRef({ identity, active, query, onStale });
  latest.current = { identity, active, query, onStale };
  useEffect(() => {
    if (!active || !access.token || !access.snapshot) return;
    const capture: AuthorizationCapture = {
      token: access.token, snapshotVersion: access.snapshot.version, generation: access.generation,
    };
    const controller = new AbortController(), unlink = linkAbort(access.signal, controller);
    const current = () => !controller.signal.aborted && latest.current.active &&
      latest.current.identity === identity && access.isCurrent(capture);
    const discard = () => {
      // A silent same-tab token replacement may not have caused any React render yet.
      // Make that invalidation observable without touching the new owner's session.
      if (!controller.signal.aborted && latest.current.identity === identity &&
          localStorage.getItem("token") !== capture.token) {
        setState({ identity: "invalid", data: null, error: null, loading: false });
        latest.current.onStale?.();
      }
    };
    setState({ identity, data: null, error: null, loading: true });
    void Promise.resolve().then(() => {
      if (!current()) throw new AuthorizationCancelled();
      return latest.current.query({ ...capture, signal: controller.signal, queryKey: requestKey });
    }).then(data => {
      if (current()) setState({ identity, data, error: null, loading: false });
      else discard();
    }, error => {
      if (!current()) { discard(); return; }
      setState({ identity, data: null, error, loading: false });
      if (isUnavailable(error)) access.invalidate();
    });
    return () => { controller.abort(); unlink(); };
  }, [identity, active]);
  const visible = active && state?.identity === identity ? state : undefined;
  return {
    data: visible?.data ?? null,
    error: visible?.error ?? null,
    loading: active && (visible?.loading ?? true),
    allowed: access.allowed,
    retry: () => { if (latest.current.active) setAttempt(a => a + 1); },
  };
}
export function AuthorizedQueryError({ error, retry, message = "数据读取失败" }: { error: unknown; retry: () => void; message?: string }) {
  if (!error || error instanceof AuthorizationCancelled) return null;
  return <Alert type="error" showIcon message={isUnavailable(error) ? "权限服务暂不可用" : message}
    description={isUnavailable(error) ? "已停止请求，请重新检查访问权限。" : "本次读取未完成，未保留上次数据。"}
    action={!isUnavailable(error) && <Button onClick={retry}>重试</Button>} />;
}
