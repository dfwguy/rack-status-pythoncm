# AGENTS.md — instructions for AI coding agents

**Single source of truth:** this project's rules, conventions, and hard-won operational
runbooks live in **`.cursor/rules/*.mdc`**. Those Cursor rule files are authoritative for
*every* agent working in this repo — Cursor, Claude Code, and Codex alike. This file exists
only to point non-Cursor agents at them, so there is one place to maintain. **Do not
duplicate rule content here** — edit the `.mdc` files instead.

> `CLAUDE.md` is a symlink to this file, so Claude Code and Codex read the exact same thing.
>
> The `@.cursor/claude-rules/*.md` imports below are symlinks to the `.mdc` files: Claude Code silently
> skips `@`-imports of `.mdc`, so it can only load them under a `.md` name. After adding, renaming
> or removing a rule, run `bash scripts/sync_claude_rules.sh` and update this file; CI runs it
> with `--check`.

## Start here if you are resuming work on the Nscale TX OVA

**[docs/HANDOVER_2026-08-14.md](docs/HANDOVER_2026-08-14.md)** — endpoints moved out of `app.py`
into `backend/api/`, and nine files that were uncommitted on the OVA were harvested into
`694ff28`. Read it before editing `backend/app.py` or discarding anything in that working tree.

## Read these first (always apply)

Read these before doing any work — they apply to every task:

- @.cursor/claude-rules/build-cost-is-not-a-tradeoff.md — **how to decide, before deciding anything.**
  Implementation effort is not a design input: choose for quality, simplicity, robustness,
  scalability and maintainability, and never trade the right structure for a smaller diff
- @.cursor/claude-rules/core-designs.md — **read this before adding any new mechanism.** The product's
 **first-class citizens** — connection/transport, catalog/Tools, Project/Cluster, the workflow
 state machine, configuration resolution, platform/scheduler providers, the results pipeline, the
 durable run registry and locks, naming templates, artifacts/reports, auth, frontend contexts —
 which you build *on*, not beside. Also the deployment invariants that have each caused an outage
- @.cursor/claude-rules/nvis-rack-status.md — architecture, key files, UI framework (KUI-first)
- @.cursor/claude-rules/local-dev.md — build/run (`start.sh`), SSH mode, Docker, `deploy-scripts.sh`
- @.cursor/claude-rules/cluster-deployments.md — Mayo / Firmus / Boston map; **DO NOT mix clusters**
- @.cursor/claude-rules/cluster-naming-conventions.md — rack/dashboard id vs real compute node hostname
- @.cursor/claude-rules/dcgm-mndiag-mnubergemm.md — DCGM `mndiag` / `mnubergemm` gotchas
- @.cursor/claude-rules/bcm-prolog-rogueprocess.md — BCM Slurm "Prolog error" drain root cause
- @.cursor/claude-rules/multirack-nccl-hcoll-nvls.md — multi-rack NCCL/MPI: HCOLL abort + NVLS cliqueId hang
- @.cursor/claude-rules/dell-idrac-datacenter-license-hgx.md — Dell HGX iDRAC Datacenter license gates GPU OOB power APIs
- @.cursor/claude-rules/identity-auth-traps.md — `backend/identity/` traps: coarse-value comparisons ("add a
  second to be safe" is the wrong fix), read paths that defeat their own cache, unsafe `srp`/PyJWT
  defaults, and why fail-closed controls must not be defaulted permissive
- @.cursor/claude-rules/storage-engines.md — **which storage engine to use**: the join test, the four
  paradigms already in use, and why "put it in a SQL table" is usually the wrong default
- @.cursor/claude-rules/local-vs-ci-tree.md — why "it passes locally" says nothing about CI: untracked
  files, missing tools, and the unbuilt frontend (`static/spa/`)

## Read when relevant (task-scoped)

Not imported, so they don't cost context on every task — open the file before working in its area.

- `.cursor/rules/frontend.mdc` — React 19 / KUI frontend conventions
- `.cursor/rules/kui-components.mdc` — Kaizen UI component catalog + MUI→KUI migration table
- `.cursor/rules/backend-api.mdc` — Flask backend / `/api/*` conventions
- `.cursor/rules/docker-host-exec.mdc` — Docker + SSH host-exec details
- `.cursor/rules/docs-diataxis.mdc` — writing anything under `docs/`: the Diátaxis compass as a per-page test

## Notes

- Each `.mdc` carries YAML frontmatter (`description`, `alwaysApply`) — honor it.
- Cursor **skills** live under `.cursor/skills/` (e.g. `kaizen-ui`) — use them when relevant.
- To change guidance, edit the `.mdc` source of truth — never this pointer or the `CLAUDE.md` symlink.
