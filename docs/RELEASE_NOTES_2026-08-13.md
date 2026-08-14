# NVIS Rack Status release candidate — 2026-08-13

This release candidate consolidates the rack-validation, post-run reliability,
telemetry, report-persistence, and deployment-hardening work completed through
2026-08-13. It is prepared for review and import into the internal GitLab project;
it has not been pushed or merged by the preparation process.

The detailed operating procedures are in [RACKSTATUS_USER_GUIDE.md](RACKSTATUS_USER_GUIDE.md).
The engineering/deployment reference is the repository [README](../README.md), and
the exact qualification evidence is in [RELEASE_QA_2026-08-13.md](RELEASE_QA_2026-08-13.md).

## Operator-visible additions

- Native node-level and rack-level status with completed-log scans committed before
  the canonical snapshot is published.
- Exact-node, multi-node-per-rack, and multi-rack validation targeting.
- Version-gated CUDA 13.3+ / NCCL 2.31.2 validation.
- Invariant Llama 3.1 405B FP8 validation: 256 GB300 nodes, 1,024 GPUs, 50 steps,
  approved topology, image, NCCL, and NVLS settings.
- GPT-OSS CPU-only microbenchmark validation and NVIDIA DGXC Exemplar
  `system_info` v26.06.01 validation.
- Shared-NFS and `/tmp/containers` execution with allocation-scoped, checksum-
  verified rsync staging to prevent cold container loads.
- Repeatable Hardware Deep Audit and NVLink / Network Deep Audit workflows on an
  exact node selection, a rack selection, or multiple racks.
- Immediate Results access beside audit launches, durable full output, exact Slurm
  job IDs, and GPU-aware Slurm allocations.
- Native Post Run Node Report: run participation, attributed failures, repeat
  trends, three-run flags, current Slurm reasons, issue history, validation launch,
  operator annotation, and optional exact-node drain.
- Generated report archive with durable payload/metadata storage and download/delete
  controls under Reports.
- Native metrics rack monikers (`d01`, `e01`, `f01`, `g01`, `h01`) while preserving
  raw VictoriaMetrics labels for queries.

## Correctness and safety fixes

- Slurm optional arguments use `--kill-on-bad-exit=0` and `--immediate=60`; `srun`
  no longer interprets a standalone `0` as the executable.
- Every application Slurm call resolves through the platform/scheduler provider and
  its configured absolute binary.
- Deep audits request four GPUs per node, preventing GPU-cgroup false failures.
- Multi-rack audit workflows resolve each rack and deduplicate the final exact node
  list; a one-rack explicit node subset stays exact.
- Automatic draining is evidence-scoped. A launcher, transport, or multi-node
  collective failure does not mark every allocated node faulty.
- Exact-node Slurm prolog/unavailable failures retain their node attribution and do
  not become a rack-wide drain.
- Post-run polling follows the launched workflow to a terminal state and continues
  until that exact run appears in the report; the open node dialog is refreshed
  instead of retaining its pre-launch snapshot.
- Workspace toolchain/container status now probes the execution host through the
  transport filesystem boundary. Remote `PurePosixPath` values and shared-NFS
  directory symlinks no longer produce HTTP 500/400 failures.
- Durable routes, generated reports, workflow runs, node issues, validation
  activities, and post-run history are mounted under persistent application state.
- Deployment verification checks container state, imports, missing/stale modules,
  required routes, image/mounted SPA parity, every referenced asset, application
  response, and recent boot/import errors.

## Deployment notes

1. Run `bin/prod-drift.sh` against both the deployment tree and the head-node script
   tree. Review every changed or remote-only file.
2. Build the SPA and image from the same commit.
3. Recreate the container after the build. Vite replaces the `assets/` directory;
   an existing bind mount can remain attached to the old directory inode until the
   container is recreated.
4. Deploy `bin/scripts/` with `bin/deploy-scripts.sh`. The command is checksum-based
   and does not delete head-only evidence by default.
5. Run `bin/verify-deploy.sh` and the read-only API/browser smoke tests from the
   qualification document.

The Nscale TX acceptance cycle preserved the five head-only backup/evidence files.
Three older live launcher copies were archived and then replaced by their reviewed,
committed counterparts; no head-only file was deleted.

## Live acceptance evidence

The final exact-node Hardware Deep Audit was launched from the production SPA on
`ink50-dh01-d01-32`:

- workflow run `6c66e4ee-92ee-4106-9b72-d1162baa53c7`;
- Slurm job `147895`;
- `COMPLETED`, exit `0:0`;
- allocation: one node and four GB300 GPUs;
- four GPUs detected, temperatures 25/27/26/25 °C;
- no Xid or fatal PCIe event;
- structured audit result `pass`, no warnings, no drain;
- final Slurm node state `idle`, reason `none`.

The prior SU7 qualification also completed one Hardware Deep Audit and one forced
NVLink / Network Deep Audit with four GPUs allocated per job, healthy GPUs/NVLinks/
IB, no drain, immediately visible results, and complete Post Run refresh.

## Qualification summary

- Full backend suite: 1,202 passed; five optional-dependency tests skipped.
- Full frontend suite: 309 passed.
- TypeScript: passed.
- ESLint: zero errors; existing advisory warnings remain non-gating.
- Ruff: passed.
- Shell syntax, Compose resolution, Markdown link/whitespace checks: passed.
- Deployment verifier: all checks passed.
- Production Chromium route sweep: all documented routes passed after the
  execution-host workspace probe fix.
- Internal secret/vault tracking gate: passed.

The telemetry ownership cutover is also complete. On-node Alloy is now the sole
owner for SU1–SU5; all four fresh metric families agree at SU1=282, SU2=254,
SU3=222, SU4=288, and SU5=166 nodes. Fresh duplicate DCGM `(node,gpu)` and node
exporter `(node,su)` queries return no results. The exact four external source
files, SHA-256 values, acceptance evidence, and rollback steps are included in the
separate `nscaletx-telemetry-ownership-20260813.tar.gz` handoff package.

See [RELEASE_QA_2026-08-13.md](RELEASE_QA_2026-08-13.md) for the feature matrix,
commands, known limitations, and exact evidence.

## Intentionally parked

- PythonCM is not an active cluster-manager execution provider in this candidate.
- Clush is not an active fan-out transport in this candidate.

BCM/MAAS providers, the connection/transport layer, Slurm fan-out, `pdsh`, and SSH
composition remain the supported paths. The documentation does not present the
parked work as shipped.

## Packaging and GitLab handoff

The handoff includes two deterministic artifacts built from the final committed
tree:

- a source `.tar.gz` created with `git archive`, excluding `.git`, untracked
  credentials, runtime caches, WAL, report payloads, and workflow execution state;
- a Git bundle containing the complete branch history for offline import/recovery.

Verify the adjacent SHA-256 files before unpacking. After connectivity to the
internal GitLab service is available, push the reviewed branch and tag, let the
GitLab pipeline rebuild from source, and use the pipeline-produced air-gapped image
bundle for an offline deployment rather than treating the source archive as a
Docker image.
