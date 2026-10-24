import { useEffect, useMemo, useRef } from "react";
import type { AxiosRequestConfig, AxiosResponse } from "axios";
import { api } from "../api";
import { useAuthz } from "./AuthzProvider";
import { AuthorizationCancelled, useCapturedRunner } from "./requestLifecycle";
import { authorizationPath, authorizationViews, contextEntries, entryRule,
  validateContextualPayload, ContextDenied, ContextProtocolError } from "./contextProtocol";
import { commandBody, contextInputKey, executeContextRequest, type ContextInput } from "./context";
import { ruleAllowed, ruleState, vocabulary, type Rule } from "./rules";
import type { AuthorizationContext } from "./contextTypes";
import { transportConfig } from "./transport";

import { createDomainApi, type ContextRequestTransport } from "./domainApi";
export type { ContextRequestTransport, ContextualPayload } from "./domainApi";
/** This index comes from backend entry contracts; it is not a resource-to-URL authorization table. */
export function registeredEntry(method: string, url: string) {
  const path = `/api${url.replace(/^\/api(?=\/)/, "").split("?")[0]}`;
  const candidates = Object.entries(authorizationViews).filter(([, view]) => view.method === method)
    .sort(([, a], [, b]) => (a.path.match(/\{/g)?.length ?? 0) - (b.path.match(/\{/g)?.length ?? 0));
  for (const [entryId, view] of candidates) {
    const names: string[] = [];
    const pattern = view.path.split(/(\{[^}]+\})/).map(part => {
      if (part.startsWith("{")) { names.push(part.slice(1, -1)); return "([^/]+)"; }
      return part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }).join("");
    const match = new RegExp(`^${pattern}$`).exec(path);
    if (match) return { entryId, pathParams: Object.fromEntries(names.map((name, index) => [name, decodeURIComponent(match[index + 1])])) };
  }
  throw new ContextProtocolError();
}
/** Captured at the component lifecycle; late callbacks cannot acquire a newer identity. */
export function useContextTransport(lifetimeKey: string, legacyAllowed = false, resolvedRule?: (entryId: string, context: AuthorizationContext) => Rule): ContextRequestTransport {
  const authz = useAuthz();
  const run = useCapturedRunner(lifetimeKey);
  const lifetime = useRef<string | null>(lifetimeKey);
  lifetime.current = lifetimeKey;
  useEffect(() => {
    lifetime.current = lifetimeKey;
    return () => { lifetime.current = null; };
  }, []);
  return useMemo(() => {
    const owner = authz.token && authz.snapshot ? { token: authz.token, snapshotVersion: authz.snapshot.version, generation: authz.generation } : null;
    return {
      isCurrent: () => !!owner && lifetime.current === lifetimeKey && authz.isCurrent(owner),
      request: async <T,>(config: AxiosRequestConfig): Promise<AxiosResponse<T>> => {
        if (!authz.snapshot || !owner || lifetime.current !== lifetimeKey || !authz.isCurrent(owner)) throw new AuthorizationCancelled();
        const method = String(config.method ?? "GET").toUpperCase();
        const registered = registeredEntry(method, String(config.url ?? ""));
        const rule = entryRule(registered.entryId);
        const legacy = Object.fromEntries(rule.anyOf.flat().map(need => [need.resource_id, legacyAllowed]));
        const needs = rule.anyOf.flat();
        const allLegacy = needs.length > 0 && needs.every(need =>
          authz.snapshot!.resources[need.resource_id]?.runtime_state === "legacy" &&
          authz.snapshot!.resources[need.resource_id]?.migration_state !== "retired");
        // Existing helpers sometimes encode selectors in the URL. The view and formal request must share them.
        const search = String(config.url ?? "").split("?")[1] ?? "";
        const urlQuery: Record<string, string | string[]> = {};
        for (const [key, value] of new URLSearchParams(search)) {
          const previous = urlQuery[key];
          urlQuery[key] = previous === undefined ? value : Array.isArray(previous) ? [...previous, value] : [previous, value];
        }
        const query = { ...urlQuery, ...config.params };
        const currentInput = (): ContextInput => ({ pathParams: registered.pathParams, query, body: config.data, headers: config.headers });
        const input = currentInput();
        const inputKey = contextInputKey(input);
        return run({
          admit: snapshot => ruleState(snapshot, rule, legacy) !== "deny",
          queryKey: `${registered.entryId}:${inputKey}`,
          isInputCurrent: () => lifetime.current === lifetimeKey && authz.isCurrent(owner) && !config.signal?.aborted && contextInputKey(currentInput()) === inputKey,
          request: async (capture, current) => {
            const child = new AbortController();
            const abort = () => child.abort();
            capture.signal.addEventListener("abort", abort, { once: true });
            config.signal?.addEventListener?.("abort", abort, { once: true });
            if (capture.signal.aborted || config.signal?.aborted) child.abort();
            const captured = { ...capture, signal: child.signal };
            try {
              if (allLegacy) return await api.request<T>({ ...config, auth: undefined, ...transportConfig(captured, input.headers) });
              return await executeContextRequest({ entryId: registered.entryId, input, snapshot: authz.snapshot!,
                capture: captured, current, execute: async (authorized, precondition) => {
                  if (resolvedRule && !ruleAllowed(authz.snapshot, resolvedRule(registered.entryId, authorized.authorizationContext!), legacy, authorized.authorizationContext))
                    throw new ContextDenied();
                  const response = await api.request<T>({ ...config, auth: undefined, ...transportConfig(authorized, input.headers),
                    url: authorizationPath(registered.entryId, registered.pathParams),
                    params: method === "GET" ? { ...query, authorization_precondition: JSON.stringify(precondition) } : query,
                    data: method === "GET" ? undefined : commandBody(config.data, precondition),
                  });
                  if (!current()) throw new AuthorizationCancelled();
                  const read = Object.values(contextEntries[registered.entryId].branches).filter(branch => !branch.authorization_view_only).every(branch =>
                    Object.entries(branch.requirements).every(([resource, actions]) => actions.every(action => vocabulary[resource]?.actions[action]?.kind === "data")));
                  if (read && config.responseType !== "blob") validateContextualPayload(response.data, authz.snapshot!,
                    { entryId: registered.entryId, pathParams: registered.pathParams }, authorized.authorizationContext!);
                  return response;
                },
              });
            } finally {
              capture.signal.removeEventListener("abort", abort);
              config.signal?.removeEventListener?.("abort", abort);
            }
          },
        });
      },
    };
  }, [authz.token, authz.snapshot, authz.generation, authz.isCurrent, run, legacyAllowed, lifetimeKey, resolvedRule]);
}
export const domainApi = (capture?: ContextRequestTransport) => createDomainApi(api, capture);

/** Add a per-query abort lifetime without replacing the captured actor or snapshot. */
export function withTransportSignal(capture: ContextRequestTransport, signal: AbortSignal): ContextRequestTransport {
  return { signal, isCurrent: () => !signal.aborted && capture.isCurrent(),
    request: config => capture.request({ ...config, signal }) };
}
