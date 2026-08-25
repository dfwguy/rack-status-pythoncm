# Nscale Rack Status: PythonCM-primary feature map

This is the deployment and handoff README for the Nscale TX Rack Status build.
It describes the runtime provider used by every operator-visible feature, the
reason for each retained fallback, and the tests that prevent recurring Slurm
load from returning to the Racks view.

## Scope and decision rule

This build has one production target:

- customer: **Nscale**;
- cluster: **Nscale TX** (`RACK_CLUSTER_ID=nscale-tx`);
- BCM: **11.x**;
- product line: **GB300**;
- primary scheduler/execution provider: **PythonCM**.

Mayo, Boston, Firmus, and any similarly named historical site are not runtime
targets, configuration fallbacks, UI choices, or provider-selection inputs for
this build. The shared source tree still contains generic hostname parsers,
tests, and historical documents used by the upstream project. They do not take
part in Nscale target resolution.

The provider rule is:

1. Use PythonCM whenever BCM exposes an equivalent operation.
2. Keep the original feature available when PythonCM cannot preserve its
   semantics.
3. Label that path as a Slurm or transport fallback and invoke it only when the
   operator requests that feature.
4. Never put a Slurm command, `cmsh`, `pdsh`, or direct compute-node SSH on the
   recurring Racks/status refresh path.

This deliberately differs from a “PythonCM or disabled” policy. PythonCM is a
management and remote-execution API; it is not a translation layer for Slurm
allocations, Pyxis containers, MPI launcher topology, LLMB submissions, an
interactive PTY, or arbitrary vendor recovery scripts.

## Provider flow

```text
Browser Racks poll (30 s)
        |
        v
  /api/status -----> PythonCM Rack + PhysicalNode inventory
        |            PythonCM device status + drain state/reason
        |            durable Rack Status activity registry
        |            cached/parsing data on dashboard host
        |
        +----------> NO squeue/sinfo/sacct/scontrol/cmsh/pdsh/SSH

Operator starts a feature
        |
        +-- equivalent exists ------> PythonCM API / ProgramRunner UUID
        |
        +-- semantic gap -----------> explicit Slurm or transport fallback
                                      only for that requested operation
```

## Recurring and read-only surfaces

| Feature | UI/API surface | Provider | What happens |
|---|---|---|---|
| Racks refresh | Racks view, `GET /api/status` | PythonCM | Initial request and 30,000 ms browser interval read the cached canonical snapshot. Server background refresh defaults to 30 seconds. |
| Rack inventory | Racks, multirack, selectors | PythonCM | BCM `Rack` entities provide rack identity. The rack-name cache defaults to 900 seconds. |
| Physical nodes | Racks, Nodes, target pickers | PythonCM | BCM `PhysicalNode` entities provide exact node names, rack placement, serial/MAC/interface data, and BMC address where exposed. |
| Device health | status cells and counts | PythonCM | `cluster.parallel.device_status(...)` supplies device state. |
| Drain state/reason | Scheduler, Racks, node report | PythonCM | `cluster.parallel.drain_status(...)` supplies drained/undrained state and reason. |
| Allocation/activity overlay | Racks and Scheduler | local registry | PythonCM UUID and explicit Slurm fallback rows are read from the durable activity registry. This does not query Slurm. |
| Active/recent jobs | Scheduler | hybrid registry | Each row carries provider provenance. UUID rows are PythonCM; numeric rows are Slurm fallbacks. |
| Job detail | Scheduler/job dialog | hybrid, targeted | A UUID calls PythonCM status. A numeric fallback ID makes one targeted `scontrol show job` request when the operator opens or polls that job. |
| Job output | job/log dialogs | local/remote file path | Reads the output path recorded by the selected provider; no fleet-wide queue query. |
| Job cancel | `POST /api/scheduler-cancel` | hybrid, targeted | UUID calls PythonCM kill; numeric fallback ID calls one targeted `scancel`. |
| Post-run inventory | Post Run Node Report | PythonCM | Current node identity/state comes from PythonCM. |
| Post-run accounting | Post Run Node Report | hybrid, on demand | PythonCM UUIDs reconcile from the registry; numeric fallback IDs use one batched `sacct` query only when the report is explicitly refreshed. |
| Physical rf0 | rack/node details | PythonCM | BMC/rf0 address is read from PythonCM entity/interface data. |
| Log parsing/history | Racks, Analytics, Reports | dashboard host | Parses existing result files with cluster probes disabled. No scheduler call is required. |
| Comments/issues | Racks and node reports | dashboard host | Local durable metadata; optional drain uses PythonCM. |
| Cluster version/capabilities | overview/settings | PythonCM | PythonCM/CMDaemon version and capability probes replace `scontrol -V` and `cmsh versioninfo`. |
| Fabric probe | Infrastructure | PythonCM ProgramRunner | Executes the node-local probe on an exact PhysicalNode and returns per-node output. |
| Uptime | rack actions | PythonCM ProgramRunner | Runs `uptime` on exact PythonCM-resolved nodes. |

### The 30-second Racks guarantee

`frontend/src/hooks/useRackStatus.ts` schedules `/api/status` at 30,000 ms.
`backend/app.py` does not start the `cmsh` warmer when PythonCM is primary.
`backend/native_status.py` obtains rack/node state from `scheduler_runtime`, and
`scheduler_runtime.active_job_times()` reads only the durable registry. Slurm
fallback rows can remain visible, but the refresh never runs `squeue`, `sinfo`,
`sacct`, `scontrol`, `sbatch`, `srun`, or `scancel`.

## Rack and node actions

| Feature | Provider | Implementation and notes |
|---|---|---|
| Drain nodes | PythonCM | Exact target validation, then `cluster.parallel.drain(nodes, reason=...)`. |
| Resume nodes | PythonCM | Exact target validation, then `cluster.parallel.undrain(nodes)`. |
| Power status/on/off/reset | PythonCM | Parallel device power API. The UI keeps its confirmation and reserved-target safeguards. |
| GPU reset | PythonCM ProgramRunner | Runs the reviewed node-local GPU reset command on the selected PhysicalNodes. |
| Restart services | PythonCM ProgramRunner | Runs node-local mount and NVIDIA IMEX/DCGM service restart commands on the selection. It does not restart `slurmd` in PythonCM mode. |
| Hardware checker, safe presets | PythonCM ProgramRunner | Runs the node-local checker on exact selected nodes. A preset that embeds workload-manager behavior is not relabelled PythonCM. |
| NVLink preflight | PythonCM ProgramRunner | Executes the node-local preflight and parses per-node results. |
| Hardware/network deep-audit workflow | hybrid workflow engine | PythonCM is used for declared fan-out steps. A workflow step that explicitly needs a `.sbatch` allocation uses Slurm; head-local/script steps use their declared transport. |
| Bug-report generation | PythonCM ProgramRunner | Runs `nvidia-bug-report.sh` on selected nodes. |
| Bug-report bundle retrieval | SSH/file transport fallback | PythonCM ProgramRunner has no file-download/interactive transfer equivalent in the current BCM integration, so collection is PythonCM but retrieval uses the existing explicit transport path. |
| BMC recovery | management-script fallback | The current cold-reset recipe uses `ipmitool` against rf0. PythonCM provides node power control but not an equivalent BMC-controller reset operation. |
| MAC change | management-script fallback | The existing network-interface mutation and verification sequence has no proven PythonCM equivalent. |
| NVSwitch fix | vendor-script fallback | Preserves the installed NMXC recovery sequence. |
| NVSwitch SDN reset | vendor-script fallback | Preserves the installed controller reset sequence. |
| NVSwitch system reset | vendor-script fallback | Preserves the installed whole-system reset ordering. |
| NVSwitch audit/check | vendor-script/transport fallback | Uses the established audit/check scripts where PythonCM lacks the switch-specific diagnostic surface. |
| IMEX reset/check | vendor-script fallback | Preserves the rack-specific IMEX recipes. PythonCM ProgramRunner is used only where the script can be executed node-locally without changing its meaning. |
| NVLMapper | vendor-tool fallback | PythonCM has no NVLMapper topology-analysis API. |
| Precheck | vendor-script fallback | Preserves the installed precheck semantics. |
| System info | script/transport fallback | The installed collector performs its own fleet aggregation and cannot be replaced by a single ProgramRunner command without changing output semantics. |

## Validation and benchmark features

All validation controls are enabled. Safe MNUberGEMM stage-only copying uses
PythonCM. The workload recipes themselves have no semantically equivalent
PythonCM form, so they use explicit, on-demand Slurm fallbacks. They never run
because the Racks page refreshed.

| Feature | Fallback reason |
|---|---|
| Single-rack validation / Nemotron | Requires Slurm allocation, Pyxis/container integration, and distributed launch semantics. |
| Cross-rack NeMo | Requires multi-rack allocation plus the established Pyxis/distributed launcher behavior. |
| NCCL full-rack | The accepted recipe uses MPI launcher topology and SSH/rsh bootstrap; ProgramRunner fan-out is not equivalent to one coordinated MPI job. |
| NCCL 2.31.2 preset | Same MPI coordination requirement, plus pinned CUDA/NCCL acceptance checks. |
| LLMB NCCL | `llmb-run` owns workload submission and scale/topology behavior. |
| Llama 3.1 405B FP8 | `llmb-run submit` and the exact 256-node/1024-GPU allocation are part of the validated recipe. |
| Exemplar Llama | Same LLMB/Slurm allocation semantics. |
| NeMo 26.04.01 preset | Requires the established Pyxis/container and distributed training recipe. |
| GPT-OSS CPU | The accepted recipe uses `srun`/Pyxis; a direct-enroot ProgramRunner version has not been validated as equivalent. |
| DGXC Exemplar system_info | Packaged as a batch/fan-out recipe whose result layout is part of the feature. |
| ClusterKit | Uses an external coordinated multi-host launcher. Independent ProgramRunner commands would change the test. |
| NVLoom | Current recipe combines sysinfo collection and MPI-over-SSH. |
| MNUberGEMM 26.03-v1.17 | Stage-only container copy uses PythonCM ProgramRunner once per exact rack. The workload itself uses Slurm because a proposed direct `dcgmi mndiag` command is not the pinned-container MNUberGEMM workload. |

Preset launch responses and durable activities identify the execution provider
as `slurm` and retain a human-readable fallback reason. Numeric scheduler job
IDs remain cancellable and inspectable through the provider-neutral Scheduler
API.

## Other restored application features

| Feature | Provider | Notes |
|---|---|---|
| Interactive head/node terminal | SSH/PTTY fallback | PythonCM ProgramRunner is non-interactive and cannot adopt xterm sessions. The Terminal page, detached tab, grid, and per-node launch are restored. |
| Workflows | per-step hybrid | Default exact-node fan-out uses PythonCM. Explicit `.sbatch`/`scheduler.kind: slurm` steps use Slurm. Explicit SSH hops and head-local steps keep their transport. |
| Bring-up configuration and reports | local/script/transport fallback | These features orchestrate existing report files and site scripts, not a scheduler-equivalent operation. |
| Post-run node reports | PythonCM inventory plus on-demand accounting fallback | Feature and operator marking/drain are restored. |
| Tools/installers | dashboard host plus explicit build-host fallback | Downloads and local preparation run on the dashboard host. Existing compute build/install steps use their documented transport/Slurm dependency where PythonCM cannot reproduce the installer. |
| Reports/downloads | dashboard filesystem | Read-only listing/download remains local; remote collection provider is described separately above. |
| Projects & Clusters | application database/config | Restored. It does not change the fixed Nscale production target unless an administrator explicitly changes deployment configuration. |
| Platform Catalog | application database/config | Restored for metadata management; active Nscale runtime still resolves `nscale-tx`. |
| Tool Registry | application database/config | Restored for tool metadata and paths. |
| API Reference | local OpenAPI/help data | Provider-neutral scheduler routes are primary; historical `/api/slurm-*` aliases remain callable for client compatibility. |
| AI/help | local content plus configured LLM | Guidance says PythonCM-primary and names explicit fallbacks; it no longer claims features are blocked or invents live Nscale state. |

## PythonCM examples used by this build

The snippets below are reduced from the shipped helpers, not hypothetical API
names.

### Inventory and status

```python
from pythoncm.cluster import Cluster

cluster = Cluster(settings)
racks = cluster.get_by_type("Rack")
nodes = cluster.get_by_type("PhysicalNode")
states = cluster.parallel.device_status(nodes)
drain_states = cluster.parallel.drain_status(nodes)
```

Rack Status shapes entity-valued attributes into plain JSON and preserves BCM's
authoritative hostname/rack spelling.

### Drain and resume

```python
nodes = [cluster.get_by_name(name) for name in exact_node_names]
cluster.parallel.drain(nodes, reason="Rack Status operator maintenance")
cluster.parallel.undrain(nodes)
```

### Power

```python
operation = getattr(cluster.parallel, f"power_{action}")
completed, states = operation(nodes, force=False, wait=True, timeout=120)
```

`action` is constrained to `status`, `on`, `off`, or `reset` by the shipped
helper.

### ProgramRunner submit/status/cancel

```python
success, status = cluster.parallel.execute(
    nodes=execution_nodes,
    command=wrapped_node_local_command,
    env=environment,
    user=execution_user,
    max_run_time=timeout,
    wait=False,
    info="Rack Status: hardware-check",
)
pythoncm_id = str(status.uuid)

active = cluster.parallel.active_command_status(ids=[UUID(pythoncm_id)])
for command_status in active:
    command_status.kill()  # cancellation
```

The native UUID, exact nodes, launch mode, result paths, and terminal state are
persisted by Rack Status. `fanout` executes once per selected node; `leader`
executes only on the first selected node while retaining the full allocation
metadata; `head` resolves the active BCM head.

### Slurm fallback example

An NCCL launch keeps its exact accepted script. Rack Status records the activity
with `scheduler="slurm"`; the launcher submits its allocation, records the
numeric job ID, and returns immediately to asynchronous status tracking. Later:

```text
GET  /api/scheduler-job-status?job_id=12345  -> targeted scontrol
POST /api/scheduler-cancel {"job_id":"12345"} -> targeted scancel
```

No fleet-wide Slurm query is added to `/api/status`.

## Configuration

```bash
RACK_CLUSTER_ID=nscale-tx
RACK_CLUSTER_NAME=Nscale-TX
RACK_CLUSTER_CUSTOMER_NAME=Nscale
RACK_CLUSTER_PRODUCT_LINE=gb300
RACK_PLATFORM=bcm
RACK_SCHEDULER=pythoncm

NVIS_BCM_PYTHON=/cm/local/apps/python312/bin/python3
NVIS_PYTHONCM_PATH=/cm/local/apps/cmd/pythoncm
NVIS_BCM_HOST=127.0.0.1
NVIS_BCM_PORT=8081
NVIS_BCM_CERT=/path/to/cert.pem
NVIS_BCM_KEY=/path/to/cert.key
NVIS_BCM_CA=/path/to/cluster-ca.pem
NVIS_BCM_COOKIE_FILE=/path/to/cookie
# or NVIS_BCM_USERNAME plus NVIS_BCM_PASSWORD_FILE
```

Secrets must not be placed in this repository, an artifact manifest, or a
frontend setting.

## Deployment and live acceptance

This change adds backend modules and frontend routes. Rebuild and recreate the
backend/frontend deployment; a plain container restart cannot add a module that
was not mounted into the old image.

Read-only PythonCM inventory was validated against BCM 11 and returned 220 racks
and 3,943 PhysicalNodes. The remaining live gate is controlled mutation and
compute execution. It requires a BCM login cookie or username/password file and
an administrator-approved reserved node. On that node, acceptance must prove:

1. harmless ProgramRunner submit, UUID persistence, status, stdout/stderr, and
   completion across an application restart;
2. cancellation kills the remote process;
3. drain/resume state and reason round-trip through PythonCM;
4. power only when separately authorized;
5. each retained Slurm validation launches, reports, tails, and cancels through
   its numeric job ID;
6. no recurring `/api/status` request starts a Slurm or `cmsh` command.

No destructive or compute mutation is claimed without those credentials and an
approved node.

## Automated verification for this package

- backend: **1,440 passed, 1 skipped**;
- frontend: **379 passed** across 53 test files;
- TypeScript and production Vite build: **passed**;
- generated help registry: **78 entries**, including every navigable view;
- frontend lint: **0 errors** (57 existing warnings);
- production backend Ruff and Python byte compilation: **passed**;
- shell and `.sbatch` syntax checks: **passed**;
- Git whitespace check: **passed**.

## Verification documents

- Detailed independent audit: `docs/NSCALE_PYTHONCM_HYBRID_AUDIT_2026-08-25.md`
- Deployment procedure: `docs/PYTHONCM_MIGRATION.md`
- Live validation record: `docs/PYTHONCM_VALIDATION_2026-08-24.md`
