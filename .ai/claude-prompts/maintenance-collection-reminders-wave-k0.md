You are the Claude Code implementation lead inside the trusted main workspace.

The outer Codex process is the only review, Git commit, push, merge, release, and production owner. You and every teammate are prohibited from all Git writes, SSH, Docker, deployment, backup, GitHub mutation, and production access.

Read these files completely before acting:

- .ai/MAINTENANCE_COLLECTION_REMINDERS_DESIGN.md
- .ai/MAINTENANCE_COLLECTION_REMINDERS_IMPLEMENTATION_PLAN.md
- .ai/contracts/maintenance-collections/project-manager-xls-v1.yaml
- .ai/contracts/maintenance-collections/collection-reminders-api-v1.yaml

Explicitly use the Agent tool and delegate Wave K0 to the schema_integrator teammate. Do not start reminder_backend, xls_importer, reminder_frontend, or test_reviewer in this wave.

Execute Task 1 only with strict red-green TDD. Preserve unrelated untracked files. Do not weaken existing tests. When Task 1 focused tests and generated dependency checks finish, ensure the teammate is stopped, return the exact changed-file list, the verified initial red failures, final commands/results, and any blocker. Then stop and wait for the outer Codex review barrier.

The only executable checks permitted in this wave are these exact fixed-runner calls:

- `python3 .ai/claude-prompts/run_collection_reminders_checks.py k0-migration`
- `python3 .ai/claude-prompts/run_collection_reminders_checks.py k0-sync-dependencies`
- `python3 .ai/claude-prompts/run_collection_reminders_checks.py k0-focused`
- `python3 .ai/claude-prompts/run_collection_reminders_checks.py k0-alembic-heads`
- `python3 .ai/claude-prompts/run_collection_reminders_checks.py k0-alembic-rehearsal`
- `python3 .ai/claude-prompts/run_collection_reminders_checks.py sbom-check`
- `python3 .ai/claude-prompts/run_collection_reminders_checks.py git-diff-check`

After the migration and focused tests are green, run `k0-alembic-heads` and then
`k0-alembic-rehearsal`. The rehearsal creates and owns a disposable local
`spareparts_test_<pid>_<token>` database, upgrades it first to `d9f1a3c7e5b2`
and then to the new head, runs `alembic check`, and verifies ownership again
before cleanup. The outer process supplies only an explicit local test database
credential base; never fall back to application defaults or reuse that base as
the rehearsal target.

Never wrap, concatenate, redirect, or append shell syntax to these calls. File inspection uses Read/Glob/Grep, not Bash.
