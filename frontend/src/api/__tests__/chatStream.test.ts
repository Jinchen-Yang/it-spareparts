import { describe, expect, it, vi } from "vitest";
import { sessionChatStream } from "../../api";
import { consumeSSE, createStreamValidator, STREAM_ERROR_CATEGORIES } from "../chatStream";
import type { AuthorizationContext } from "../../authz/contextTypes";

const runId = "abcdef0123456789abcdef0123456789";
// Parser-only marker. The production callback runs the shared exact context validator.
const contextMarker = Object.freeze({ marker: "validated elsewhere" });
const ready = (extra = {}) => ({ type: "ready", schema_version: 1, session_id: 1, run_id: runId,
  authorization_context: contextMarker, replay_complete: true, ...extra });
const done = (extra = {}) => ({ type: "done", run_id: runId, seq: 2, tool_calls: [], stopped: false, configured: true, ...extra });
function parser(attach = false) {
  return createStreamValidator({ sessionId: 1, attach, validateContext(value) {
    expect(value).toEqual(contextMarker);
    return value as AuthorizationContext;
  } });
}
function frames(...values: unknown[]) { return values.map(v => `data: ${JSON.stringify(v)}\n\n`).join(""); }
function chunks(text: string, width = 1) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({ start(controller) {
    for (let offset = 0; offset < bytes.length; offset += width) controller.enqueue(bytes.slice(offset, offset + width));
    controller.close();
  } }), { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
}

describe("chat stream transport safety", () => {
  it("does not report an unterminated EOF as a completed answer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('data: {"type":"delta","text":"partial"}\n\n', {
      headers: { "Content-Type": "text/event-stream" },
    })));
    try {
      await expect(sessionChatStream(1, "question", vi.fn())).rejects.toThrow();
    } finally { vi.unstubAllGlobals(); }
  });
  it("does not swallow failures raised by the event consumer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('data: {"type":"delta","text":"partial"}\n\n')));
    try {
      await expect(sessionChatStream(1, "question", () => { throw new Error("consumer failed"); })).rejects.toThrow("consumer failed");
    } finally { vi.unstubAllGlobals(); }
  });
});

describe("closed canonical SSE v1", () => {
  it("decodes split UTF8, CRLF/comments/multiline and EOF flush without appending done.answer", async () => {
    const p = parser(), onEvent = vi.fn();
    const source = frames(ready()).replace(/\n/g, "\r\n") + ': heartbeat\r\n' +
      `data: {"type":"delta","run_id":"${runId}",\r\ndata: "seq":1,"text":"中文🌿"}\r\n\r\n` +
      `data: ${JSON.stringify(done({ answer: "final round only" }))}`;
    await consumeSSE(chunks(source), { parse: p.accept, complete: () => p.complete, onEvent, requireContentType: true });
    expect(onEvent.mock.calls.map(([e]) => e.type)).toEqual(["ready", "delta", "done"]);
    expect(onEvent.mock.calls[1][0].text).toBe("中文🌿");
  });
  it.each([
    ["no handshake", { type: "delta", text: "secret", seq: 1, run_id: runId }],
    ["wrong session", ready({ session_id: 2 })], ["extra field", ready({ allowed: true })],
    ["bad run", ready({ run_id: "x" })], ["null run outside attach", ready({ run_id: null })],
    ["unknown schema", ready({ schema_version: 2 })],
  ])("rejects %s before any content", (_label, value) => { expect(() => parser().accept(value)).toThrow(); });
  it.each([
    { type: "delta", run_id: runId, seq: 2, text: "gap" },
    { type: "delta", run_id: "0".repeat(32), seq: 1, text: "other run" },
    { type: "mystery", run_id: runId, seq: 1 },
    { type: "tool", run_id: runId, seq: 1, name: "bad", args: [] },
    { type: "tool", run_id: runId, seq: 1, name: "bad", args: { n: Infinity } },
    { type: "error", run_id: runId, seq: 1, code: "AUTHZ_FORBIDDEN", category: "provider", message: "bad" },
    { type: "done", run_id: runId, seq: 1, tool_calls: [], stopped: false },
    { type: "no_active", run_id: runId, seq: 1 },
  ])("rejects an invalid frame %#", value => { const p = parser(); p.accept(ready()); expect(() => p.accept(value)).toThrow(); });
  it("requires complete context validation before accepting ready", () => {
    const p = createStreamValidator({ sessionId: 1, attach: false, validateContext: () => { throw new Error("context denied"); } });
    expect(() => p.accept(ready())).toThrow("context denied");
  });
  it("accepts only zero-content replay gap after incomplete replay", () => {
    const p = parser(true); p.accept(ready({ replay_complete: false }));
    expect(() => p.accept({ type: "delta", run_id: runId, seq: 1, text: "prefix" })).toThrow();
    p.accept({ type: "error", run_id: runId, seq: 1, code: "CHAT_REPLAY_GAP", category: "protocol", message: "gap" });
    expect(p.complete).toBe(true);
  });
  it.each(["no_active", "error"])("accepts null-run attach terminating with %s", type => {
    const p = parser(true); p.accept(ready({ run_id: null }));
    p.accept({ type, run_id: null, seq: 1, ...(type === "error" ? { code: "AUTHZ_FORBIDDEN", category: "authorization", message: "denied" } : {}) });
    expect(p.complete).toBe(true);
  });
  it.each(Object.entries(STREAM_ERROR_CATEGORIES))("accepts closed terminal %s", (code, category) => {
    const p = parser(); p.accept(ready());
    p.accept({ type: "error", run_id: runId, seq: 1, code, category, message: "safe explanation" });
    expect(p.complete).toBe(true);
    expect(() => p.accept(done())).toThrow();
  });
  it("limits one merged event rather than the complete response chunk", async () => {
    const p = parser();
    const text = "a".repeat(600_000);
    await consumeSSE(chunks(frames(ready(), { type: "delta", run_id: runId, seq: 1, text },
      { type: "delta", run_id: runId, seq: 2, text }, done({ seq: 3 })), 2_000_000), {
      parse: p.accept, complete: () => p.complete, onEvent: vi.fn(),
    });
    const q = parser();
    await expect(consumeSSE(chunks(frames(ready(), { type: "delta", run_id: runId, seq: 1, text: "a".repeat(1024 * 1024) }), 2_000_000), {
      parse: q.accept, complete: () => q.complete, onEvent: vi.fn(),
    })).rejects.toThrow();
  });
  it("rejects malformed JSON, data after terminal, and early EOF", async () => {
    for (const source of [frames(ready()) + 'data: {bad}\n\n', frames(ready(), done({ seq: 1 }), { type: "title", title: "late", seq: 2, run_id: runId }), frames(ready())]) {
      const p = parser();
      await expect(consumeSSE(chunks(source), { parse: p.accept, complete: () => p.complete, onEvent: vi.fn() })).rejects.toThrow();
    }
  });
});
