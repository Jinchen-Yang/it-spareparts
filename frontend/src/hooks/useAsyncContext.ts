import { useLayoutEffect, useMemo, useRef } from "react";

export type AsyncGuard = () => boolean;

/** Own callback validity and portal cleanup; callers own data, permissions and APIs. */
export function useAsyncContext(scopeKey: string) {
  const scope = useMemo(() => ({ key: scopeKey }), [scopeKey]);
  const state = useRef<{
    scope: typeof scope | null; epoch: number; sequence: number; mounted: boolean;
  }>({ scope: null, epoch: 0, sequence: 0, mounted: false });
  const confirmation = useRef<{ close: () => void } | null>(null);

  const context = useMemo(() => {
    const closeConfirmation = () => {
      const owned = confirmation.current;
      confirmation.current = null;
      owned?.close();
    };
    const capture = (): AsyncGuard => {
      const epoch = state.current.epoch;
      return () => state.current.mounted && state.current.scope === scope
        && state.current.epoch === epoch;
    };
    return {
      capture,
      begin(): AsyncGuard {
        const current = capture();
        if (!current()) return current;
        const sequence = ++state.current.sequence;
        closeConfirmation();
        return () => current() && state.current.sequence === sequence;
      },
      invalidate() {
        if (state.current.scope !== scope) return;
        state.current.epoch += 1;
        state.current.sequence += 1;
        closeConfirmation();
      },
      ownConfirmation(close: () => void) {
        if (!capture()()) { close(); return () => {}; }
        const owned = { close };
        confirmation.current = owned;
        // A retiring dialog must not unregister the next dialog in the same flow.
        return () => {
          if (confirmation.current === owned) confirmation.current = null;
        };
      },
    };
  }, [scope]);

  useLayoutEffect(() => {
    // Abandoned concurrent renders must not retire the still-visible scope.
    state.current.scope = scope;
    state.current.mounted = true;
    return () => {
      if (state.current.scope === scope) {
        state.current.mounted = false;
        context.invalidate();
      }
    };
  }, [scope, context]);

  return context;
}
