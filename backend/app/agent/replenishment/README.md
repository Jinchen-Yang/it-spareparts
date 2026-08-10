# Replenishment policy kernel (dark)

This package is the deterministic, pure-function slice of GitHub #227. It is
deliberately **dark**: nothing here is registered as an API route, Agent tool,
runtime capability, database adapter, or LangGraph node. It performs no I/O and
cannot write business data.

## Contract

`evaluate_replenishment()` accepts frozen, strict Pydantic models and returns a
frozen decision/evidence projection. Its input is post-resolution: the
canonical `dim_part.id` is mandatory and both commercial projections must bind
to that same ID. PN text is only an optional display snapshot and is never an
identity key. `ReplenishmentRequest` intentionally has no `as_of` field. The
durable Task layer must derive and freeze `ServerReviewContext.as_of` in
`Asia/Shanghai` when the Task is created; retries and resumes must reuse that
value. The source adapter must also provide a bounded application reference and
lower-hex SHA-256 source snapshot fingerprint.

The commercial query interval is the closed range
`[as_of - 6 calendar months, as_of]`. Each purchase and sales projection must
declare that exact query window independently. A different or future query
window is a technical contract failure.

Zero counts are eligible for `RPL-100` only when both sides have all of the
following:

- `completeness_status=complete`;
- `coverage_through >= as_of`;
- verified lineage and a last-successful-import marker;
- at least one successful, correctly typed batch manifest;
- matching import-batch, raw-file, and archived-file SHA-256 values.

Known stale, partial, unknown, or missing proof produces
`need_info/RPL-090-source-coverage-incomplete`. Broken lineage, invalid batch
state/type, missing archive, hash mismatch/content drift, or query-window drift
raises `ReplenishmentTechnicalError` with a stable code; callers must route that
to Task retry/fail and must not seal a business outcome.

Every supporting reference is versioned and bound to the same canonical part
ID and source snapshot fingerprint as the request. A cross-part or
cross-snapshot pool, maintenance, or business-context reference is a technical
failure, not a usable caveat.

The only possible outcomes are `need_info`, `recommend_reject`, and
`human_review_required`. Complete purchase=0 and sales=0 always locks
`recommend_reject` with `overrideable=false`; pool, maintenance, and business
context can only add caveats. Multiple active pools become `need_info` only
after that hard gate passes. The only policy model in this slice is the
threshold-free `replenishment-v1-shadow`, so all surviving candidates remain
`human_review_required` with `support_class=unscored`. Approval is not part of
the type system.

The final outcome, rule code, overrideability, support class, and caveats live
inside the same deeply immutable, self-validating Evidence payload. The outer
decision object only projects those sealed values and cannot reassemble the
same facts with another conclusion. The payload also binds the application
reference, source snapshot fingerprint, canonical part ID, requested quantity,
`as_of`, closed window, Policy version, and rule implementation version.
Commercial lineage includes coverage/completeness, the
last-successful-import marker, counts, and verified batch IDs and hashes, plus
opaque typed references. Semantically unordered batch and supporting-reference
collections are sorted and deduplicated before sealing, so input permutations
produce byte-identical JSON. Evidence has no filename, path, order ID,
customer, vendor, price, or SN fields.

## Activation gate

Real application canary remains blocked. Production wiring requires #219
(Capability Gateway), #223 (Durable Agent Task), #224 (Query Broker), #226
(versioned workflow/Human Interrupt), and especially #231 (real source snapshot
binding and review queue), plus their independent security and read-only
acceptance. Until those dependencies are complete, this package is suitable
only for unit tests and explicitly controlled structured shadow evaluation.
