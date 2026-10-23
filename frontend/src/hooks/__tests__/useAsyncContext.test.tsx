import { StrictMode, useEffect } from "react";
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useAsyncContext, type AsyncGuard } from "../useAsyncContext";

describe("async callback ownership", () => {
  it("separates scope validity from the latest request in that scope", () => {
    const { result, unmount } = renderHook(() => useAsyncContext("A"));
    const scoped = result.current.capture();
    const first = result.current.begin();
    const second = result.current.begin();
    expect(scoped()).toBe(true);
    expect(first()).toBe(false);
    expect(second()).toBe(true);
    act(() => result.current.invalidate());
    expect(scoped()).toBe(false);
    expect(second()).toBe(false);
    unmount();
  });

  it("does not revive callbacks after A to B to A or after unmount", () => {
    const { result, rerender, unmount } = renderHook(
      ({ scope }) => useAsyncContext(scope), { initialProps: { scope: "A" } },
    );
    const original = result.current.capture();
    const close = vi.fn();
    result.current.ownConfirmation(close);
    rerender({ scope: "B" });
    expect(close).toHaveBeenCalledOnce();
    rerender({ scope: "A" });
    expect(original()).toBe(false);
    const current = result.current.capture();
    expect(current()).toBe(true);
    unmount();
    expect(current()).toBe(false);
    expect(close).toHaveBeenCalledOnce();
  });

  it("retiring a previous confirmation cannot unregister its successor", () => {
    const { result, unmount } = renderHook(() => useAsyncContext("A"));
    const releaseOld = result.current.ownConfirmation(vi.fn());
    const closeNext = vi.fn();
    result.current.ownConfirmation(closeNext);
    releaseOld();
    act(() => result.current.invalidate());
    expect(closeNext).toHaveBeenCalledOnce();
    unmount();
  });

  it("an old handler cannot start a new operation in a different scope", () => {
    const { result, rerender, unmount } = renderHook(
      ({ scope }) => useAsyncContext(scope), { initialProps: { scope: "A" } },
    );
    const old = result.current;
    rerender({ scope: "B" });
    const current = result.current.begin();
    const closeCurrent = vi.fn();
    result.current.ownConfirmation(closeCurrent);
    expect(old.capture()()).toBe(false);
    expect(old.begin()()).toBe(false);
    expect(current()).toBe(true);
    expect(closeCurrent).not.toHaveBeenCalled();
    rerender({ scope: "A" });
    const returned = result.current.begin();
    expect(old.capture()()).toBe(false);
    expect(old.begin()()).toBe(false);
    expect(returned()).toBe(true);
    unmount();
  });

  it("invalidates the first StrictMode effect lifetime and keeps the second usable", () => {
    const guards: AsyncGuard[] = [];
    const { result, unmount } = renderHook(() => {
      const context = useAsyncContext("A");
      useEffect(() => { guards.push(context.capture()); }, [context]);
      return context;
    }, { wrapper: StrictMode });
    expect(guards).toHaveLength(2);
    expect(guards[0]()).toBe(false);
    expect(guards[1]()).toBe(true);
    const close = vi.fn();
    result.current.ownConfirmation(close);
    unmount();
    expect(guards[1]()).toBe(false);
    expect(close).toHaveBeenCalledOnce();
  });
});
