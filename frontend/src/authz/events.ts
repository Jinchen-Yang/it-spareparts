export const AUTHZ_SESSION_INVALIDATED = "authz-session-invalidated";

export function invalidateAuthzSession(token: string | null) {
  window.dispatchEvent(new CustomEvent(AUTHZ_SESSION_INVALIDATED, { detail: { token } }));
}
