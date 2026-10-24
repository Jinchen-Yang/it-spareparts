import { useCallback, useRef, type ReactNode } from "react";
import { Alert, Button, Spin } from "antd";
import { api } from "../api";
import type { AuthzSnapshot } from "../api/authz";
import { useAuthz, AuthzUnavailable } from "./AuthzProvider";
import { useAccess } from "./access";
import { ruleAllowed, ruleState, type Rule } from "./rules";
import { AuthorizationCancelled, useCapturedQuery, useCapturedRunner, type AuthorizedRequest } from "./requestLifecycle";
import {
  authorizationPath, authorizationViews, entryRule, validateAuthorizationResolution,
  ContextDenied, type ContextExpectation,
} from "./contextProtocol";
import {
  contextPrecondition, type AuthorizationPrecondition, type BoundAuthorizationContext,
} from "./contextTypes";
import { transportConfig } from "./transport";

export interface ContextInput {
  pathParams?: Readonly<Record<string, string | number>>;
  query?: Readonly<Record<string, unknown>>;
  body?: unknown;
  locator?: ContextExpectation["locator"];
}
const fileIds = new WeakMap<Blob, number>();
let nextFileId = 0;
/** Browser identity only: the server computes the actual bytes/command plan digest. */
export function contextInputKey(input: ContextInput): string {
  function canonical(value: unknown): unknown {
    if (value instanceof Blob) {
      if (!fileIds.has(value)) fileIds.set(value, ++nextFileId);
      return { file: fileIds.get(value), size: value.size, type: value.type };
    }
    if (value instanceof FormData) return [...value.entries()].map(([key, item]) => [key, canonical(item)]);
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(
      ([key, item]) => [key, canonical(item)],
    ));
    return value;
  }
  return JSON.stringify(canonical(input));
}
export function commandBody(body: unknown, precondition: AuthorizationPrecondition): unknown {
  if (body instanceof FormData) {
    const data = new FormData();
    body.forEach((value, key) => { if (key !== "authorization_precondition") data.append(key, value); });
    data.set("authorization_precondition", JSON.stringify(precondition));
    return data;
  }
  if (body === undefined || body === null) return { authorization_precondition: precondition };
  if (typeof body !== "object" || Array.isArray(body) || body instanceof Blob)
    throw new Error("此操作需要已登记的对象或文件表单");
  return { ...body, authorization_precondition: precondition };
}
/** A fixed registry entry controls method/path/requirements. User input supplies only business values. */
export async function requestAuthorizationView(entryId: string, input: ContextInput, capture: AuthorizedRequest) {
  const view = authorizationViews[entryId];
  const url = authorizationPath(entryId, input.pathParams);
  return api.request<unknown>({
    ...transportConfig(capture), method: view.method, url,
    params: { ...input.query, view: "authorization" }, data: input.body,
  });
}
/** Both query transport and explicit commands use this exact two-phase protocol. */
export async function executeContextRequest<T>({ entryId, input, rule = entryRule(entryId), snapshot, capture, current, execute }: {
  entryId: string; input: ContextInput; rule?: Rule; snapshot: AuthzSnapshot;
  capture: AuthorizedRequest; current: () => boolean;
  execute: (capture: AuthorizedRequest, precondition: AuthorizationPrecondition) => Promise<T>;
}): Promise<T> {
  if (!current() || ruleState(snapshot, rule) === "deny") throw new AuthorizationCancelled();
  const response = await requestAuthorizationView(entryId, input, capture);
  if (!current()) throw new AuthorizationCancelled();
  const resolution = validateAuthorizationResolution(response.data, snapshot, { entryId, locator: input.locator, pathParams: input.pathParams });
  if (!ruleAllowed(snapshot, rule, {}, resolution.authorization_context)) throw new ContextDenied();
  if (!current()) throw new AuthorizationCancelled();
  const precondition = contextPrecondition(resolution.authorization_context);
  return execute({ ...capture, authorizationContext: resolution.authorization_context }, precondition);
}
export function useAuthorizationContext({ entryId, input, rule = entryRule(entryId), enabled = true }: {
  entryId: string; input: ContextInput; rule?: Rule; enabled?: boolean;
}) {
  const authz = useAuthz();
  const inputKey = contextInputKey(input);
  const entryState = authz.status === "ready" ? ruleState(authz.snapshot, rule) : "deny";
  const active = enabled && authz.status === "ready" && entryState !== "deny" && !!authorizationViews[entryId];
  const query = useCapturedQuery<BoundAuthorizationContext>({
    active, identityKey: [entryId, rule], queryKey: [entryId, inputKey],
    query: async capture => {
      const result = await requestAuthorizationView(entryId, input, capture);
      if (!authz.snapshot) throw new AuthorizationCancelled();
      const parsed = validateAuthorizationResolution(result.data, authz.snapshot, { entryId, locator: input.locator, pathParams: input.pathParams });
      return { capture, inputKey, value: parsed.authorization_context };
    },
  });
  const context = query.data && authz.isCurrent(query.data.capture) ? query.data : null;
  const allowed = !!context && ruleAllowed(authz.snapshot, rule, {}, context.value);
  return { ...query, context, allowed, entryState,
    status: authz.status !== "ready" ? authz.status : !active ? "denied" as const :
      query.error ? "error" as const : !context ? "loading" as const : allowed ? "ready" as const : "denied" as const,
  };
}
export function useContextAccess(context: BoundAuthorizationContext | null, rule: Rule) {
  return useAccess(rule, { context });
}
/** One captured intention: authorize actual input, then execute once. No conflict retry. */
export function useAuthorizedOperation({ entryId, input, rule = entryRule(entryId) }: {
  entryId: string; input: ContextInput; rule?: Rule;
}) {
  const authz = useAuthz();
  const inputKey = contextInputKey(input);
  const lifetime = JSON.stringify([entryId, inputKey, rule]);
  const latest = useRef(lifetime);
  latest.current = lifetime;
  const run = useCapturedRunner(lifetime);
  return useCallback(async function runOperation<T>(options: {
    execute?: (capture: AuthorizedRequest, precondition: AuthorizationPrecondition) => Promise<T>;
    commit?: (value: T) => void;
    responseType?: "json" | "blob";
  } = {}): Promise<T> {
    return run({
      admit: snapshot => !!authorizationViews[entryId] && ruleState(snapshot, rule) !== "deny",
      queryKey: lifetime,
      isInputCurrent: () => latest.current === lifetime && contextInputKey(input) === inputKey,
      request: async (capture, current) => {
        if (!current() || !authz.snapshot) throw new AuthorizationCancelled();
        return executeContextRequest({ entryId, input, rule, snapshot: authz.snapshot, capture, current,
          execute: async (captured, precondition) => {
            if (options.execute) return options.execute(captured, precondition);
            const view = authorizationViews[entryId];
            const result = await api.request<T>({
              ...transportConfig(captured), method: view.method, url: authorizationPath(entryId, input.pathParams),
              params: view.method === "GET" ? { ...input.query, authorization_precondition: JSON.stringify(precondition) } : input.query,
              data: view.method === "GET" ? undefined : commandBody(input.body, precondition), responseType: options.responseType ?? "json",
            });
            return result.data;
          },
        });
      },
      commit: options.commit,
    });
  }, [run, authz.snapshot, lifetime, entryId, input, rule]);
}
export function ContextBoundary({ resolution, children }: {
  resolution: ReturnType<typeof useAuthorizationContext>; children: ReactNode;
}) {
  const authz = useAuthz();
  if (authz.status !== "ready") return <AuthzUnavailable />;
  if (resolution.status === "loading") return <div role="status"><Spin /> 正在核对对象权限</div>;
  if (resolution.status === "error") return <Alert type="error" showIcon message="对象权限暂不可用"
    description="尚未读取业务数据。请重新核对权限。" action={<Button onClick={resolution.retry}>重新核对</Button>} />;
  if (!resolution.allowed) return <Alert type="warning" showIcon message="没有此对象的访问权限" />;
  return <>{children}</>;
}
