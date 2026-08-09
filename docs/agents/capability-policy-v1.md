# Agent Capability Policy v1

`#219` establishes the read-only Capability Kernel. Its job is to decide which tool schemas a
model may see and to repeat the same decision immediately before dispatch.

Each tool declares immutable `effects`, `egress`, `sensitivity`, `permission_id`, `enabled`,
`ToolBudget`, argument validator, handler and `implementation_version` metadata. Dispatch resolves
the immutable `ToolSpec` and invokes only `ToolSpec.handler`; `_REGISTRY` is a read-only compatibility
projection and is not an execution authority. Before a handler can touch a database or file, one
fail-closed validator rejects undeclared keys, schema/type errors and resource-budget overruns with
stable, non-retriable codes (`AGENT_TOOL_ARGS_INVALID`, `AGENT_TOOL_BUDGET_EXCEEDED` or
`AGENT_TOOL_VALIDATOR_FAILED`). Budgets cover payload shape plus tool-specific query/PN length,
limit, days, page, rows/items/cells, sheet and output-name bounds.

The static fingerprint is deterministic over tool name, function parameter schema, effects,
egress, sensitivity, permission policy, stable-subject effects, budget, validator/handler identity
and implementation version. Descriptions, arguments and secrets are excluded. A separate runtime
fingerprint covers the current trust zone, both egress switches, normalized model-provider origin
and normalized private-origin allowlist. Keeping these fingerprints separate lets durable tasks
distinguish a code-policy change from a deployment-policy change.

The provider boundary fails closed:

- `LLM_TRUST_ZONE=unknown` or disabled model-context egress exposes no tools.
- `private` is not trusted by label alone. Model-context egress must be enabled and the normalized
  `LLM_BASE_URL` origin must exactly match one of `LLM_PRIVATE_BASE_URLS`. The allowlist defaults to
  empty, accepts only HTTP(S) origins without userinfo/path/query/fragment, and does not infer trust
  from RFC1918 addressing. Any malformed allowlist entry keeps private egress closed. Once matched,
  private may use business and customer-file capabilities locally without an external-file
  authorization.
- `approved_external` may receive business-confidential data after model-context authorization,
  but customer-file content additionally requires `AGENT_EXTERNAL_FILE_EGRESS_ENABLED=true`.
- External Vision always requires the customer-file egress authorization.
- `FILE_READ` and `ARTIFACT_CREATE` capabilities additionally require a stable
  `authn=sys_user` subject. Shared credentials retain only permission-gated business reads.
- Upload access logs contain only the normalized extension class and byte size, never the raw
  customer filename. PDF scan detection counts extracted text/table cells, never generated page
  labels.

This slice does **not** enable v2 Artifact release, durable task execution, business writes or
production deployment. Artifact lifecycle, download release and route-level file ownership gates
belong to the subsequent Artifact/task increment.
