---
diataxis: explanation
description: Why a cluster's on-host paths should derive from one root, the sprawl that happens when they don't, and the single-source-of-truth the app should expose.
---

# The cluster home directory

Rack Status runs commands, reads logs and stages build artefacts on a cluster's own hosts, so it
has to know *where* on those hosts things live. Today it learns each of those locations from a
**separate** setting, with its own default, and nothing ties them together or shows them in one
place. On a deployment where the defaults happen to be wrong, the result is a cluster whose files
are scattered across three different `/home` users — and a tool that silently looks in a fourth.

This page is the argument for a single **cluster home**: one configured root from which every other
on-host path derives, surfaced by the app as the one place an operator looks to answer "where does
this cluster keep its things?"

## The sprawl, concretely

These are the on-host paths the app resolves, each from an independent knob:

| What | Setting | Default | On NscaleTX-NC today |
|---|---|---|---|
| App scripts (launchers, sbatch) | `RACK_REMOTE_BASE_PATH` (+ the layered `scripts` role; see [how-a-script-path-is-chosen](how-a-script-path-is-chosen.md)) | `/home/cmsupport/nvis-rack-status` | `/home/rkoike/nvis-rack-status` |
| Job logs | cluster `connection.log_dir` → `NVIS_LOG_DIR` (legacy `RACK_STATUS_LOG_DIR`) | `$PWD` of the launch | `/home/cmods/nvis/logs` |
| Readable roots | `RACK_STATUS_ALLOWED_PREFIXES` | — | `/home/cmods/nvis/logs,/home/cmods/bringup-data` |
| Bug-report drop | `RACK_BUG_REPORT_REMOTE_DIR` | — | `/home/cmods/bringup-data/reports` |
| Build workspace (HPC-X, CUDA, host NCCL + nccl-tests) | `NVIS_WORKSPACE_DIR` / `RACK_WORKSPACE_DIR` | `/home/cmsupport/workspace` | **unset → `/home/cmsupport/workspace`** |
| Single-node bring-up scripts (external, owned by the bring-up team) | — (consumed, not owned) | — | `/home/cmods/bringup` |

Three different users — `rkoike`, `cmods`, `cmsupport` — and the one that matters most for the
host-NCCL validation, the workspace, is the one nobody ever set. It falls through to a hard-coded
`/home/cmsupport/workspace` that does not exist on this cluster. An operator installing the "CUDA
toolkit & NCCL" tool and then running the NCCL sweep has no way to see that the two are pointed at
different places until the sweep fails with `libnccl.so.2 not found`.

## Why this is the disease, not a typo

The scattered defaults are not individually wrong so much as *un-anchored*. Two symptoms give it
away:

- **The sweep wildcards the user.** `nccl_allreduce.sbatch` resolves its library with a glob —
  `/home/`**`*`**`/workspace/nccl-<ver>/...` — precisely because no setting ever told it which home
  the workspace lives in. A wildcard that stands in for a missing configuration is a path that was
  never made a first-class citizen. It happens to match any `/home/<user>/workspace`, which hides
  the problem right up until two tools disagree on the version, and then the error names a literal
  `*` and the reader cannot tell configuration from a bug.
- **A ghost default silently breaks a tool.** `NVIS_WORKSPACE_DIR` unset is not an error; it is a
  confident pointer at an empty directory. The install tool writes real artefacts to the *configured*
  workspace and the sweep reads from the *wildcard* one, and nothing reconciles the two.

Both are the same root cause: a cluster's on-host geography is spread across N independent knobs
with N independent defaults, and the app treats each as if it were unrelated to the others.

## The design: one root, derived paths, shown in the UI

A cluster should declare **one** home — call it the cluster home, `/home/cmods` on NscaleTX-NC —
and every other on-host path should **derive from it by default**:

```
<cluster_home>/                     e.g. /home/cmods
├── nvis-rack-status/               app scripts         (RACK_REMOTE_BASE_PATH)
├── nvis/logs/                      job logs            (connection.log_dir)
├── workspace/                      build toolchain     (NVIS_WORKSPACE_DIR)
│   ├── hpcx-*/                       HPC-X (OpenMPI for nccl-tests)
│   ├── cuda → cuda13/                CUDA toolkit
│   ├── nccl-<ver>/nccl/build/lib/    host libnccl.so.2
│   └── nccl-<ver>/nccl-tests/build/  all_reduce_perf
├── bringup/                        single-node scripts (external; consumed)
└── bringup-data/reports/           bug-report drop     (RACK_BUG_REPORT_REMOTE_DIR)
```

Three properties make this a consolidation rather than a rename:

1. **One source of truth.** The home is set once per cluster (beside `log_dir` in the cluster
   record). Each derived path keeps its own setting as an *override* for the rare cluster that needs
   one, but the default is the home plus a well-known suffix — so a correctly-shaped cluster needs
   exactly one value, not six.
2. **No ghost defaults.** A path that derives from a configured home cannot fall through to a
   stranger's directory. If the home is `/home/cmods`, the workspace is `/home/cmods/workspace`, and
   the sweep resolves *that*, not a wildcard.
3. **Findable.** The app surfaces the resolved set — home and every derived path, with whether each
   exists and is writable — on one screen, so "where does this cluster keep its things?" has a single
   answer an operator can read, instead of being reconstructed from six settings and a glob.

The sweep's `/home/*/workspace/...` wildcard then becomes a resolution of the configured workspace,
which is both more honest and the thing that lets an error say *which* directory it looked in.

## What this is not

It is not a demand that every cluster use `/home/cmods`, nor that the three existing users collapse
into one overnight. It is that the app should *model* a cluster home and derive from it, so a new
deployment is one decision instead of six, and an existing one — like NscaleTX-NC, which already
keeps logs and bring-up data under `/home/cmods` — can be pulled onto a single root deliberately
rather than discovering the sprawl one failed launch at a time. See
[how-a-script-path-is-chosen](how-a-script-path-is-chosen.md) for the script layer that already does
the derived-from-a-root thing well, and that this generalises.
