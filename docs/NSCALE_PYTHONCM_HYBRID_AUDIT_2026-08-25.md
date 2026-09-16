# Nscale PythonCM-primary / Slurm-fallback audit — 2026-08-25

## Audit conclusion

The earlier acceptance rule was wrong: it treated “no PythonCM equivalent” as a
reason to disable a feature. The corrected build uses PythonCM for equivalent
inventory, state, lifecycle, and ProgramRunner operations and restores Slurm or
transport fallback only for the feature-specific gaps documented in
[`../README_NSCALE_PYTHONCM_HYBRID.md`](../README_NSCALE_PYTHONCM_HYBRID.md).

The original `PYTHONCM_ZERO_SLURM_AUDIT_2026-08-25.md` is superseded.

## Final remediation sweep

The independent audits were not treated as documentation-only reviews. Their
runtime findings produced these additional changes before packaging:

- isolated the active catalog as `catalog-nscale-tx.json`, preventing a stale
  generic/default catalog in the persistent volume from overriding Nscale,
  GB300, SSH, naming, or PythonCM settings;
- removed the tracked Grafana password and switched the compose override to
  deployment-time secret/environment injection;
- made validation provider, fallback reason, and exact nodes durable fields;
- made every family status endpoint fall back to the durable activity after an
  app restart or in-memory purge;
- made outer activity detail aggregate every native child and outer activity
  cancellation kill the wrapper plus every nonterminal child;
- closed pre-submit cancellation races for validation wrappers, PythonCM jobs,
  MNUberGEMM staging, and workflows;
- made workflow provider/reference state durable and cancellation race-safe;
- centralized terminal activity capture into the results/lake path, including
  failures and stage-only runs without a parsed benchmark artifact;
- used structured PythonCM node reasons in Events/Scheduler rather than parsing
  them as fixed-width Slurm text;
- authenticated standalone terminal tabs and rejected unauthenticated terminal
  WebSockets before PTY allocation;
- hid the Slurm-exporter node chart when PythonCM is primary and retained the
  native BCM inventory chart;
- removed non-Nscale examples/default rack pools from active frontend and
  launcher source. The on-demand NCCL campaign now requires explicit approved
  `ink...` rack ids;
- replaced Rack Status' inferred `Row a`/`Row b` headings with authoritative
  PythonCM `NodeGroup` labels, while retaining hostname-derived logical rack IDs
  instead of BCM's physical `EH...`/`WH...` coordinates;
- widened the active Nscale naming template and direct-node selector from
  `dh01` only to both `dh01` and `dh02`.

## 2026-08-26 live topology acceptance

A read-only BCM 11 PythonCM topology query returned 220 physical Rack entities,
14 NodeGroups, and the exact Nscale membership supplied by the operator. Mapping
NodeGroup members through the active Nscale hostname template produced 219
logical compute racks with zero unmatched racks:

```text
ink50-dh01: SU12=16 SU13=16 SU14=16
ink50-dh02: SU1=16 SU2=16 SU3=16 SU4=11 SU5=16 SU6=16
             SU7=16 SU8=16 SU9=16 SU10=16 SU11=16
```

The 220th physical Rack entity is not a logical compute rack. The application
therefore uses the 219 hostname-derived racks for status and action targeting,
and uses the NodeGroup name only as the operator-facing SU heading.

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
| Nscale DH01/DH02 rows use PythonCM SU labels | `bin/scripts/nvis_bcm_query.py`, `backend/nvis_id_templates.py`, `backend/scheduler_runtime.py`, `frontend/src/components/RackFloorView.tsx` | `test_topology_shapes_racks_and_naturally_sorted_node_groups`, `test_nscale_hierarchy_uses_pythoncm_su_nodegroups_for_both_data_halls`, `test_pythoncm_topology_uses_su_membership_and_one_cached_query`, frontend `rackTopology` SU-heading test |
| Background status does not start cmsh warmer | `backend/app.py` | `test_status_bootstrap_does_not_start_cmsh_warmer` |
| Fallback jobs remain visible without polling Slurm | `backend/scheduler_runtime.py` | `test_active_overlay_includes_fallback_registry_without_polling_slurm` |
| UUID/numeric IDs retain provider provenance | `backend/scheduler_runtime.py` | `test_registry_keeps_provider_provenance` |
| Numeric status/cancel is targeted Slurm | `backend/scheduler_runtime.py` | `test_numeric_status_and_cancel_use_targeted_slurm_calls` |
| UUID cancel is PythonCM | `backend/scheduler_runtime.py` | `test_uuid_cancel_uses_pythoncm_not_slurm` |
| Outer activity detail/cancel aggregates every child | `backend/app.py`, `backend/validation_activities.py` | `test_validation_activity_aggregates_status_and_cancels_every_native_child` |
| Pre-cancelled validation never spawns a wrapper | `backend/app.py` | `test_pre_cancelled_validation_never_spawns_fallback_wrapper` |
| Failed sync validation returns recoverable provider/activity | `backend/app.py` | `test_sync_validation_failure_returns_recoverable_activity_and_provider` |
| rf0, power, drain/resume provider is PythonCM | `backend/bcm_query.py`, `backend/app.py` | `test_rf0_power_drain_resume_use_pythoncm` plus scheduler API tests |
| GPU/service/hardware/bug-report/NVLink commands use ProgramRunner | `backend/app.py` | `test_gpu_services_bugreport_hw_and_nvlink_use_programrunner` |
| Compatibility URLs do not change provider | `backend/slurm_query.py`, `backend/api/slurm_queries.py` | `test_neutral_and_legacy_scheduler_urls_are_compatible` |
| Workflow and validation feature surfaces are enabled | API blueprints and `backend/app.py` | `test_workflows_and_validation_presets_are_not_blanket_disabled` |
| Every validation gap has a reason | `backend/pythoncm_validation.py` | `test_validation_gap_has_documented_slurm_fallback_reason` |
| PythonCM UUID/terminal state is durable | `backend/app.py`, helper manifests | `test_pythoncm_worker_persists_uuid_and_terminal_state` |
| Workflow cancel reaches native child and stays cancelled | `backend/workflow_engine.py` | `test_workflow_cancel_dispatches_native_child_and_cannot_be_overwritten` |
| Workflow submit/cancel race kills child before wait | `backend/workflow_engine.py` | `test_workflow_cancel_while_submit_is_blocked_kills_handle_before_wait` |
| Nscale observability and Ethernet contracts | `backend/grafana_embeds.py`, `backend/metrics_query.py`, `backend/api/fabric_probe.py` | three simulated acceptance tests for all six views, upstream health/chart shaping, and PythonCM Ethernet probe |
| Slurm metrics chart hidden under PythonCM | `backend/metrics_query.py` | simulated native metrics catalog assertion |
| Terminal WebSocket requires signed session | `backend/api/terminal.py` | `test_unauthorized_websocket_falls_through_without_opening_a_pty` |
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
window described in the handoff README. Observability route contracts passed
simulation, but real Grafana/VictoriaMetrics and Ethernet/IB/storage data remain
external gates because the production embed map and collectors are not contained
in this repository.

## Automated evidence

The final source state passed 1,458 backend tests with 1 skipped, all 381
frontend tests, the production TypeScript/Vite build, production-module Ruff,
Python byte compilation, shell/`.sbatch` syntax checks, and Git whitespace
checks. Frontend lint completed with zero errors and 57 existing warnings.
