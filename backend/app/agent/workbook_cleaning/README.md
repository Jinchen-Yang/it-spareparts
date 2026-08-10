# Workbook cleaning proposal kernel

This package is a dark-mode, pure in-memory slice of Issue #228. It validates an
untrusted AI/algorithm field-change proposal against authoritative local source,
template, rule, and observed-field snapshots. It returns bounded Evidence for a
future Human Interrupt.

Hard boundaries:

- no file, database, network, model, clock, random, route, tool/runtime registry,
  Artifact creation, workbook writing, import, or business mutation;
- every result is `human_review_required`, `executable=false`,
  `artifact_create_allowed=false`, and `business_write_allowed=false`;
- Sheet and column names remain in a future trusted adapter. This package accepts
  and emits only upstream-issued opaque UUID `sheet_ref`, `column_ref`,
  `observed_field_ref`, `proposed_value_ref`, and snapshot/proposal refs;
- Evidence contains no owner, cell value, source/template/rule content hash,
  before/after SHA, whole-proposal fingerprint, or assessment fingerprint. Hashing
  low-entropy values is not anonymization and is deliberately absent;
- `verify_cleaning_assessment(request, assessment)` is the only supported
  consumption boundary. It revalidates both inputs, deterministically re-assesses,
  and constant-time compares canonical bytes. Callers consume only its returned
  fresh instance;
- that comparison detects projection tampering but provides no authenticity,
  authorization, signing, or replay protection. It is explicitly not a substitute
  for the platform `integrity-envelope/v1` or Task-owner binding;
- the opaque refs must later be issued from an immutable, owner-bound authoritative
  registry. This kernel cannot prove their issuer or that a ref was not reused for
  different content; therefore it is not production-capable on its own;
- runtime build/registry fingerprint binding is deferred. No hard-coded or
  self-declared build fingerprint is treated as evidence;
- identity/PN/part columns remain immutable. Amount, quantity, and date changes are
  admitted only as versioned deterministic parse proposals. Semantic rewrite is
  limited to columns classified as `semantic_text`;
- cell limits count UTF-8 bytes. The slice tightens the later full-workflow limits
  to 200 fields, 100 semantic rewrites, 256 KiB proposal bytes, and 256 KiB local
  observed-projection bytes. Larger batches require a new reviewed version and
  pressure evidence;
- source/template authorization, opaque-ref registry, template classifier,
  Task/Plan/Step, Human Interrupt, deterministic workbook executor, safe writer,
  Artifact Set, atomic publication, and final integrity envelope remain blocked on
  Issues #219/#220/#222/#223/#226/#230 and the reviewed Artifact foundation
  currently carried by Draft PR #236.
