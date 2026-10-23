import { useCallback, useEffect, useRef, useState } from "react";
import { explainApiError } from "../../api/accounts";
import { invalidateAuthzSession } from "../../authz/events";
import { clearSessionScopedPreferences } from "../../sessionPreferences";

/** A mounted dialog owns exactly one login, target and sequence. */
export function useAuthorizationTask(target: string) {
  const [owner] = useState(() => localStorage.getItem("token") || "");
  const generation = useRef(0),
    alive = useRef(true);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const current = useCallback(
    () => alive.current && !!owner && localStorage.getItem("token") === owner,
    [owner],
  );
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      generation.current++;
    };
  }, [target]);
  const invalidate = useCallback(() => {
    generation.current++;
    setBusy(false);
    setError("");
  }, []);
  const run = useCallback(
    async <T>(job: () => Promise<T>, success: (value: T) => void) => {
      if (!current()) return;
      const ticket = ++generation.current;
      setBusy(true);
      setError("");
      try {
        const value = await job();
        // A committed revocation belongs to the captured login, even if its dialog
        // closed or another target replaced it. Never clear a newer login.
        if (
          localStorage.getItem("token") === owner &&
          value &&
          typeof value === "object" &&
          "actor_session_revoked" in value &&
          value.actor_session_revoked === true
        ) {
          clearSessionScopedPreferences();
          for (const k of [
            "token",
            "username",
            "role",
            "name",
            "permissions",
            "beta_features",
          ])
            localStorage.removeItem(k);
          invalidateAuthzSession(owner);
          return;
        }
        if (!current() || ticket !== generation.current) return;
        success(value);
      } catch (e) {
        if (current() && ticket === generation.current)
          setError(
            explainApiError(e, "操作失败，请重新加载后确认；没有自动重试。"),
          );
      } finally {
        if (current() && ticket === generation.current) setBusy(false);
      }
    },
    [current, owner],
  );
  return { owner, current, run, busy, error, setError, invalidate };
}
