# Nscale Rack Status Git-ready check — 2026-08-25

## Release verdict

The `feature/rack-status-pythoncm` branch is ready to archive and hand off after
the release commit. This check covers the Nscale TX target only. PythonCM is the
primary inventory, state, management-action, and remote-execution provider;
Slurm and transport paths remain enabled only for requested features whose
semantics cannot be reproduced by PythonCM.

The complete provider mapping, PythonCM examples, fallback reasons, deployment
settings, simulated acceptance matrix, and remaining live gates are in
`README_NSCALE_PYTHONCM_HYBRID.md`. Independent audit evidence is in
`docs/NSCALE_PYTHONCM_HYBRID_AUDIT_2026-08-25.md`.

## Source and configuration checks

| Check | Result |
|---|---|
| Branch | `feature/rack-status-pythoncm` |
| Nscale identity | `RACK_CLUSTER_ID=nscale-tx`, customer `Nscale`, GB300, PythonCM |
| Persistent catalog | isolated as `/app/var/store/catalog-nscale-tx.json` |
| Racks browser refresh | 30,000 ms; `/api/status` is PythonCM/local-registry only |
| Nscale floor topology | live PythonCM NodeGroups: DH01 = SU12–SU14; DH02 = SU1–SU11; 219 logical racks, 0 unmatched |
| Tracked web/Grafana secrets | none; deployment variables fail closed when absent |
| Production Python Ruff | passed |
| Added/changed backend-test Ruff | passed |
| Python byte compilation | passed |
| Shell and `.sbatch` syntax | passed |
| Git whitespace check | passed |

Historical parsers and fixtures retained by the shared upstream tree do not
participate in Nscale deployment selection. Active Nscale configuration,
operator examples, rack pools, and launch defaults use only the Nscale target;
the NCCL campaign requires an explicit approved `ink...` rack list.

## Executed acceptance gates

| Gate | Exact result |
|---|---|
| Complete backend regression | 1,458 passed, 1 skipped in 91.71 s |
| Complete frontend unit/component suite | 54 files, 381 tests passed |
| TypeScript and Vite production build | passed |
| Generated help registry | 78 entries |
| Frontend ESLint | 0 errors, 57 existing warnings |

`backend/tests/test_nscale_simulated_acceptance.py` exercises the public API
lifecycles used by Rack Status: Racks and Scheduler reads; NVLoom launch/status;
durable provider and node tracking; multi-child status/cancel; post-run marking
and PythonCM drain; analytics start/poll/result; Grafana metrics, logs, events,
Ethernet, IB/NVLink, and storage contracts; VictoriaMetrics health/catalog;
exact-node PythonCM fabric execution; workflow submit/cancel races; terminal
authorization; and the 30-second Racks source contract.

## Provider and feature readiness

- PythonCM: rack/PhysicalNode inventory, device and drain status, power,
  drain/resume, exact-node ProgramRunner operations, fabric/Ethernet probing,
  UUID status/output/cancel, and current post-run node state.
- Local/read-only services: durable activity overlays, reports, parsers,
  analytics, metadata, VictoriaMetrics, and Grafana embedding.
- Targeted Slurm fallback: validation workloads that require allocations,
  Pyxis, MPI/SSH coordination, or `llmb-run`; only numeric child status/cancel
  performs targeted Slurm calls.
- Explicit transport/vendor fallback: interactive PTY, artifact retrieval,
  BMC-controller reset, interface mutation, and vendor switch/IMEX recovery
  where PythonCM has no equivalent API.

All operator-visible features remain enabled. A fallback is recorded with its
provider, exact nodes, native reference, and human-readable reason; it is not
used by the recurring Racks refresh.

## Packaging and reproducibility

The release archive is created with `git archive` from the final commit, and the
Git bundle is created from the same branch. The package manifest records the
full commit hash. The tar archive and bundle are extracted/cloned locally before
upload, and every uploaded artifact is downloaded from NVFTP and checked against
the shipped SHA-256 file.

After the release commit, `git status --porcelain` must be empty. The exact
commit and artifact filenames are therefore recorded in the external package
manifest rather than embedded into this pre-commit document.

## Honest remaining live gates

No simulated test is represented as a production mutation or workload result.
Live completion still requires Nscale BCM credentials, an administrator-approved
reserved node, the deployed Grafana embed map, and reachable Nscale Grafana and
VictoriaMetrics endpoints. Those inputs are external to this repository.

On the approved node, the remaining checks are live ProgramRunner execution and
restart persistence, remote cancellation, drain/resume, separately authorized
power, real retained-Slurm validation launch/tail/cancel, real observability
panel/data availability, and confirmation that repeated `/api/status` requests
start no Slurm or `cmsh` process.
