# Agent Capability Policy v1

This increment advances the read-only Capability Kernel tracked by `#219`. It does **not** close
the full issue. Its job is to decide which tool schemas a model may see, repeat the decision at
each model/dispatch/result seam, and bound every projection that can cross those seams.

Each tool declares immutable `effects`, multi-edge `egress`, `sensitivity`, `permission_id`, `enabled`,
`ToolBudget`, argument validator, handler and `implementation_version` metadata. Dispatch resolves
the immutable `ToolSpec` and invokes only `ToolSpec.handler`; `_REGISTRY` is a read-only compatibility
projection and is not an execution authority. Before a handler can touch a database or file, one
fail-closed validator rejects undeclared keys, schema/type errors and resource-budget overruns with
stable, non-retriable codes (`AGENT_TOOL_ARGS_INVALID`, `AGENT_TOOL_BUDGET_EXCEEDED` or
`AGENT_TOOL_VALIDATOR_FAILED`). Budgets cover payload shape plus tool-specific query/PN length,
limit, days, page, rows/items/cells, sheet and output-name bounds.
`get_profit_ranking` additionally accepts only canonical `YYYY-MM-DD` calendar dates and rejects
an inverted start/end range before any query; malformed dates never degrade into an unbounded
all-history query.

Each egress edge also declares a stable purpose ID, projection ID, UTF-8 byte ceiling, edge policy
version and `no_additional_egress_archive_v1` retention ID. The retention ID means only that this
application creates no additional durable archive of the serialized egress payload; it does not
claim that a provider keeps no logs. Empty edge tuples are structurally valid in the general
contract, but this model-tool runtime fails closed unless a capability has a bounded primary-model
result edge.

The static fingerprint is deterministic over tool name, function parameter schema, effects,
egress, sensitivity, permission policy, stable-subject effects, budget, validator/handler identity
and implementation version. Descriptions, arguments and secrets are excluded. A separate runtime
fingerprint covers the current trust zone, both egress switches, normalized model-provider origin
and normalized private/approved-external allowlists for both primary and Vision providers, their
model names, the loopback-HTTP exception and the edge policy. Keeping these fingerprints separate lets durable tasks
distinguish a code-policy change from a deployment-policy change.

The provider boundary fails closed:

- `LLM_TRUST_ZONE=unknown` or disabled model-context egress exposes no tools.
- `private` is not trusted by the zone label alone, but v1's allowlist is still only an
  **operator assertion**, not endpoint identity attestation. Model-context egress must be enabled
  and the normalized `LLM_BASE_URL` origin must exactly match one of `LLM_PRIVATE_BASE_URLS`.
  `approved_external` also
  requires an exact match in `LLM_APPROVED_EXTERNAL_BASE_URLS`; a label alone never authorizes it.
  All production origins require HTTPS. HTTP is accepted only for an explicit
  `AGENT_ALLOW_LOOPBACK_HTTP=true` development deployment and a literal loopback target. Allowlist
  entries reject userinfo/path/query/fragment and do not infer trust from RFC1918/Tailnet
  addressing. Any malformed entry keeps that destination closed. Exact origin equality does **not**
  prove the current DNS answer, route, Tailnet peer, ACL or TLS peer: a public HTTPS FQDN can still
  be operator-misclassified as private in v1. Therefore operator assertion alone is never an
  attested customer-file release permission: production rejects private conversation/Vision file
  egress even after exact-origin match. A separate
  `AGENT_ALLOW_UNATTESTED_PRIVATE_FOR_DEVELOPMENT=true` escape hatch works only when
  `ENVIRONMENT` is `dev`/`test`; production ignores it. Production Agent remains disabled until
  `#225` binds the profile to a verifiable Tailscale peer/ACL plus egress firewall, or mTLS/SPKI
  identity, and handles DNS rebinding on every connection fail-closed.
- `approved_external` may receive a capability's business-confidential result only after exact
  destination authorization. Customer-file edges are disabled in v1: the deployment-wide
  `AGENT_EXTERNAL_FILE_EGRESS_ENABLED` switch is a kill-switch/future input, not evidence that the
  current user consented. No value of that switch enables an approved-external customer-file edge
  until a verifiable per-user grant/revocation record is designed and independently reviewed.
- The primary-model call itself is gated before every request, not merely when tool schemas are
  selected. v1 has no reliable per-message provenance, and prior assistant answers may contain
  customer-file-derived excerpts, so the entire system/user/assistant/history prompt is
  conservatively classified as `customer_file`. Consequently an `approved_external` primary
  provider cannot receive any v1 conversation, even when the global switch is true. API/session
  handlers preflight this before starting or saving a new turn, while runtime and provider repeat
  the live decision before every call (including the final no-tools call) to catch policy changes.
  A `private` exact-allowlisted provider still needs verified private identity in production;
  v1 implements no such attestation, so only the explicit dev/test override is usable.
- Vision has an independent `VISION_TRUST_ZONE`, base URL, model and private/external allowlists.
  `read_document_with_vision` declares two mandatory edges: customer file to Vision and OCR output
  to the primary model. The capability is hidden and denied unless both pass. An
  approved-external Vision customer-file edge is likewise disabled pending per-user consent.
- `FILE_READ` and `ARTIFACT_CREATE` capabilities additionally require a stable
  `authn=sys_user` subject. In RBAC deployments, shared/legacy credentials receive no business or
  file Capability schemas and dispatch/model-call identity refresh fails closed; they are never
  upgraded into non-revocable shared data readers. The direct upload route enforces the same named
  subject before its application code reads the `UploadFile`, records customer filename metadata or
  calls storage, preventing ownerless artifacts. Framework-level streaming multipart admission is
  tracked separately by `#222`.
- Every RBAC-enabled dispatch reloads the active `SysUser`, current role, salesperson subject,
  runtime-safe effective permissions and `token_version`; missing/disabled/revoked/DB-error states
  fail before the handler. Named files are owner-only for every role, including admin and boss.
- Runtime repeats that same DB identity/role/permission refresh before **every** provider call and
  recomputes the visible schemas. Disabling an account or revoking a token stops before another
  delegate/network send; changing a role/permission removes the schema from the next call. A
  server-only release record retains the canonical source `file_id`/`base_file_id` and top-level
  result Artifact IDs without putting them in logs/SSE. After a handler, every result edge and
  concrete source owner/existence check is repeated before serialization, append, public release
  and every later provider attempt/retry.
- Provider clients disable redirects and ambient proxy inheritance and are closed after success or
  failure. A profile-aware transport repeats the fresh principal/page/capability/source-owner guard,
  live trust-zone, enabled-switch and exact-origin allowlist decision before that transport reads
  the body of **every actual HTTP attempt**, including SDK retries. The SDK may already have
  serialized its request, but a denied `handle_request` produces zero delegate/network sends. The
  principal/capability/source-owner authorizer is called fresh on every attempt, but any false or
  exceptional decision permanently poisons that logical provider call; a later SDK retry cannot
  resurrect it even if a subsequent decision would allow. The
  actual URL must remain on the current configured origin and exact base path plus
  `/chat/completions`; credentials, query/fragment, encoded/dot/backslash paths and sibling prefixes
  fail closed. Primary and Vision wire bodies are capped before delegate send, and transport markers
  map to fixed value-free model/Vision egress or payload errors. Vision's server-only guard also runs
  before local parsing/PDF rendering and again after projection at the transport boundary, so a
  revoked owner/capability produces zero provider delegate sends.
  Spreadsheet strings are emitted as literal cells while real numeric values stay numeric.
- Tool traces sent over SSE or stored in checkpoints contain only capability name, argument keys and
  counts/lengths, declared policy metadata, and format-validated artifact IDs. Raw argument values
  are re-sanitized at runtime, session-worker and durable-store seams; legacy stored traces are also
  sanitized on read.
- A single immutable resource contract caps 16 tool calls per provider response, 32 per run,
  8 MiB UTF-8 arguments per call and 16 MiB per response. It also caps visible chat output at
  512 KiB per provider response and per run/session, coalesced into at most 64 public delta events
  (8 KiB target batches); larger results must use an owner-ACL Artifact. Public trace entries cap at
  32 and artifact IDs at 8
  per entry. Tool result projections (2 MiB business, 4 MiB customer-file), cumulative conversation
  JSON (8 MiB), Vision request JSON including data URLs/hint (20 MiB), and OCR output (2 MiB) are
  measured at their declared edges before append/client use. `NaN`/`Infinity` are rejected with
  `allow_nan=false`; over-budget errors use fixed codes and carry no raw values.
- Provider `reasoning_content` is consumed and discarded at the adapter. Runtime also drops any
  legacy/future `reasoning` event, and the session hub whitelists event types so neither live SSE nor
  attach replay, checkpoints, durable messages or logs can carry chain-of-thought. Thinking is
  disabled by default to reduce generation, but the discard boundary does not rely on that setting.
  Exact `<think>...</think>` content is removed by stateful filters at provider, runtime and session
  seams even when tags split across chunks; an unclosed block is dropped. Provider profiles that put
  untagged chain-of-thought in normal content are not admissible because it cannot be distinguished
  safely from the user-facing answer.
- SSE errors are rebuilt from a value-free public registry. An allowlisted code selects its fixed
  message, kind and retryability; unknown runtime/provider errors collapse to
  `AGENT_RUNTIME_ERROR`. Source message, args, result, trace and debug fields are discarded before
  initial streaming, session-hub buffering, attach replay or logging.
- Upload access logs contain only the normalized extension class and byte size, never the raw
  customer filename. PDF scan detection counts extracted text/table cells, never generated page
  labels.

This slice does **not** enable v2 Artifact release, durable task execution, business writes or
production deployment, and it does not claim full `#219` acceptance. `ENABLE_AGENT` is deliberately
`false` by default in application settings, both example environments and Compose, and must remain
false in production. Remaining `#219` gaps include
a real per-user revocable consent/grant record for `approved_external`, an independently attested
provider-profile projection/retention contract (including provider-side logging/deletion), and the
later durable-task/Artifact lifecycle. Until those exist, approved-external customer-file edges
remain disabled and the retention ID above is strictly an application-local statement.

Production enablement is additionally blocked by all of the following:

- `#223`: global/per-user concurrent-run and durable-queue admission, plus per-task/per-step database
  query-count and wall-time budgets. Current payload/event ceilings do not bound SQL execution or
  aggregate/materialization work; the 512 KiB/64-delta cap only bounds plain-chat stream
  amplification. Tool-heavy multi-round workflows need the independent workload gate.
- `#222`: a controlled parser worker/sandbox that enforces multipart admission before framework
  parsing, archive member/expanded-byte/compression-ratio limits for XLSX/DOCX/PDF, and page/pixel,
  CPU, memory and wall-time limits with a hard kill. The current Vision preflight pixel/time checks
  are defense in depth, not a parser sandbox.
- `#224`: bounded generated-work integration gates and representative load/soak tests.
- `#225`: verifiable private endpoint identity. The v1 exact-origin allowlist is explicitly
  `operator_assertion_only_v1`; it is not Tailscale node/certificate/route attestation and does not
  prevent a public FQDN from being mislabeled private to bypass external-file consent.
- `#231`: durable executor/resume safety. The capability fingerprint describes declarations and
  policy metadata, not binary/code provenance; durable resume must also bind a reviewed release
  SHA/build digest and bump implementation versions for behavior changes.
- `#233`: message-body provenance and current-scope revocation. `ChatMessage` does not yet persist
  the required permissions, row subject or source scope for assistant text/checkpoints. Session
  ownership and historical role are not substitutes. Before production, list/history/provider
  replay and `_RunHub` attach-buffer replay must apply current-scope-dominates; legacy/unproven
  sensitive content must be hidden or isolated fail-closed when cost/profit/customer/row
  permissions are revoked. Fresh Artifact-ID filtering does not solve disclosure of sensitive
  values already embedded in persisted or buffered message text.

## Rollback safety gate

Do not roll back this kernel while Agent egress is live. Older images may ignore the new trust-zone,
allowlist and edge-policy variables.

1. Before selecting an older image, deny the entire `/api/agent` namespace (`/api/agent` and every
   `/api/agent/*` route, including upload/preview/download/session routes) at every reachable ingress:
   public proxy, Tailnet listener and any upstream load balancer. `ENABLE_AGENT=false` alone is not a
   rollback boundary because older images may leave file routes active.
2. Cut primary-model and Vision egress at the network/firewall layer, then revoke both provider API
   keys at their issuers. Never copy key values into tickets or logs.
3. From outside each ingress, probe the blocked namespace with representative anonymous, sales,
   admin and owner/non-owner requests. Verify no Agent route or file body is reachable and no model
   destination receives traffic.
4. Only then switch to the older image. Do not downgrade the database. Keep the namespace blocked,
   egress cut and keys revoked while the rollback image runs.
5. Reopen Agent routes only by forward-deploying a security-reviewed image with equivalent identity,
   ownership, trace/reasoning/error projection and egress gates, then repeat the external multi-role
   probes. Issue fresh keys only for that reviewed forward image.
