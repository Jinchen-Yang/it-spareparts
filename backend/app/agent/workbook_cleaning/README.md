# Workbook cleaning proposal kernel

This package is the first dark-mode slice of Issue #228. It accepts immutable,
version-bound source/template/rule snapshots plus an untrusted AI or algorithmic
field-change proposal. It returns deterministic, hash-only Evidence for a future
Human Interrupt.

Hard boundaries for this slice:

- pure in-memory validation only; no file, database, network, model, clock, or
  random-number access;
- no API route, tool/runtime registration, Artifact creation, workbook writing,
  import, or business mutation;
- every accepted result remains `human_review_required`, `executable=false`,
  `artifact_create_allowed=false`, and `business_write_allowed=false`;
- source values and proposed values are represented only by SHA-256 in Evidence;
  the owner subject is likewise projected only as a SHA-256 binding;
- each proposal `before` value and source-column set must exactly match a separate
  authoritative local `observed_fields` projection bound to the source SHA and
  projection implementation version; the AI cannot self-certify source values;
- SHA-256 fingerprints are replay/change bindings, not authorization, signatures,
  or the platform `integrity-envelope/v1`. A future completed Step must wrap the
  bounded Evidence with that authoritative envelope;
- identity/PN/part columns are immutable in this slice. Amount, quantity, and date
  changes are admitted only as versioned deterministic parse proposals. Semantic
  rewrite is limited to columns classified as `semantic_text`;
- this review kernel deliberately tightens the later full-workflow budgets to at
  most 200 proposed fields, 100 semantic rewrites, and a 256 KiB serialized
  proposal. Larger batches need a new reviewed version and pressure evidence;
- source/template authorization, template classification, Task/Plan/Step,
  Human Interrupt, deterministic workbook execution, safe writer, Artifact Set,
  and atomic publication remain blocked on Issues #219/#220/#222/#223/#226/#230.
