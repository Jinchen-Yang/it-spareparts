# v1.22 Compose Cutover And Root Lineage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the v1.22 release controller atomically install the reviewed candidate Compose file before migration, restore the previous Compose/image lineage on rollback, and atomically commit the new root release state only after the 30-minute observation passes.

**Architecture:** Add two explicit release phases: `compose_installed` between `restore_checked` and `migrated`, and `observe_30_passed` before the root lineage is committed. The controller owns every Compose/root-state mutation, binds it to the fresh backup and package manifest, and uses compare-and-swap plus fsync/rename so retries are deterministic.

**Tech Stack:** Bash 5, Python 3 standard library, Docker Compose v2, pytest subprocess fixtures.

## Global Constraints

- Only `.deploy/v122_collection_reminders_release.sh`, `backend/tests/test_v122_collection_reminders_release_control.py`, and this plan may change.
- The existing protected main branch, required CI, package manifest trust anchor, global release lock, backup checksums, and production-ready gate remain mandatory.
- Never downgrade `c8e2a4f6b1d3`, delete production data, or restore DB/uploads automatically.
- The preliminary and final-candidate rehearsal flows must not install candidate Compose or commit root lineage.
- All root-owned mutations use a same-directory temporary regular file, preserve owner/mode, fsync the file, atomic rename, and fsync the parent directory.
- Tests use different old and candidate Compose bytes; a fixture that preinstalls candidate Compose is invalid for lifecycle tests.

---

### Task 1: Bind And Install Candidate Compose

**Files:**
- Modify: `.deploy/v122_collection_reminders_release.sh`
- Test: `backend/tests/test_v122_collection_reminders_release_control.py`

**Interfaces:**
- Consumes: `backup_dir`, `backup_manifest_sha256`, packaged `candidate-compose.yml`, current root release state.
- Produces: command `install-compose`; phase `compose_installed`; state fields `previous_compose_sha256`, `candidate_compose_sha256`, `pre_cutover_root_state_sha256`.

- [ ] Write failing tests with old active Compose bytes different from candidate bytes. Assert `migrate` rejects `restore_checked`, and `install-compose` rejects a non-production-ready package, wrong phase, changed active Compose, changed root state, changed backup manifest, invalid candidate config, and symlink/ownership drift before replacing the active file.
- [ ] Run the exact RED selection and record that the current controller has no `install-compose` command.
- [ ] Add `install-compose` to usage and command dispatch. Require `production_ready=true` and phase `restore_checked`.
- [ ] Read the state-bound `backup-manifest.json`; require its `active_compose_sha256`, `candidate_compose_sha256`, and `root_release_state_sha256` to match the live old Compose, packaged candidate Compose, and live root state respectively. Revalidate package/preflight and run candidate `docker compose config -q` before mutation.
- [ ] Close collection writes, stop app, atomically install candidate Compose, verify its hash and `compose config -q`, then advance to `compose_installed`. On any pre-rename failure leave old Compose unchanged; on post-rename/state-write failure retain enough exact state for idempotent retry.
- [ ] Make `migrate` require `compose_installed` and read back `MAINTENANCE_COLLECTION_PLAN_APPLY_ENABLED=false` plus the manifest canary ID from the recreated candidate app before migration.
- [ ] Run the Task 1 tests and the existing migrate/deploy tests.

### Task 2: Restore Previous Compose And Images On Rollback

**Files:**
- Modify: `.deploy/v122_collection_reminders_release.sh`
- Test: `backend/tests/test_v122_collection_reminders_release_control.py`

**Interfaces:**
- Consumes: phase/state from Task 1 and state-bound `backup_dir/docker-compose.yml`.
- Produces: rollback evidence binding candidate close/readback, previous image IDs, restored Compose SHA, health/readiness, and unchanged old root-state SHA.

- [ ] Write RED tests for rollback from `compose_installed`, `migrated`, `deployed`, `canary`, and observation phases using distinct old/candidate Compose files. Assert candidate Compose remains active while flags/actions close and previous images are restored; only then may old Compose be atomically restored and previous images recreated.
- [ ] Add failpoint tests for candidate flag readback failure, previous-image mismatch, old-Compose checksum drift, atomic restore failure, old-image recreate failure, health/readiness failure, and root-state drift. Each must leave phase unadvanced and must not claim rollback success.
- [ ] Preserve the existing pre-cutover rollback path for `preflight`, `frozen`, `backup`, and `restore_checked`: restore previous images using the still-active old Compose without requiring new flag readback.
- [ ] For post-install phases, restore permissions first when required, close/read back flags under candidate Compose, restore and verify previous exact images, atomically restore backup Compose, recreate previous images under old Compose, verify old Compose hash, previous image IDs, health/readiness, DB remains additive, and live root state still equals the pre-cutover SHA.
- [ ] Persist hashes of the rollback/Compose evidence and advance to `rolled_back`; never update root release state on rollback.
- [ ] Run all rollback and interruption tests.

### Task 3: Commit Root Release State After Observation

**Files:**
- Modify: `.deploy/v122_collection_reminders_release.sh`
- Test: `backend/tests/test_v122_collection_reminders_release_control.py`

**Interfaces:**
- Consumes: phase `observe_30_passed`, target/image/Compose values from the verified manifest, and `pre_cutover_root_state_sha256` from Task 1.
- Produces: command `commit-release`; atomically updated root release state; final phase `observed`; `root_release_state_sha256` in evidence.

- [ ] Change successful `observe 30` to phase `observe_30_passed`; add RED tests proving it does not mutate root state and that `commit-release` is the only transition to `observed`.
- [ ] Add `commit-release`, requiring `production_ready=true` and `observe_30_passed`. Reverify package, candidate Compose hash/config, exact running app/frontend/database image IDs, DB revision `c8e2a4f6b1d3`, health/readiness, and all observation evidence hashes.
- [ ] Build the exact root JSON: existing format, `production_sha=target_sha`, target app/frontend image IDs, manifest database image ID, candidate Compose SHA, and `adopted_for=v122-collection-reminders`.
- [ ] Compare-and-swap the current root file from `pre_cutover_root_state_sha256` to the exact new JSON via fsync/rename. If a retry finds the exact new JSON already installed, treat it as crash-resume; any third value fails closed.
- [ ] Bind the new root-state SHA into evidence and advance to `observed`. Remove `observed` from this package's normal rollback phases; a later rollback requires a new controlled release/incident authority.
- [ ] Add tests for root CAS drift, write/rename/state-write crash points, exact-new retry, future child preflight using the new root state, and zero secret content.
- [ ] Run all observation/root-lineage tests.

### Task 4: Full Verification And Review

**Files:**
- Modify only the three files listed in Global Constraints.

- [ ] Run `bash -n .deploy/v122_collection_reminders_release.sh`.
- [ ] Run `python3 .deploy/v122_collection_reminders_static_test.py`.
- [ ] Run `backend/.venv/bin/pytest -q backend/tests/test_v122_collection_reminders_release_control.py`.
- [ ] Run `git diff --check` and verify the isolated worktree contains no unrelated or protected user files.
- [ ] Freeze SHA-256 for all changed files and obtain two independent read-only reviews with P0=0 and P1=0 before committing.
- [ ] Commit exact files, push a dedicated branch, open a normal PR, resolve actionable review threads, and merge only after both required CI jobs succeed.
- [ ] Treat every 5bfc build/package as superseded; rebuild exact post-merge main before any backup, rehearsal, migration, or deployment.
