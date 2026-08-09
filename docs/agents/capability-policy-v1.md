# Agent Capability Policy v1

`#219` establishes the read-only Capability Kernel. Its job is to decide which tool schemas a
model may see and to repeat the same decision immediately before dispatch.

Each tool declares immutable `effects`, `egress`, `sensitivity`, `permission_id`, `enabled` and
handler metadata. The exported policy version and fingerprint are deterministic over the tool
name, function parameter schema and authorization metadata, so future durable tasks can detect a
policy change before resuming.

The provider boundary fails closed:

- `LLM_TRUST_ZONE=unknown` or disabled model-context egress exposes no tools.
- `private` may use business and customer-file capabilities locally after model-context egress is
  enabled; it does not require an external-file authorization.
- `approved_external` may receive business-confidential data after model-context authorization,
  but customer-file content additionally requires `AGENT_EXTERNAL_FILE_EGRESS_ENABLED=true`.
- External Vision always requires the customer-file egress authorization.
- `FILE_READ` and `ARTIFACT_CREATE` capabilities additionally require a stable
  `authn=sys_user` subject. Shared credentials retain only permission-gated business reads.

This slice does **not** enable v2 Artifact release, durable task execution, business writes or
production deployment. Artifact lifecycle, download release and route-level file ownership gates
belong to the subsequent Artifact/task increment.
