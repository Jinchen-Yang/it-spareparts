# B1 core implementation and independent transport review

Scope: development candidates only. No production access or mutation; no commit made by this worker.

## Implemented

- Public transaction facade: typed create/profile/availability commands; consistent single-part snapshots; one-query bounded list summaries; bounded audit cursor pagination; current-scope receipt retrieval.
- Independent identity/profile/field/availability revisions; category pair baseline; ABA detection; permanent manual-spec deletion tombstones; explicit manufacturer field without changing identity/governance status.
- Transactional receipts keyed by canonical principal/client/request UUID with canonical command hash; current authorization before replay; replay before old preconditions; latest permitted projection separated from applied revisions.
- Append-only audit with actor/executor/entrypoint/provenance, request/correlation fields and independent revision vectors. Public AI text cannot directly become effective profile data.
- Batch legacy mutation protocol with sorted parent locks, batched profile/spec/alias snapshots, identity group invalidation, and no per-PN SELECT/flush loop. Ordinary edits on independent PNs proceed independently.
- Shared manual-create duplicate gate and sorted compact keys; source-authoritative imports take shared creation gate. This is scoped to creation, not all profile edits. A long import can delay manual creation until transaction end; existing host timeout/retry behavior remains relevant.
- Migration adds state/audit/receipts and nullable positive actor_user_id snapshots to import job/batch without guessing historical identities. Initial PN IDs/status/exclusion unchanged. Downgrade refuses losing adopted data, deletion protection, receipts or actor provenance.

## Focused verification

Command: PYTEST_DATABASE_BASE_URL=postgresql+psycopg://spareparts:spareparts@127.0.0.1:32785/spareparts_test .venv/bin/pytest tests/test_pn_catalog_core.py -q --tb=short

Final result: 27 passed in 14.70s. Tests use isolated per-process PostgreSQL database creation/migrations/cleanup. Coverage includes real concurrent same-field conflicts, concurrent same-key replay, independent PN writes, rollback, category fields, tombstone restore, current permissions, hidden-field search, identity axes, idempotency domains, duplicate confirmation, bounded audit pagination, batch query count, resident ORM stale-spec snapshots and migration downgrade guards.

Ruff on all worker-owned Python files passed. git diff --check passed.

## Independent findings and resolution

- Shop GET delegation bound X-Request-ID, but itsp previously regenerated request ID for GET. Root preserved validated tracing UUID and rejects duplicate/conflicting identifiers. Reviewed fixed middleware.
- Shop parsed incoming JSON before its outbound byte cap. Shop owner added pre-parse 64 KiB prefix middleware and tests.
- Shop response models rejected additive optional fields. Shop owner separated tolerant response models from strict command models and added tests.
- Shop invalid JSON returned 422 instead of the authority's 400 request.invalid_json. Shop owner aligned it and added regression.
- itsp accepted nonpositive IDs and prefix 404/405 bypassed the error envelope. Root added positive path bounds and prefix-scoped error handling.
- Independent core reviewer reproduced new-vs-legacy creation race and ungranted derived-spec writes. Shared creation gate now covers both; profile/create precompute actual derived spec delta and enforce current read/write permission before changes. Both are regression tested.

## Cross-repository response evidence

A temporary producer exercised real itsp HTTP plus PostgreSQL through the explicit trusted test authorization port. Its eight captured create/get/patch/replay/list/audit/receipt/conflict responses were then consumed by shop's actual SharedPnClient and generated response DTOs. All eight matched status, schema, request/replay/error semantics. Evidence: /tmp/pn-shared-refactor-20261023/shared-pn-http-review-evidence.json. Temporary producer test was removed.

This is recorded-response transport replay, not live-network delegation acceptance. New permission-framework resolver remains a host integration port and fails closed when absent; it has not been treated as delivered production authorization. Narrow-scope list queries remain explicitly disabled until the registered SQL scope adapter is integrated.
