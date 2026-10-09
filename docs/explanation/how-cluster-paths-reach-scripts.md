---
diataxis: explanation
description: Why every on-cluster path a script uses comes from the project/cluster record and one shell resolver, never from the script — the roots, how they travel, and how to fix the next path bug.
---

# How cluster paths reach scripts

Rack Status ships one set of scripts to many clusters. Each cluster keeps its scripts, logs,
toolchains and container images in different places, so the scripts must not know any of those
places. This page explains the design that makes that true, why it is shaped this way, and the
procedure to follow when a path or prefix goes wrong. It complements
[the cluster home directory](the-cluster-home-directory.md), which argues for consolidating the
roots under one home, and [how a script path is chosen](how-a-script-path-is-chosen.md), which
covers which *copy* of a script runs.

:::{note}
**Status on `main`.** The rule on this page applies to every change now. The implementation it
describes — the `workspace_dir` and image-directory cluster fields, `lib/llmb_paths.inc.sh`,
`lib/node_images.inc.sh`, `lib/node_config.inc.sh` and the `test_cluster_workspace.py` guard — was
built on the `nscale-nc-app` integration branch and lands here when that branch merges. Until then
a change on `main` adds a missing root as described in *Fixing the next path or prefix bug* below,
or builds on `nscale-nc-app`; it never ships a literal path as a stopgap.
:::

## The rule

> **A path is a site fact. It lives in the project/cluster record, travels as an environment
> variable, and is turned into concrete paths by exactly one shell resolver. A script never
> carries a site path, and never guesses one.**

Changing a project or cluster setting must be enough to move everything that depends on it. If a
new deployment needs a script edited, the design has been bypassed somewhere.

## Why: the bugs this replaced

Every one of these reached a real cluster (most on Nscale NC, 2026-10) and every one had the same
cause — a path baked into a script, or a default that pointed at another site:

| Symptom | Baked-in path | Where it really was |
|---|---|---|
| Single-rack NeMo died at `mkdir -p /raid/data` | `/raid/data` (Boston/Mayo DGX) | NC nodes have no `/raid`; images at `/opt/containers` |
| NCCL sweep: `mkdir: cannot create directory '/cluster'` | `/cluster/benchmark/harish/rack_status/logs` | the cluster's `log_dir`, `/home/cmods/nvis/logs` |
| NCCL sweep: `libnccl.so.2 not found (looked: /home/*/workspace/...)` | a `/home/*` glob standing in for a missing setting | `/home/cmods/workspace` |
| Tools installed somewhere the launchers never read | `/home/cmsupport/workspace` fallback | that directory exists on no current cluster |
| NCCL Benchmark (Stage-1): `llmb-run not found (searched under LLMB_INSTALL=/home/nvis/dgxc-benchmarking)` | `/home/nvis/...` | `/home/cmods/workspace/llmb` |
| Every launch on NC: `image is missing or empty on the launch host` | assumed the launch host sees the shared image store | the BCM head mounts only `/home`; `/cluster` is on the app server only |

Each was "fixed" at least once by changing the literal to the next cluster's value — which is how
`/home/nvis`, `/home/cmods`, `/home/cmsupport` and `/home/*` all came to coexist in one tree.
Per-script defaults do not converge; they drift.

## The roots

Every on-cluster path derives from one of these. The left column is what an operator edits
(Projects → cluster); everything to the right follows from it.

| Root | Project/cluster setting | Exported as | Resolved by | Derives |
|---|---|---|---|---|
| Scripts | `connection.remote_base_path` | `RACK_REMOTE_BASE_PATH` | `backend/script_tree.py` (layers); scripts self-locate | `scripts/{project-<id>,custom,<platform>,multi-platform}/` |
| Logs | `connection.log_dir` | `NVIS_LOG_DIR`, launch cwd, `RACK_JOB_LOG_DIR` | each launcher: `${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-$PWD}}` | job `--output`, collectors |
| Workspace | `connection.workspace_dir` | `NVIS_WORKSPACE_DIR` | `bin/scripts/workspace-dir-common.sh` (the root); `bin/scripts/lib/llmb_paths.inc.sh` (LLMB) | `LLMB_INSTALL` (`<ws>/llmb`; an explicit value is complete on its own), `LLMB_VENV` (`<ws>/llmb_venv`), HPC-X, CUDA, host NCCL + nccl-tests, nvloom, built images |
| Shared images | `connection.shared_container_dir` | `RACK_VALIDATION_CONTAINER_ROOT` | `validation_presets.shared_container_root()`; scripts read the env | origin store (`/cluster/containers` on NC) |
| Node image cache | `connection.node_container_dir` | `NVIS_NODE_CONTAINER_DIR` (+ `NVIS_NODE_IMAGES_P2P=1` when P2P is on) | `bin/scripts/lib/node_images.inc.sh` | where jobs run images from (`/opt/containers` on NC) |
| Published settings | — (follows the scripts root) | — | `bin/scripts/lib/node_config.inc.sh` | `<scripts dir>/config/` — the image catalog today |

Three of these are the roots agreed on 2026-10-08 — **scripts, logs, workspace** — with the plan
to consolidate them under one cluster home (`/home/cmods` on NC); see
[the cluster home directory](the-cluster-home-directory.md). The image directories are the same
idea for container data, and the P2P instance derives its own `container_path` and `peer_root`
from them (`P2pSettings.aligned_to`) so P2P and the launchers can never disagree.

`RACK_STATUS_ALLOWED_PREFIXES` and `RACK_BUG_REPORT_REMOTE_DIR` are still deployment-wide env
settings; when the cluster home lands they should derive from it like everything above.

## How a value travels

```
Projects → cluster record (catalog store)
   │  catalog.context.high_overlay()          one function maps fields → env names
   ▼
config resolution (_eget): cluster overlay > Settings job-env > process env > platform defaults
   │  over SSH: NVIS_* / RACK_* forward automatically (transport.factory.FORWARD_ENV_PREFIXES);
   │  anything else must be in the launch's env_extra
   ▼
launcher on the execution host (e.g. the BCM head)
   │  sources the resolver lib → concrete paths, or a loud error naming the setting
   │  into Slurm: --export=ALL (or an explicit --export list)
   ▼
batch script on a compute node
      runs from Slurm's spool, so it finds lib/ via NVIS_SCRIPTS_DIR, never via BASH_SOURCE
```

Four properties keep this honest:

1. **One mapping from field to env.** `high_overlay()` is the only place a cluster field becomes an
   environment variable. Precedence is defined there, so the cluster record beats a stale
   Settings value or a variable inherited from someone's shell.
2. **One resolver per root, in shell.** `workspace-dir-common.sh` (the workspace),
   `lib/llmb_paths.inc.sh` (LLMB under it) and `lib/node_images.inc.sh` are where `<ws>/llmb` or
   `<cache>/<image>` is spelled — once. A launcher sources the resolver;
   it does not repeat the derivation. The installers source the same resolver, so the place a
   tool is installed and the place a launcher looks for it cannot diverge.
3. **No ghost defaults.** An unset root is an error that names the setting ("set it under
   Projects → cluster → Workspace"), raised before anything is submitted. A fallback to some
   other site's directory is worse than an error: it fails later, far from the cause, with a
   message about the wrong thing.
4. **Overrides stay overrides.** `LLMB_INSTALL`, `NVIS_NODE_CONTAINER_DIR`, an installer's
   explicit argument and the like still win when set — they describe the rare cluster whose layout
   departs from its root, not a second source of truth.

## Which host can see what

A path is only meaningful relative to the host that resolves it. On Nscale NC:

| Host | Sees |
|---|---|
| App server (`appserver-nc`, the container host) | `/home` (NFS), `/cluster` (its own md0, the P2P origin) |
| BCM head (where launches and `sbatch` run) | `/home` only — no `/cluster`, no `/opt/containers` |
| Compute nodes | `/home`, node-local `/opt/containers` (P2P cache) |

So: anything a launcher must *read on the head* lives under a shared mount (`/home`). Images are
checked *on the nodes*, by `pdsh`, against the catalog Rack Status publishes beside the scripts
(`<scripts>/config/image-catalog.tsv`) — never by stat-ing the shared store from the head. That
catalog is script execution's own copy; the P2P control plane keeps its own manifest and receipts
to know whether *it* is in sync, and the two checks stay separate on purpose.

## Fixing the next path or prefix bug

Work through this in order; stop at the first step that applies.

1. **Name the root.** Which of scripts / logs / workspace / shared images / node cache does the
   path belong to? Almost every path is "a root plus a well-known suffix".
2. **If the root exists, derive.** Add the suffix to that root's resolver
   (`workspace-dir-common.sh`, `lib/llmb_paths.inc.sh`, `lib/node_images.inc.sh`, `lib/node_config.inc.sh`) and source the
   resolver from the script. Do not write `${X:-/some/site/path}` in the script.
3. **If no root fits, add one properly** — a new path is a new cluster setting, not a new
   default:
   - field on `ClusterConnection` (`backend/catalog/models.py`), validated with `valid_data_dir`;
   - export in `catalog.context.high_overlay()` under an `NVIS_`/`RACK_` name (so it forwards
     over SSH);
   - a registry entry or `JOB_ENV_UI_DEFINITIONS` row as the deployment-wide fallback, plus a
     hint in `docs/settings-hints.toml` (regenerate `docs/reference/configuration/settings.md`);
   - an editable field in the cluster editor (`ProjectsView.tsx`) and `types/catalog.ts` — a
     setting nobody can see or edit is not a setting;
   - the derivation in a resolver lib, and the table on this page.
4. **Fail loudly when unset**, naming where to set it. Never fall back to a literal path.
5. **Check every hop**: the overlay export, SSH forwarding (prefix or `env_extra`), the Slurm
   `--export`, and `NVIS_SCRIPTS_DIR` inside batch scripts.
6. **Check which host resolves it** (table above). A head-side `stat` of a node-local or
   app-server-only path is a bug, not a missing file.
7. **Test it the way it runs.** `backend/tests/test_cluster_workspace.py` fails the build if any
   script's code carries a `/home/<user>/` or `/home/*/` literal and pins the resolver contract;
   `test_node_config.py` deploys a real instance into a local tree and follows published settings
   to where each project runs; `test_cluster_container_dirs.py` pins the image roots. Extend the
   one that matches. Run both suites in the CI images (`bin/ci-local.sh`), not on macOS.

### Anti-patterns, by their tells

- **A `/home/<user>/` literal in a script** — another site's path; derive it from a root.
- **A glob like `/home/*/workspace`** — a wildcard standing in for a setting nobody made.
- **`${VAR:-/somewhere}` repeated across scripts** — N copies of one default that will drift; it
  belongs in the root's resolver, once.
- **A backend default that "usually works"** (`/home/cmsupport/workspace`) — a ghost: it turns a
  configuration gap into a wrong-directory failure.
- **Stat-ing a shared store from the launch host** — the launch host may not mount it.
- **Writing into a shipped script layer** — only a deploy writes `multi-platform/`; published
  settings go to `config/` (shipped with only a README, which both deploy paths preserve), and
  publishing never creates a script layer.
- **Fixing it on the host** (editing a deployed script or exporting a variable in someone's
  shell) — the next deploy or the next operator loses it. Fix the setting.

## Related

- [The cluster home directory](the-cluster-home-directory.md) — consolidating the roots under one
  home; the roadmap item.
- [How a script path is chosen](how-a-script-path-is-chosen.md) — which copy of a script runs.
- [Projects and clusters](../reference/catalog/projects-and-clusters.md) — the connection fields.
- `.cursor/rules/cluster-paths.mdc` — the condensed version of this page for coding agents.
