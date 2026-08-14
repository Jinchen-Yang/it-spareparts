The outer Codex owner has frozen, reviewed, fixed, and committed the parallel writers. Explicitly delegate the final shared integration work to schema_integrator only.

The integrator may edit only the shared integration files and tests enumerated in Task 7: router registration and Beta gate coverage. Generated dependency/package metadata is owned by the outer Codex metadata barrier; if any such drift remains, report a blocker and stop. The collection-plan API remains self-contained and must not modify the generic maintenance upload helper. The integrator must not redesign DTOs or modify lane-owned domain behavior without reporting a blocker.

Run integration tests, backend full tests, frontend full tests/build, Alembic heads/check, SBOM freshness, git diff check, and the privacy/forbidden-terminology scans in the plan. Do not run Git writes, SSH, Docker, deployment, backup, GitHub mutation, or production access.

The only executable checks permitted are the exact fixed-runner IDs `integration-backend`, `integration-backend-full`, `integration-frontend`, `frontend-build`, `k0-alembic-heads`, `k0-alembic-rehearsal`, `sbom-check`, and `git-diff-check`. The command form is always `python3 .ai/claude-prompts/run_collection_reminders_checks.py CHECK_ID`; never wrap or append shell syntax. File inspection uses Read/Glob/Grep.

After the integrator stops, return exact changed paths, full command results, and any blocker, then exit. Do not start test_reviewer. The outer Codex owner must freeze this writer diff and start the reviewer in a separate fresh read-only invocation.
