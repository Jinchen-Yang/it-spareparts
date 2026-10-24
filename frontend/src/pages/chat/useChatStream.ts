import { useRef } from "react";
import { useAuthz } from "../../authz/AuthzProvider";
import { useCapturedRunner, AuthorizationCancelled } from "../../authz/requestLifecycle";
import { executeContextRequest, commandBody } from "../../authz/context";
import { entryRule, validateContextualPayload } from "../../authz/contextProtocol";
import { registeredEntry } from "../../authz/domainTransport";
import { ruleState, surfaceRule, allRule } from "../../authz/rules";
import { consumeSSE, createStreamValidator, type CanonicalStreamEvent } from "../../api/chatStream";
import type { SessionStreamEvent } from "../../api";

/** Uses the same captured runner and registered entry as JSON requests; SSE only changes decoding. */
export function useChatStream(lifetime: string, legacyAllowed: boolean) {
  const authz = useAuthz(), run = useCapturedRunner(lifetime), latest = useRef(lifetime);
  latest.current = lifetime;
  return async (sessionId: number, message: string | undefined,
    onEvent: (event: SessionStreamEvent | CanonicalStreamEvent) => void, signal: AbortSignal) => {
    const attach = message === undefined;
    const path = `/agent/sessions/${sessionId}/chat/${attach ? "attach" : "stream"}`;
    const { entryId, pathParams } = registeredEntry("POST", path);
    const rule = allRule(entryRule(entryId), surfaceRule(attach ? "private_chat_session.attach" : "agent_chat.conversation"));
    const legacy = Object.fromEntries(rule.anyOf.flat().map(need => [need.resource_id, legacyAllowed]));
    const input = { pathParams, body: attach ? {} : { message } };
    return run({ admit: snapshot => ruleState(snapshot, rule, legacy) !== "deny", queryKey: `${entryId}:${sessionId}`,
      isInputCurrent: () => latest.current === lifetime && !signal.aborted,
      request: async (capture, current) => {
        const controller = new AbortController(), abort = () => controller.abort();
        capture.signal.addEventListener("abort", abort, { once: true }); signal.addEventListener("abort", abort, { once: true });
        if (capture.signal.aborted || signal.aborted) abort();
        const captured = { ...capture, signal: controller.signal };
        const fetchStream = async (body: unknown) => {
          const response = await fetch(`/api${path}`, { method: "POST", cache: "no-store", signal: captured.signal,
            headers: { "Content-Type": "application/json", Authorization: `Bearer ${captured.token}` }, body: JSON.stringify(body) });
          if (!response.ok) throw Object.assign(new Error(`聊天请求失败（${response.status}），没有自动重发。`), { response: { status: response.status } });
          if (!current()) throw new AuthorizationCancelled();
          return response;
        };
        try {
          const allLegacy = rule.anyOf.flat().every(need => authz.snapshot?.resources[need.resource_id]?.runtime_state === "legacy");
          if (allLegacy) {
            let complete = false;
            return await consumeSSE(await fetchStream(input.body), { signal: captured.signal, current,
              parse: value => {
                if (!value || typeof value !== "object" || !("type" in value) || complete) throw new Error("聊天流协议无效");
                const event = value as SessionStreamEvent; complete = ["done", "error", "no_active"].includes(event.type); return event;
              }, onEvent, complete: () => complete });
          }
          await executeContextRequest({ entryId, input, rule, snapshot: authz.snapshot!, capture: captured, current,
            execute: async (authorized, precondition) => {
              const validator = createStreamValidator({ sessionId, attach, validateContext: value => {
                const payload = { authorization_context: value };
                validateContextualPayload(payload, authz.snapshot!, { entryId, pathParams }, authorized.authorizationContext!);
                return payload.authorization_context as NonNullable<typeof authorized.authorizationContext>;
              } });
              await consumeSSE(await fetchStream(commandBody(input.body, precondition)), {
                signal: captured.signal, current, requireContentType: true, parse: validator.accept,
                onEvent, complete: () => validator.complete,
              });
            } });
        } finally { capture.signal.removeEventListener("abort", abort); signal.removeEventListener("abort", abort); }
      } });
  };
}
