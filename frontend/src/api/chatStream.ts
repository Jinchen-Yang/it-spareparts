import type { AuthorizationContext } from "../authz/contextTypes";

export const STREAM_ERROR_CATEGORIES = {
  AUTHN_SESSION_INVALID: "authorization", AUTHZ_FORBIDDEN: "authorization",
  AUTHZ_CONTEXT_STALE: "authorization", AUTHZ_ARTIFACT_STALE: "authorization",
  AUTHZ_RESOURCE_UNAVAILABLE: "server", AUTHZ_ENGINE_UNAVAILABLE: "server",
  CHAT_PERSISTENCE_FAILED: "server", CHAT_PROVIDER_UNAVAILABLE: "provider",
  CHAT_REPLAY_GAP: "protocol", CHAT_STREAM_PROTOCOL_ERROR: "protocol",
} as const;
export type StreamErrorCode = keyof typeof STREAM_ERROR_CATEGORIES;
export interface StreamToolCall { name: string; args: Record<string, unknown> }
export type StreamPayload =
  | { type: "delta" | "thinking"; text: string }
  | ({ type: "tool" } & StreamToolCall)
  | { type: "tool_done"; name: string; ok: boolean }
  | { type: "title"; title: string }
  | { type: "done"; tool_calls: StreamToolCall[]; stopped: boolean; configured: boolean; answer?: string }
  | { type: "error"; code: StreamErrorCode; category: typeof STREAM_ERROR_CATEGORIES[StreamErrorCode]; message: string }
  | { type: "no_active" };
export interface StreamReady {
  type: "ready"; schema_version: 1; session_id: number | null; run_id: string | null;
  authorization_context: AuthorizationContext; replay_complete: boolean;
}
export type CanonicalStreamEvent = StreamReady | (StreamPayload & { run_id: string | null; seq: number });
export class ChatStreamProtocolError extends Error {
  constructor(message = "聊天流协议无效，已停止接收；没有自动重发。") { super(message); this.name = "ChatStreamProtocolError"; }
}
export class ChatStreamInterrupted extends Error {
  constructor() { super("连接已中断，未收到完整结束确认；没有自动重发。 "); this.name = "ChatStreamInterrupted"; }
}
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
function keys(v: Record<string, unknown>, required: string[], optional: string[] = []) {
  if (required.some(k => !Object.prototype.hasOwnProperty.call(v, k)) || Object.keys(v).some(k => !required.includes(k) && !optional.includes(k))) throw new ChatStreamProtocolError();
}
function jsonValue(v: unknown): boolean {
  return v === null || typeof v === "string" || typeof v === "boolean" ||
    (typeof v === "number" && Number.isFinite(v)) || (Array.isArray(v) && v.every(jsonValue)) ||
    (object(v) && Object.values(v).every(jsonValue));
}
function toolCall(v: unknown): boolean {
  if (!object(v)) return false;
  keys(v, ["name", "args"]);
  return typeof v.name === "string" && object(v.args) && jsonValue(v.args);
}

/** The context callback must use the captured snapshot and exact registered entry. */
export function createStreamValidator(options: {
  sessionId: number | null; attach: boolean;
  validateContext: (value: unknown) => AuthorizationContext;
}) {
  let ready: StreamReady | null = null, next = 1, terminal = false;
  return {
    get complete() { return terminal; },
    accept(value: unknown): CanonicalStreamEvent {
      if (!object(value) || terminal) throw new ChatStreamProtocolError();
      if (!ready) {
        keys(value, ["type", "schema_version", "session_id", "run_id", "authorization_context", "replay_complete"]);
        if (value.type !== "ready" || value.schema_version !== 1 || value.session_id !== options.sessionId ||
            (value.session_id !== null && (!Number.isSafeInteger(value.session_id) || Number(value.session_id) <= 0)) ||
            !(typeof value.run_id === "string" && /^[a-f0-9]{32}$/.test(value.run_id) || options.attach && value.run_id === null) ||
            typeof value.replay_complete !== "boolean" || (value.run_id === null && value.replay_complete !== true)) throw new ChatStreamProtocolError();
        const context = options.validateContext(value.authorization_context);
        ready = { ...value, authorization_context: context } as unknown as StreamReady;
        return ready;
      }
      if (value.run_id !== ready.run_id || !Number.isSafeInteger(value.seq) || value.seq !== next) throw new ChatStreamProtocolError();
      const base = ["type", "run_id", "seq"];
      const fail = () => { throw new ChatStreamProtocolError(); };
      if (ready.replay_complete === false && !(value.type === "error" && value.code === "CHAT_REPLAY_GAP")) fail();
      if (ready.run_id === null && !["no_active", "error"].includes(String(value.type))) fail();
      switch (value.type) {
        case "delta": case "thinking": keys(value, [...base, "text"]); if (typeof value.text !== "string") fail(); break;
        case "tool": keys(value, [...base, "name", "args"]); if (!toolCall({ name: value.name, args: value.args })) fail(); break;
        case "tool_done": keys(value, [...base, "name", "ok"]); if (typeof value.name !== "string" || typeof value.ok !== "boolean") fail(); break;
        case "title": keys(value, [...base, "title"]); if (typeof value.title !== "string") fail(); break;
        case "done":
          keys(value, [...base, "tool_calls", "stopped", "configured"], ["answer"]);
          if (!Array.isArray(value.tool_calls) || !value.tool_calls.every(toolCall) || typeof value.stopped !== "boolean" || typeof value.configured !== "boolean" ||
              ("answer" in value && typeof value.answer !== "string")) fail();
          terminal = true; break;
        case "error":
          keys(value, [...base, "code", "category", "message"]);
          if (typeof value.code !== "string" || !Object.prototype.hasOwnProperty.call(STREAM_ERROR_CATEGORIES, value.code) ||
              STREAM_ERROR_CATEGORIES[value.code as StreamErrorCode] !== value.category || typeof value.message !== "string") fail();
          terminal = true; break;
        case "no_active":
          keys(value, base); if (!options.attach || ready.run_id !== null || next !== 1 || !ready.replay_complete) fail();
          terminal = true; break;
        default: fail();
      }
      next++;
      return value as unknown as CanonicalStreamEvent;
    },
  };
}

/** Decode complete SSE data events, never treat a network chunk as an event. */
export async function consumeSSE<T>(response: Response, options: {
  parse: (value: unknown) => T; onEvent: (event: T) => void; complete: () => boolean;
  signal?: AbortSignal; current?: () => boolean; requireContentType?: boolean;
}): Promise<void> {
  if (!response.ok || !response.body) throw new Error(`stream http ${response.status}`);
  if (options.requireContentType && response.headers.get("content-type")?.split(";", 1)[0].trim() !== "text/event-stream") throw new ChatStreamProtocolError();
  const reader = response.body.getReader(), decoder = new TextDecoder("utf-8", { fatal: true }), encoder = new TextEncoder();
  let buffer = "", data: string[] = [], size = 0;
  const check = () => { if (options.signal?.aborted || options.current && !options.current()) throw new DOMException("Stream owner changed", "AbortError"); };
  const dispatch = () => {
    if (!data.length) return;
    const raw = data.join("\n"); data = []; size = 0;
    check();
    let value: unknown;
    try { value = JSON.parse(raw); } catch { throw new ChatStreamProtocolError(); }
    const event = options.parse(value);
    check(); options.onEvent(event);
  };
  const line = (value: string) => {
    if (!value) { dispatch(); return; }
    if (value.startsWith(":")) return;
    const colon = value.indexOf(":");
    const name = colon === -1 ? value : value.slice(0, colon);
    if (name !== "data") throw new ChatStreamProtocolError();
    let text = colon === -1 ? "" : value.slice(colon + 1);
    if (text.startsWith(" ")) text = text.slice(1);
    size += encoder.encode(text).length + (data.length ? 1 : 0);
    if (size > 1024 * 1024) throw new ChatStreamProtocolError("聊天单帧过大，已停止接收。");
    data.push(text);
  };
  const drain = (final: boolean) => {
    for (;;) {
      const pos = buffer.search(/[\r\n]/);
      if (pos < 0 || !final && pos === buffer.length - 1 && buffer[pos] === "\r") break;
      const width = buffer[pos] === "\r" && buffer[pos + 1] === "\n" ? 2 : 1;
      const value = buffer.slice(0, pos); buffer = buffer.slice(pos + width); line(value);
    }
    if (encoder.encode(buffer).length > 1024 * 1024 + 6) throw new ChatStreamProtocolError();
    if (final) { if (buffer) line(buffer); buffer = ""; dispatch(); }
  };
  const abort = () => { void reader.cancel().catch(() => undefined); };
  options.signal?.addEventListener("abort", abort, { once: true });
  try {
    for (;;) {
      check();
      const { done, value } = await reader.read();
      check();
      if (done) { buffer += decoder.decode(); drain(true); break; }
      buffer += decoder.decode(value, { stream: true }); drain(false);
    }
    if (!options.complete()) throw new ChatStreamInterrupted();
  } catch (error) { await reader.cancel().catch(() => undefined); throw error; }
  finally { options.signal?.removeEventListener("abort", abort); reader.releaseLock(); }
}
