# Skill provenance & local adaptations

External skills are vendored as **copies** (not symlinks) so their content is fixed in git
and reviewed before any update. Re-review the upstream diff manually before updating.

| Skill | Upstream | Vendored from commit | Local adaptations |
|---|---|---|---|
| `vercel-react-best-practices` | `vercel-labs/agent-skills` (`skills/react-best-practices`, MIT) | `063bee94c3f4df8453406c830b0a7df0f2860278` (2026-09-11) | Directory renamed to match frontmatter `name` (OpenCode requires folder name == skill name). Description + header scoped to React 18.3 + Vite 7 + AntD 5; `server-*`/Next.js/RSC/React-19-only rules excluded. Internal `AGENTS.md` renamed to `FULL_GUIDE.md` so it is not picked up as project instructions. |
| `web-design-guidelines` | `vercel-labs/agent-skills` (MIT) | `063bee94c3f4df8453406c830b0a7df0f2860278` (2026-09-11) | Rules **pinned locally**: `references/web-interface-guidelines.command.md` vendored from `vercel-labs/web-interface-guidelines` @ `e3d624baaf29dc1fc645aff3e38f03e564d2d6b1`. Skill reads the local copy instead of fetching remote `main` on every review; refresh only on explicit request. |
| `supabase-postgres-best-practices` | `supabase/agent-skills` (MIT) | `8331f910845103c08d51f6ca1d86ebb7d1f745e3` (2026-09-11) | Header note: plain PostgreSQL 15 + SQLAlchemy 2 + Alembic, **not** Supabase; apply generic Postgres guidance only; production DB read-only for agents. |

Project-authored skills (no upstream): `run-it-spareparts`, `db-change-safety`,
`vertical-slice-contract`, `regression-gate`.

Shared by Claude Code and OpenCode from this single `.claude/skills/` tree — do not
duplicate into `.agents/skills/` or `.opencode/skills/` (OpenCode scans all of them and
requires unique names).
