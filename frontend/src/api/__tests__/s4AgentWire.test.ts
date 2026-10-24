import { expect, it } from "vitest";
import wire from "../../../../docs/reviews/authz-rewrite-20261023/s4-agent-http-wire-golden.json";
import { type AuthzSnapshot, type AuthzCatalog, validateAuthzSnapshot, validateCatalog } from "../authz";
import { validateAuthorizationResolution, validateContextualPayload } from "../../authz/contextProtocol";
import { consumeSSE, createStreamValidator } from "../chatStream";
it("consumes actual PG/PDP catalog, snapshot and list/history/preview responses without repairing context", () => {
  const snapshot = validateAuthzSnapshot(wire.snapshot as unknown as AuthzSnapshot);
  expect(validateCatalog(wire.catalog as unknown as AuthzCatalog).modules.length).toBeGreaterThan(100);
  for (const sample of wire.samples) {
    const resolved = validateAuthorizationResolution(sample.resolution, snapshot, { entryId: sample.entry_id });
    expect(validateContextualPayload(sample.payload, snapshot, { entryId: sample.entry_id }, resolved.authorization_context)).toBe(sample.payload);
  }
});
it("consumes actual server ready/terminal through the closed parser and exact context contract", async () => {
  const snapshot = validateAuthzSnapshot(wire.snapshot as unknown as AuthzSnapshot);
  const first = JSON.parse(wire.sse.split("\n").find(line => line.startsWith("data: "))!.slice(6));
  const validator = createStreamValidator({ sessionId: first.session_id!, attach: false,
    validateContext: value => {
      const payload = { authorization_context: value };
      validateContextualPayload(payload, snapshot, { entryId: wire.sse_entry }, first.authorization_context as never);
      return value as typeof first.authorization_context & never;
    } });
  await consumeSSE(new Response(wire.sse, { headers: { "Content-Type": "text/event-stream" } }), { parse: validator.accept, onEvent: () => {}, complete: () => validator.complete, requireContentType: true });
  expect(validator.complete).toBe(true);
});
