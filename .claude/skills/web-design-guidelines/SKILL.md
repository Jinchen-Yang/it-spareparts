---
name: web-design-guidelines
description: Review UI code for Web Interface Guidelines compliance. Use when asked to "review my UI", "check accessibility", "audit design", "review UX", or "check my site against best practices". For it-spareparts (React 18 + Ant Design admin app), keep existing AntD interaction and visual conventions.
metadata:
  author: vercel
  version: "1.0.0"
  argument-hint: <file-or-pattern>
  local-scope: pinned-rules-do-not-refetch
---

# Web Interface Guidelines

Review files for compliance with Web Interface Guidelines.

> **Project scope (it-spareparts):** rules are **vendored locally** for reproducible reviews.
> Use `references/web-interface-guidelines.command.md` in this skill directory.
> Only fetch the remote source when the user explicitly asks to refresh the rules.
> Preserve existing Ant Design component conventions; flag issues, don't restyle pages.

## How It Works

1. Read the pinned guidelines at `references/web-interface-guidelines.command.md`
2. Read the specified files (or prompt user for files/pattern)
3. Check against all rules in the guidelines
4. Output findings in the terse `file:line` format the guidelines specify

## Guidelines Source (pinned)

Local copy: `references/web-interface-guidelines.command.md`
Upstream (refresh only on explicit request):
`https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md`

## Usage

When a user provides a file or pattern argument:
1. Read the pinned guidelines file above
2. Read the specified files
3. Apply all rules from the guidelines
4. Output findings using the format specified in the guidelines

If no files specified, ask the user which files to review.
