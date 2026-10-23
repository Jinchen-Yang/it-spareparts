import { useEffect } from "react";
import { useAuthorizedQuery, type AuthorizedRequest } from "../../../authz/access";
import { actionRule } from "../../../authz/rules";
import { sessionStale, SESSION_EXPIRED_MESSAGE } from "../../../utils/session";
export interface AsyncState<T> { data: T | null; loading: boolean; error: string | null }
/** Sales adapter supplies a precise data action; display eligibility is a caller's mount rule. */
export function useAsyncData<T>(
  load: (context: AuthorizedRequest) => Promise<{ data: T }>, deps: unknown[],
  toMessage: (error: unknown) => string, enabled: boolean,
  owner: string | null | undefined, onStale: (() => void) | undefined, action: string,
): AsyncState<T> & { retry: () => void } {
  const stale = owner !== undefined && sessionStale(owner);
  useEffect(() => { if (stale) onStale?.(); }, [stale]);
  const result = useAuthorizedQuery({ rule: actionRule("sales_analysis", action),
    legacy: { sales_analysis: true }, queryKey: deps, enabled: enabled && !stale, onStale,
    query: async context => (await load(context)).data });
  return { data: result.data, loading: result.loading,
    error: stale ? SESSION_EXPIRED_MESSAGE : result.error ? toMessage(result.error) : null, retry: result.retry };
}
