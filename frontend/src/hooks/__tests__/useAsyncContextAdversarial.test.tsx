import { StrictMode, Suspense, startTransition, useEffect, useState } from "react";
import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAsyncContext } from "../useAsyncContext";

afterEach(cleanup);

describe("async context independent adversarial cases", () => {
  it.each(["B", "A"])("a retired A owner cannot invalidate or steal the portal after switching through B to %s", (destination) => {
    const { result, rerender, unmount } = renderHook(
      ({ scope }) => useAsyncContext(scope), { initialProps: { scope: "A" } },
    );
    const retired = result.current;
    rerender({ scope: "B" });
    if (destination === "A") rerender({ scope: "A" });
    const active = result.current.begin();
    const closeActive = vi.fn();
    result.current.ownConfirmation(closeActive);
    retired.invalidate();
    const closeLatePortal = vi.fn();
    const releaseLate = retired.ownConfirmation(closeLatePortal);
    releaseLate();
    expect(active()).toBe(true);
    expect(closeActive).not.toHaveBeenCalled();
    expect(closeLatePortal).toHaveBeenCalledOnce();
    unmount();
    expect(closeActive).toHaveBeenCalledOnce();
  });

  it("independent read and upload scopes do not cancel each other's request or portal", () => {
    const { result, rerender } = renderHook(({ permission }) => ({
      read: useAsyncContext("project-A"),
      upload: useAsyncContext(`project-A:${permission}`),
    }), { initialProps: { permission: true } });
    const read = result.current.read.begin();
    const upload = result.current.upload.begin();
    const closeUpload = vi.fn();
    result.current.upload.ownConfirmation(closeUpload);
    rerender({ permission: false });
    expect(read()).toBe(true);
    expect(upload()).toBe(false);
    expect(closeUpload).toHaveBeenCalledOnce();
    expect(result.current.upload.begin()()).toBe(true);
  });

  it("a delayed StrictMode first-lifetime completion cannot replace the current lifetime's portal", async () => {
    const deliver: Array<() => void> = [];
    const closeStale = vi.fn();
    const closeCurrent = vi.fn();
    const { unmount } = renderHook(() => {
      const context = useAsyncContext("A");
      useEffect(() => {
        const current = context.begin();
        const close = deliver.length === 0 ? closeStale : closeCurrent;
        deliver.push(() => { if (current()) context.ownConfirmation(close); });
      }, [context]);
    }, { wrapper: StrictMode });
    expect(deliver).toHaveLength(2);
    await act(async () => { deliver[1](); deliver[0](); });
    unmount();
    expect(closeStale).not.toHaveBeenCalled();
    expect(closeCurrent).toHaveBeenCalledOnce();
  });

  it("a cancelled suspended scope switch leaves the committed scope able to start work", async () => {
    const never = new Promise<void>(() => {});
    const attempted = vi.fn();
    const started = vi.fn();
    function Scoped({ scope }: { scope: string }) {
      const context = useAsyncContext(scope);
      if (scope === "B") { attempted(); throw never; }
      return <button onClick={() => { if (context.begin()()) started(scope); }}>Run {scope}</button>;
    }
    function Harness() {
      const [scope, setScope] = useState("A");
      return <>
        <button onClick={() => startTransition(() => setScope("B"))}>Try B</button>
        <button onClick={() => setScope("A")}>Keep A</button>
        <Suspense fallback={<p>Pending</p>}><Scoped scope={scope} /></Suspense>
      </>;
    }
    render(<Harness />);
    fireEvent.click(screen.getByText("Run A"));
    expect(started).toHaveBeenCalledTimes(1);
    await act(async () => { fireEvent.click(screen.getByText("Try B")); });
    expect(attempted).toHaveBeenCalled();
    fireEvent.click(screen.getByText("Keep A"));
    fireEvent.click(screen.getByText("Run A"));
    expect(started).toHaveBeenCalledTimes(2);
    expect(started).toHaveBeenLastCalledWith("A");
  });
});
