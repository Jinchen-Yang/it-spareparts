import { useCallback, type ComponentType, type ReactNode } from "react";
import { Alert, Button } from "antd";
import { AuthzUnavailable, useAuthz } from "./AuthzProvider";
import { AuthorizationCancelled, isUnavailable, useCapturedQuery, useCapturedRunner, type AuthorizedRequest } from "./requestLifecycle";
import type { BoundAuthorizationContext } from "./contextTypes";
export { AuthorizationCancelled, type AuthorizedRequest } from "./requestLifecycle";
import { actionRule, routeRule, ruleAllowed, ruleState, surfaceRule, type Rule } from "./rules";
import { NAV_ITEMS, DETAIL_ROUTES, readPermissionMap } from "../nav";

export interface AccessOptions { legacy?: Readonly<Record<string, boolean>>; context?: BoundAuthorizationContext | null }
export function useAccess(rule: Rule, options: AccessOptions = {}) {
  const authz = useAuthz();
  const context = options.context && authz.isCurrent(options.context.capture) ? options.context.value : null;
  const entryState = authz.status === "ready" ? ruleState(authz.snapshot, rule, options.legacy, context) : "deny";
  return {
    ...authz,
    allowed: entryState === "allow",
    entryState,
    rule,
    legacy: options.legacy,
  };
}
export function useSurface(id: string, options: AccessOptions = {}) {
  return useAccess(surfaceRule(id), options);
}
/** Admission only: resolve permits an authorization shell, never a fact or command. */
export function useContextAdmission() {
  const authz = useAuthz();
  const state = (rule: Rule, legacyAllowed = false, context?: BoundAuthorizationContext | null) => {
    if (authz.status !== "ready") return "deny" as const;
    const legacy = Object.fromEntries(rule.anyOf.flat().map(need => [need.resource_id, legacyAllowed]));
    const bound = context && authz.isCurrent(context.capture) ? context.value : null;
    return ruleState(authz.snapshot, rule, legacy, bound);
  };
  return { state, surface: (id: string, legacyAllowed = false, context?: BoundAuthorizationContext | null) =>
    state(surfaceRule(id), legacyAllowed, context) };
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
export function useModuleAccess(resource: string, context?: BoundAuthorizationContext | null) {
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
      authz.snapshot.resources[resource].field_permissions.includes(id) === true &&
      (context === undefined || !!context && authz.isCurrent(context.capture) &&
        context.value.catalog_version === authz.snapshot.catalog_version &&
        context.value.authorization_epoch === authz.snapshot.authorization_epoch &&
        (context.value.resources[resource]?.grant_version ?? 0) === authz.snapshot.resources[resource].grant_version &&
        context.value.resources[resource]?.field_permissions.includes(id)),
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
    if (access.entryState === "deny") return <Alert type="warning" message="没有此页面访问权限" />;
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
/** Imperative writes/downloads have one attempt. Contexts never replace the actor snapshot. */
export function useAuthorizedRunner() {
  const authz = useAuthz();
  const run = useCapturedRunner();
  return useCallback(async function runAuthorized<T>(options: {
    rule: Rule;
    legacy?: AccessOptions["legacy"];
    context?: BoundAuthorizationContext | null;
    queryKey?: string;
    isInputCurrent?: () => boolean;
    request: (context: AuthorizedRequest) => Promise<T>;
    commit?: (value: T) => void;
  }): Promise<T> {
    const context = options.context;
    return run({
      admit: snapshot => (!context || authz.isCurrent(context.capture)) &&
        ruleAllowed(snapshot, options.rule, options.legacy, context?.value),
      queryKey: options.queryKey,
      isInputCurrent: () => (!context || authz.isCurrent(context.capture)) && (options.isInputCurrent?.() ?? true),
      request: capture => options.request({ ...capture, authorizationContext: context?.value }),
      commit: options.commit,
    });
  }, [run, authz.isCurrent]);
}
/** No previous data is returned while owner, object, version, query, rule or permission differs. */
export function useAuthorizedQuery<T>({ rule, legacy, context, queryKey, query, enabled = true, onStale }: {
  rule: Rule;
  legacy?: AccessOptions["legacy"];
  context?: BoundAuthorizationContext | null;
  queryKey: readonly unknown[];
  query: (context: AuthorizedRequest) => Promise<T>;
  enabled?: boolean;
  onStale?: () => void;
}) {
  const access = useAccess(rule, { legacy, context });
  const result = useCapturedQuery({
    active: enabled && access.allowed,
    identityKey: [rule, legacy, context?.inputKey, context?.value.context],
    queryKey,
    query: capture => query({ ...capture, authorizationContext: context?.value }),
    onStale,
  });
  return { ...result, allowed: access.allowed };
}
export function AuthorizedQueryError({ error, retry, message = "数据读取失败" }: { error: unknown; retry: () => void; message?: string }) {
  if (!error || error instanceof AuthorizationCancelled) return null;
  return <Alert type="error" showIcon message={isUnavailable(error) ? "权限服务暂不可用" : message}
    description={isUnavailable(error) ? "已停止请求，请重新检查访问权限。" : "本次读取未完成，未保留上次数据。"}
    action={!isUnavailable(error) && <Button onClick={retry}>重试</Button>} />;
}
