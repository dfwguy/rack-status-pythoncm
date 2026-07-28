# AGENTS.md — instructions for AI coding agents

**Single source of truth:** this project's rules, conventions, and hard-won operational
runbooks live in **`.cursor/rules/*.mdc`**. Those Cursor rule files are authoritative for
*every* agent working in this repo — Cursor, Claude Code, and Codex alike. This file exists
only to point non-Cursor agents at them, so there is one place to maintain. **Do not
duplicate rule content here** — edit the `.mdc` files instead.

> `CLAUDE.md` is a symlink to this file, so Claude Code and Codex read the exact same thing.

## Read these first (always apply)

Read these before doing any work — they apply to every task:

- @.cursor/rules/nvis-rack-status.mdc — architecture, key files, UI framework (KUI-first)
- @.cursor/rules/local-dev.mdc — build/run (`start.sh`), SSH mode, Docker, `deploy-scripts.sh`
- @.cursor/rules/cluster-deployments.mdc — Mayo / Firmus / Boston map; **DO NOT mix clusters**
- @.cursor/rules/cluster-naming-conventions.mdc — rack/dashboard id vs real compute node hostname
- @.cursor/rules/dcgm-mndiag-mnubergemm.mdc — DCGM `mndiag` / `mnubergemm` gotchas
- @.cursor/rules/bcm-prolog-rogueprocess.mdc — BCM Slurm "Prolog error" drain root cause

## Read when relevant (task-scoped)

- @.cursor/rules/frontend.mdc — React 19 / KUI frontend conventions
- @.cursor/rules/kui-components.mdc — Kaizen UI component catalog + MUI→KUI migration table
- @.cursor/rules/backend-api.mdc — Flask backend / `/api/*` conventions
- @.cursor/rules/docker-host-exec.mdc — Docker + SSH host-exec details

## Notes

- Each `.mdc` carries YAML frontmatter (`description`, `alwaysApply`) — honor it.
- Cursor **skills** live under `.cursor/skills/` (e.g. `kaizen-ui`) — use them when relevant.
- To change guidance, edit the `.mdc` source of truth — never this pointer or the `CLAUDE.md` symlink.
