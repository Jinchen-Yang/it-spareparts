import type { ContextEntryId } from "./generated";
import { useAuthz } from "./AuthzProvider";
import { useCapturedQuery } from "./requestLifecycle";
import { useContextTransport, withTransportSignal, type ContextRequestTransport } from "./domainTransport";
import { entryRule, validateAuthorizationContext, ContextProtocolError } from "./contextProtocol";
import { allRule, ruleState, type Rule } from "./rules";
import type { AuthorizationContext, BoundAuthorizationContext } from "./contextTypes";

/** The shell may resolve; query transport still performs view→allow→business before this returns any payload. */
export function useContextQuery<T>({ entryId, rule, queryKey, query, enabled = true, legacyAllowed = false, onStale, refreshKey, retainLegacyOnRefresh = false, resolvedRule }: {
  entryId: ContextEntryId;
  rule: Rule;
  queryKey: readonly unknown[];
  query: (capture: ContextRequestTransport) => Promise<T>;
  enabled?: boolean;
  legacyAllowed?: boolean;
  onStale?: () => void;
  refreshKey?: unknown;
  retainLegacyOnRefresh?: boolean;
  resolvedRule?: (entryId: string, context: AuthorizationContext) => Rule;
}) {
  const authz = useAuthz();
  const combined = allRule(rule, entryRule(entryId));
  const legacy = Object.fromEntries(combined.anyOf.flat().map(need => [need.resource_id, legacyAllowed]));
  const state = authz.status === "ready" ? ruleState(authz.snapshot, combined, legacy) : "deny";
  const inputKey = JSON.stringify([entryId, queryKey]);
  const transport = useContextTransport(inputKey, legacyAllowed, resolvedRule);
  const result = useCapturedQuery<{ payload: T; context: BoundAuthorizationContext | null }>({
    active: enabled && state !== "deny", onStale, refreshKey,
    retainWhileRefreshing: retainLegacyOnRefresh && combined.anyOf.flat().length > 0 && combined.anyOf.flat().every(need =>
      authz.snapshot?.resources[need.resource_id]?.runtime_state === "legacy"), identityKey: combined, queryKey: [entryId, ...queryKey],
    query: async capture => {
      const payload = await query(withTransportSignal(transport, capture.signal));
      const allLegacy = combined.anyOf.flat().length > 0 && combined.anyOf.flat().every(need =>
        authz.snapshot?.resources[need.resource_id]?.runtime_state === "legacy");
      if (allLegacy) return { payload, context: null };
      if (!authz.snapshot || !payload || typeof payload !== "object") throw new ContextProtocolError();
      const value = validateAuthorizationContext((payload as { authorization_context?: AuthorizationContext }).authorization_context,
        authz.snapshot, { entryId });
      return { payload, context: { capture, inputKey, value } };
    },
  });
  return { ...result, data: result.data?.payload ?? null, context: result.data?.context ?? null,
    entryState: state, allowed: state !== "deny" && result.data !== null,
  };
}
