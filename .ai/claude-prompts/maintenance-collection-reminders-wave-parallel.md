The outer Codex owner has completed and committed the Wave K0 review barrier. First verify the current HEAD and contracts, then explicitly create a three-teammate Agent Team:

1. reminder_backend executes Task 2 only.
2. xls_importer executes Tasks 3 and 4 only.
3. reminder_frontend executes Tasks 5 and 6 only.

All three writers may run concurrently because their ownership is disjoint. They must not edit shared main.py, generic maintenance.py, beta gate tests, schema/migration/permissions, dependency locks, or each other's files. They must not run Git writes, SSH, Docker, deployment, backup, GitHub mutation, or production access. They must use red-green TDD and the frozen API v1 contract; no teammate may invent alternate DTO fields.

When all three finish, stop every writer. Return per-owner exact changed paths, first red failures, green commands/results, integration requests, and risks. Do not start test_reviewer and do not integrate shared files. Wait for the outer Codex freeze/review/commit barrier.

Each owner may execute only its exact fixed-runner checks:

- reminder_backend: `reminder-backend`, then `git-diff-check`.
- xls_importer: `xls-parser`, `xls-import`, `sbom-check`, then `git-diff-check`.
- reminder_frontend: `frontend-api`, `frontend-page`, `frontend-build`, then `git-diff-check`.

The command form is always `python3 .ai/claude-prompts/run_collection_reminders_checks.py CHECK_ID`. Never wrap, concatenate, redirect, or append shell syntax. File inspection uses Read/Glob/Grep, not Bash.
