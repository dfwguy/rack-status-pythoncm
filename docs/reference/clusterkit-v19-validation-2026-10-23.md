---
diataxis: reference
title: ClusterKit V19 Validation, 2026-10-23
---

# ClusterKit V19 Validation, 2026-10-23

## Provenance

The AppServer handoff `greenlight-clusterkit-v19-live-20261023-handoff.tar.gz`
has SHA-256
`b3c2e79a6604c716bec17b47056c0065bd6e8a26cc5f245594990fe95c914950`.
All 411 entries in its internal SHA-256 manifest verified on the Mac.
Its baseline is GreenLight commit `4532e26492f631e89911bc29660eb114efa06c02`
on `feature/clusterkit-ui`, plus the six-file AppServer patch.

The report and raw artifacts describe real TLS/SRP/API-to-Slurm validation of
the matched GreenLight backend on NC, running separately as `cmods`. The report
does not claim AppServer browser automation or validation from the production
Docker container. The production application was not changed by this work.

## Execution and Results

| Stage | Job | Functional result | Observed/expected pairs per metric and mode |
|---|---|---|---|
| One-node preflight | 220038 | PASS | Not applicable |
| Two explicit nodes | 220039 | PASS | 1/1 |
| Four nodes from configured V19 group | 220040 | PASS | 6/6 |
| Eight explicit nodes | 220041 | PASS | 28/28 |

Bandwidth and latency coverage were complete in both baseline and single-path
modes. Performance status is **UNVERIFIED** for every run. The recipe is CPU/RoCE,
one MPI rank and one CPU per node, nonexclusive and without GPU allocation.
Slurm received a 1 GiB/node request, but accounting omitted memory; enforcement
of that memory request was not demonstrated.

The selected nodes were the first 1/2/4/8 hosts in
`sys1-gso1-p-phy-gpu[1-8]-su23`. All 18 user-declared V19 nodes were idle at the
initial/final observation; only the selected nodes were tested. Group membership
was user supplied, not independently verified physical topology.

Each rank reported aarch64, ACTIVE/LinkUp Ethernet on `roce_vf_r1:1` and CPU
affinity `[0]`. Independent global RoCE-v2 GID discovery selected index 3 on
GPU1 and index 5 on GPU2-GPU8. Several successful UCX inventory queries exceeded
five seconds and stayed within the existing 15-second bound.

Duplicate-confirmation protection, result/download endpoints, context-scoped
history pagination, authorization refusal, backend restart recovery and recovery
into an empty activity registry passed. Recovery submitted no replacement jobs.
The handoff records 88 selected artifact downloads compared byte-for-byte with
disk. All six jobs, including failures below, had left the queue afterward.

## Preserved Failures and Fixes

| Attempt | Observation | Resolution |
|---|---|---|
| 220036 | Compute bootstrap imported host provider/storage dependencies and required `tinydb` | Lazy public scheduler exports; defer platform resolution until scheduler construction; hash/snapshot scheduler package initializer; add cold-compute regressions |
| 220037 | Compute summary reported missing `scontrol` | Load the existing Slurm module in the isolated service's parent environment and check required clients; no product/system change |

The three production source files are `backend/schedulers/__init__.py`,
`backend/schedulers/slurm.py` and `backend/clusterkit_execution.py`.
`backend/tests/test_clusterkit_compute_dependencies.py` adds three regressions.
Two existing test fixtures now supply a temporary status log directory and pin
their fake Slurm client path. Frontend source, API contracts and static launch
scripts are unchanged.

A harness-only comparison initially used the preview's plan hash rather than
the final prepared plan after fresh inventory collection. The harness was
corrected to compare execution identity and the immutable final plan; the
already successful preflight was recovered, not rerun. This was not another
failed ClusterKit job.

## Verification

| Location and scope | Result |
|---|---|
| AppServer final regression set, reported with logs/XML in handoff | 440 tests and 176 subtests passed |
| Mac, three new regressions before the product fix | All three reproduced the failure |
| Mac, ClusterKit/scheduler/authorization targeted set after the fix | 325 tests and 176 subtests passed |
| Mac, routes/imports/configuration/script-tree compatibility set | 743 tests passed |
| Mac, backend Ruff and new regression-test Ruff | Passed |
| Mac, tracked-source import check without built frontend assets | 216 modules imported; 308 routes registered |
| Mac, generated action vocabulary/snapshot, Settings reference and TOML example | Current |
| Mac, documentation structure and warning-as-error HTML build | Passed |

The two Mac test sets overlap and are not a unique combined test count. The
broader backend and frontend measurements from October 22 remain historical
measurements, not a claim that the whole Linux suite was rerun on the Mac.
No frontend rebuild was needed for this backend-only fix; its source and API
contracts remain unchanged from the previously reviewed UI release.

## Outstanding Acceptance Boundaries

The first two-node baseline measured 24,236.126 MB/s and 50.529 usec, while that
job's single-path mode measured 47,493.082 MB/s and 3.133 usec. GPU1's captured
`duplicate_request` hardware counter increased from 0 to 56 during that job.
The captured error-like counters did not increase in the later four/eight-node
runs. These observations do not establish a cause or performance acceptance.

GPU/GPUdirect testing, full-rack/SU scale and production Docker-to-host execution
remain outside this validation. The next stage is the
[reviewed deployment plan](../how-to/clusterkit-deployment-plan.md), not an
automatic production deployment or repeat live test sequence.
