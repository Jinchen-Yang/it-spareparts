import { useEffect, useMemo, useRef } from "react";

export type AsyncGuard = () => boolean;

/** Own callback validity and portal cleanup; callers own data, permissions and APIs. */
export function useAsyncContext(scopeKey: string) {
  const state = useRef({ key: scopeKey, generation: 0, epoch: 0, sequence: 0, mounted: false });
  const confirmation = useRef<{ close: () => void } | null>(null);
  if (state.current.key !== scopeKey) {
    // Invalidate during render, before an old callback can run ahead of effects.
    state.current.key = scopeKey;
    state.current.generation += 1;
    state.current.epoch += 1;
    state.current.sequence += 1;
  }

  const context = useMemo(() => {
    const generation = state.current.generation;
    const closeConfirmation = () => {
      const owned = confirmation.current;
      confirmation.current = null;
      owned?.close();
    };
    const capture = (): AsyncGuard => {
      const epoch = state.current.epoch;
      return () => state.current.mounted && state.current.key === scopeKey
        && state.current.generation === generation && state.current.epoch === epoch;
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
        state.current.epoch += 1;
        state.current.sequence += 1;
        closeConfirmation();
      },
      ownConfirmation(close: () => void) {
        const owned = { close };
        confirmation.current = owned;
        // A retiring dialog must not unregister the next dialog in the same flow.
        return () => {
          if (confirmation.current === owned) confirmation.current = null;
        };
      },
    };
  }, [scopeKey]);

  useEffect(() => {
    state.current.mounted = true;
    return () => {
      state.current.mounted = false;
      context.invalidate();
    };
  }, [scopeKey, context]);

  return context;
}
