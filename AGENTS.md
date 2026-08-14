# AGENTS.md — instructions for AI coding agents

**Single source of truth:** this project's rules, conventions, and hard-won operational
runbooks live in **`.cursor/rules/*.mdc`**. Those Cursor rule files are authoritative for
*every* agent working in this repo — Cursor, Claude Code, and Codex alike. This file exists
only to point non-Cursor agents at them, so there is one place to maintain. **Do not
duplicate rule content here** — edit the `.mdc` files instead.

> `CLAUDE.md` is a symlink to this file, so Claude Code and Codex read the exact same thing.

## Start here if you are resuming work on the Nscale TX OVA

**[docs/HANDOVER_2026-08-14.md](docs/HANDOVER_2026-08-14.md)** — endpoints moved out of `app.py`
into `backend/api/`, and nine files that were uncommitted on the OVA were harvested into
`694ff28`. Read it before editing `backend/app.py` or discarding anything in that working tree.

## Read these first (always apply)

Read these before doing any work — they apply to every task:

- @.cursor/rules/core-designs.mdc — **read this before adding any new mechanism.** The product's
 **first-class citizens** — connection/transport, catalog/Tools, Project/Cluster, the workflow
 state machine, configuration resolution, platform/scheduler providers, the results pipeline, the
 durable run registry and locks, naming templates, artifacts/reports, auth, frontend contexts —
 which you build *on*, not beside. Also the deployment invariants that have each caused an outage
- @.cursor/rules/nvis-rack-status.mdc — architecture, key files, UI framework (KUI-first)
- @.cursor/rules/local-dev.mdc — build/run (`start.sh`), SSH mode, Docker, `deploy-scripts.sh`
- @.cursor/rules/cluster-deployments.mdc — Mayo / Firmus / Boston map; **DO NOT mix clusters**
- @.cursor/rules/cluster-naming-conventions.mdc — rack/dashboard id vs real compute node hostname
- @.cursor/rules/dcgm-mndiag-mnubergemm.mdc — DCGM `mndiag` / `mnubergemm` gotchas
- @.cursor/rules/bcm-prolog-rogueprocess.mdc — BCM Slurm "Prolog error" drain root cause
- @.cursor/rules/multirack-nccl-hcoll-nvls.mdc — multi-rack NCCL/MPI: HCOLL abort + NVLS cliqueId hang
- @.cursor/rules/dell-idrac-datacenter-license-hgx.mdc — Dell HGX iDRAC Datacenter license gates GPU OOB power APIs

## Read when relevant (task-scoped)

- @.cursor/rules/frontend.mdc — React 19 / KUI frontend conventions
- @.cursor/rules/kui-components.mdc — Kaizen UI component catalog + MUI→KUI migration table
- @.cursor/rules/backend-api.mdc — Flask backend / `/api/*` conventions
- @.cursor/rules/docker-host-exec.mdc — Docker + SSH host-exec details

## Notes

- Each `.mdc` carries YAML frontmatter (`description`, `alwaysApply`) — honor it.
- Cursor **skills** live under `.cursor/skills/` (e.g. `kaizen-ui`) — use them when relevant.
- To change guidance, edit the `.mdc` source of truth — never this pointer or the `CLAUDE.md` symlink.
