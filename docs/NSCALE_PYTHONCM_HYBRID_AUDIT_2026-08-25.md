# Nscale PythonCM-primary / Slurm-fallback audit — 2026-08-25

## Audit conclusion

The earlier acceptance rule was wrong: it treated “no PythonCM equivalent” as a
reason to disable a feature. The corrected build uses PythonCM for equivalent
inventory, state, lifecycle, and ProgramRunner operations and restores Slurm or
transport fallback only for the feature-specific gaps documented in
[`../README_NSCALE_PYTHONCM_HYBRID.md`](../README_NSCALE_PYTHONCM_HYBRID.md).

The original `PYTHONCM_ZERO_SLURM_AUDIT_2026-08-25.md` is superseded.

## Independent audit inputs

Three read-only agents independently traced the shared tree.

### Backend/runtime audit

Coverage included status refresh, inventory, scheduler/runtime dispatch, power,
drain/resume, ProgramRunner execution, workflows, terminal, reports, fabric,
timeouts, cancellation, and durable state. Findings incorporated here:

- keep `/api/status` and its background refresh free of `cmsh` and Slurm;
- reject a non-PythonCM primary provider for the Nscale deployment;
- preserve provider provenance so UUID and numeric IDs cannot be confused;
- use targeted `scontrol`/`scancel` only for numeric fallback jobs;
- mark failed PythonCM inventory stale/unknown rather than replaying healthy
  cached state;
- keep terminal and external scripts explicit transport fallbacks;
- retain timeout cancellation and immediate UUID persistence for PythonCM work.

### Frontend/Nscale audit

Coverage included every navigation route, action card, validation control,
multirack launch, 30-second timer, Scheduler endpoint, help entry, and demo-AI
response. Findings incorporated here:

- the 30-second Racks timer remains and is behavior-tested;
- frontend scheduler calls use provider-neutral `/api/scheduler-*` routes;
- Tools, Workflows, Terminal, Post Run Report, Projects, Platform Catalog, Tool
  Registry, and all rack actions are restored;
- validation buttons and preset preparation are restored;
- generated help again covers every navigable view;
- active Nscale help contains the PythonCM-primary/fallback rule rather than a
  blanket “blocked” claim.

### Validation/script audit

Coverage followed every validation route through its shell script, MPI/LLMB
child launcher, and status store. Findings incorporated here:

- NCCL recipes use coordinated MPI/SSH launcher semantics;
- NeMo, Llama, GPT-OSS, and Exemplar use Slurm/Pyxis or `llmb-run submit`;
- ClusterKit has an external multi-host launcher;
- NVLoom combines sysinfo and MPI-over-SSH;
- the direct `dcgmi mndiag` proposal is not MNUberGEMM equivalence;
- therefore these are documented Slurm fallbacks, not fake PythonCM conversions;
- provider-specific UUID/numeric ID tracking must survive restart and support
  status/output/cancel.

## Source evidence matrix

| Invariant | Implementation | Acceptance test |
|---|---|---|
| Nscale primary provider is PythonCM | `backend/scheduler_runtime.py` | `test_nscale_primary_scheduler_typo_fails_closed` |
| Explicit Slurm construction remains possible | `backend/schedulers/factory.py` | `test_nscale_primary_is_pythoncm_but_explicit_slurm_fallback_can_be_built` |
| Racks cold/warm state does not call Slurm or cmsh | `backend/native_status.py` | `test_cold_and_warm_racks_sources_never_poll_slurm_or_cmsh` |
| Background status does not start cmsh warmer | `backend/app.py` | `test_status_bootstrap_does_not_start_cmsh_warmer` |
| Fallback jobs remain visible without polling Slurm | `backend/scheduler_runtime.py` | `test_active_overlay_includes_fallback_registry_without_polling_slurm` |
| UUID/numeric IDs retain provider provenance | `backend/scheduler_runtime.py` | `test_registry_keeps_provider_provenance` |
| Numeric status/cancel is targeted Slurm | `backend/scheduler_runtime.py` | `test_numeric_status_and_cancel_use_targeted_slurm_calls` |
| UUID cancel is PythonCM | `backend/scheduler_runtime.py` | `test_uuid_cancel_uses_pythoncm_not_slurm` |
| rf0, power, drain/resume provider is PythonCM | `backend/bcm_query.py`, `backend/app.py` | `test_rf0_power_drain_resume_use_pythoncm` plus scheduler API tests |
| GPU/service/hardware/bug-report/NVLink commands use ProgramRunner | `backend/app.py` | `test_gpu_services_bugreport_hw_and_nvlink_use_programrunner` |
| Compatibility URLs do not change provider | `backend/slurm_query.py`, `backend/api/slurm_queries.py` | `test_neutral_and_legacy_scheduler_urls_are_compatible` |
| Workflow and validation feature surfaces are enabled | API blueprints and `backend/app.py` | `test_workflows_and_validation_presets_are_not_blanket_disabled` |
| Every validation gap has a reason | `backend/pythoncm_validation.py` | `test_validation_gap_has_documented_slurm_fallback_reason` |
| PythonCM UUID/terminal state is durable | `backend/app.py`, helper manifests | `test_pythoncm_worker_persists_uuid_and_terminal_state` |
| Log-only rack probe executes no cluster command | `bin/scripts/rack_status.sh` | `test_log_probe_executes_no_cluster_command` |
| Browser uses a 30,000 ms interval | `frontend/src/hooks/useRackStatus.ts` | frontend `useRackStatus` timer test |
| Every navigable route has help | frontend content registry | `content.test.ts` |
| Restored feature controls remain enabled | rack/Multirack component tests | `HardwareTab`, `ValidationTab`, `MultirackView` tests |

## Not represented as PythonCM

The audit explicitly refuses to relabel these as PythonCM:

- a shell script merely launched from a BCM head;
- a ProgramRunner command that invokes `sbatch`, `srun`, `pdsh`, or MPI-over-SSH;
- independent fan-out commands substituted for one coordinated MPI allocation;
- `dcgmi mndiag` substituted for the pinned MNUberGEMM container workload;
- SSH/PTTY substituted for ProgramRunner;
- local catalog/filesystem work described as a scheduler operation.

Those paths are preserved under their real provider and documented in the
handoff README.

## Remaining live gates

Static/unit/integration tests do not authorize cluster mutation. ProgramRunner,
cancel, drain/resume, power, and real validation submission need the Nscale BCM
credential files and an approved reserved node. A final live acceptance must
also verify shared result-directory visibility and the submit/manifest crash
window described in the handoff README.

## Automated evidence

The final source state passed 1,440 backend tests with 1 skipped, all 379
frontend tests, the production TypeScript/Vite build, production-module Ruff,
Python byte compilation, shell/`.sbatch` syntax checks, and Git whitespace
checks. Frontend lint completed with zero errors and 57 existing warnings.
