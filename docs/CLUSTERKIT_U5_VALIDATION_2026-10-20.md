# ClusterKit U5 Validation - 2026-10-20

The configuration-driven ClusterKit execution path was validated on the NC
Spectrum-X/RoCE fabric in four separately approved stages. All jobs ran as
`cmods` through the reviewed static Slurm entrypoints. No GPU resources,
exclusive allocations, retries, recovery actions, node-state changes, or
system configuration changes were requested.

| Scope | Job | Nodes | Expected pair coverage | Result |
| --- | ---: | ---: | ---: | --- |
| Preflight | 217510 | 1 | Not applicable | Functional PASS |
| Pairwise | 217516 | 2 | 1 pair per metric/stage | Functional PASS |
| Pairwise | 217518 | 4 | 6 pairs per metric/stage | Functional PASS |
| Pairwise | 217519 | 8 | 28 pairs per metric/stage | Functional PASS |

The pairwise runs used nodes `sys1-gso1-p-phy-gpu55-su20` through
`sys1-gso1-p-phy-gpu62-su20` as required by each scope. Every allocation
matched its reviewed plan exactly. Each node used one MPI rank, one assigned
CPU, `roce_vf_r1:1`, and a matching per-node global RoCE-v2 GID. Slurm PMIx v3
launched the ranks without SSH fanout. Preflight, baseline, optional single-path
comparison, post-run diagnostics, result normalization, and exact unordered-pair
coverage validation completed successfully.

Observed raw ranges were:

| Scope | Stage | Bandwidth | Latency |
| --- | --- | ---: | ---: |
| 2 nodes | Baseline | 56390.212 MB/s | 9.014 usec |
| 2 nodes | Single path | 47517.832 MB/s | 3.102 usec |
| 4 nodes | Baseline | 56447.890-56548.347 MB/s | 2.982-6.045 usec |
| 4 nodes | Single path | 47451.172-47522.139 MB/s | 2.983-3.296 usec |
| 8 nodes | Baseline | 56446.370-56543.773 MB/s | 2.964-5.874 usec |
| 8 nodes | Single path | 47432.927-47504.916 MB/s | 2.965-3.107 usec |

These results establish functional execution and complete observed pair
coverage only. Performance status remains **UNVERIFIED**. The selected VF reports
200 Gb/s while ClusterKit reports approximately 47.5-56.5 GB/s, so the physical
versus virtual capacity and ClusterKit measurement/accounting semantics require
fabric-team confirmation before thresholds or performance acceptance are
enabled. Vendor `PPN=144` and `Technology=Unknown` values are preserved as raw
metadata; rank-placement evidence records the actual one-rank-per-node launch.

The transferred evidence bundle containing immutable plans, source manifests,
raw vendor output, Slurm logs, UCX logs, node observations, placements,
normalized summaries, and reconciliation reports is intentionally retained
outside Git. This repository records the reviewed implementation and this
compact validation summary rather than committing environment-specific result
artifacts.
