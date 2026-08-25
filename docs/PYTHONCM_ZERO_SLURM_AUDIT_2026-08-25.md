# Nscale TX PythonCM-only deep audit — 2026-08-25

## Decision

This repository is configured and guarded for one deployment target only:

- Customer: **Nscale**
- Cluster: **Nscale TX** (`RACK_CLUSTER_ID=nscale-tx`)
- BCM: **11.x**
- Hardware product line: **GB300**
- Scheduler/execution provider: **PythonCM**

Mayo, Boston, and Firmus have no role in target selection, production
configuration, UI navigation, help/AI guidance, or the Nscale runtime path.
Historical generic parsers, tests, comments, and dormant compatibility scripts
still exist in the shared source repository; they are not an alternate Nscale
provider and cannot be selected by Nscale configuration.

The previous 2026-08-24 completion statement was incorrect. In particular, it
treated PythonCM branches and legacy-compatible HTTP responses as proof that the
whole feature had been converted. The three independent audits below found
reachable legacy operations and validation wrappers with hidden workload-manager,
`pdsh`, or MPI-over-SSH fan-out. This document replaces that statement.

## Acceptance rule

For Nscale TX, a surface is accepted only if it does one of the following:

1. obtains state or performs the operation through PythonCM/CMDaemon; or
2. is rejected before a thread, subprocess, SSH command, or cluster mutation can
   start.

The Nscale runtime must never execute or derive operational state from:

- Slurm commands: `sinfo`, `squeue`, `scontrol`, `sacct`, `sbatch`, `srun`, or
  `scancel`;
- `module load slurm`, `slurm-init.inc.sh`, or a wrapper that invokes either;
- `cmsh`;
- `pdsh`;
- direct compute-node SSH or MPI configured to use SSH/rsh fan-out;
- PythonCM `WlmCluster`/`cluster.workload` data, because that is the configured
  workload manager surfaced through PythonCM rather than ProgramRunner state;
- an arbitrary workflow, preset, installer, or shell wrapper that has not been
  traced and certified as ProgramRunner-only.

Nscale fails closed at four boundaries:

- startup rejects any `RACK_SCHEDULER` value other than `pythoncm`;
- the scheduler factory rejects explicit construction of every other provider
  when the cluster/customer identifies Nscale;
- the host execution boundary rejects legacy executables and known wrappers
  before opening a connection;
- unconverted HTTP routes return a blocked response before starting a worker.

## Independent audit coverage

### Agent 1 — backend/runtime

The backend audit traced status, inventory, scheduler, actions, workflows,
terminal, reports, fabric, timeouts, and durable job state. It found and caused
correction of these material issues:

- `/api/status` could still start the `cmsh` rack-list warmer;
- a scheduler typo could fail open to a legacy provider;
- historical numeric/Slurm activities could pollute the PythonCM queue and node
  allocation overlay;
- PythonCM timeouts could leave a ProgramRunner command running;
- hardware fan-out did not durably record the ProgramRunner UUID;
- fabric probing used direct node SSH;
- workflow and terminal surfaces remained reachable;
- cached healthy node state could be replayed after a failed PythonCM refresh;
- head-local Tools mutations remained callable even after UI removal.

### Agent 2 — Nscale frontend

The frontend audit traced the Racks poll, Scheduler view, rack actions,
validation, multirack, Tools, terminal, workflows, platform selection, help, and
AI/demo content. It found and caused correction of these material issues:

- the 30-second interval had only a constant assertion, not a timer behavior test;
- the active UI still called legacy-named scheduler routes;
- unconverted validation, report, switch, BMC, MAC, IMEX, NVLMapper, and
  system-info actions were enabled;
- hidden typed URLs still loaded terminal, workflows, Tools/installers,
  post-run reports, projects, and catalogs after sidebar entries were removed;
- node and rack pages still exposed per-node terminals;
- the default platform could incorrectly fall back to GB200;
- generic project switching and catalog mutation contradicted the fixed Nscale
  target;
- help and demo AI contained unsupported features and fabricated non-Nscale rack
  state.

### Agent 3 — validation/scripts

The validation audit traced every submission family into its final script and
child launcher. It found that none of the installed validation families met the
ProgramRunner-only rule:

- NCCL, LLMB NCCL, ClusterKit, and NVLoom contained MPI-over-SSH/rsh or hidden
  SSH/`pdsh` fan-out;
- NeMo/Nemotron, GPT-OSS, Exemplar, and Llama depended on Slurm/Pyxis or
  `llmb-run submit`;
- the proposed direct MNUberGEMM command was not semantically equivalent to the
  pinned-container MNUberGEMM workload;
- workflow YAML allowed arbitrary shell execution and old scheduler kinds;
- there is no atomic node-claim/admission layer for concurrent validation
  submissions.

The correct result is not to relabel these recipes. Every validation submission
family is blocked.

## Converted and enforced surfaces

`CODE PASS` means unit/static/integration tests prove the PythonCM path and
legacy sentinels prove no forbidden runner is reached. `LIVE GATE` means the
code path is PythonCM-only but production mutation or output persistence still
requires the credentials/reserved-node acceptance described later.

| Surface | Nscale implementation | State |
|---|---|---|
| Nscale identity/provider selection | fixed Nscale/GB300 config; startup and factory reject non-PythonCM providers | CODE PASS |
| Browser Racks refresh | `/api/status` on initialization, then every 30,000 ms | CODE PASS |
| Server status refresh | 30-second default background refresh; no `cmsh` warmer in PythonCM mode | CODE PASS |
| Rack inventory | PythonCM `Rack` entities | CODE PASS |
| Physical node inventory | PythonCM `PhysicalNode` entities | CODE PASS |
| Device state | PythonCM parallel device-status RPC | CODE PASS |
| Node availability/drain reason | PythonCM node-state RPC plus Rack Status-owned ProgramRunner registry | CODE PASS |
| Failed inventory refresh | cached identity may remain, but state becomes `unknown`; stale healthy state is not replayed | CODE PASS |
| Active/recent jobs | durable activities containing `scheduler=pythoncm` and a valid ProgramRunner UUID only | CODE PASS |
| Job status/detail/output/cancel | PythonCM ProgramRunner UUID and helper manifest; provider-neutral frontend endpoints | CODE PASS / LIVE GATE |
| Drain/resume | PythonCM parallel drain/undrain RPCs | CODE PASS / LIVE GATE |
| Power status/on/off/reset | PythonCM parallel power RPCs | CODE PASS / LIVE GATE |
| GPU reset | exact-node PythonCM ProgramRunner command | CODE PASS / LIVE GATE |
| Service restart | exact-node PythonCM ProgramRunner command | CODE PASS / LIVE GATE |
| Hardware checker safe preset | exact-node PythonCM ProgramRunner; workload-manager preset rejected | CODE PASS / LIVE GATE |
| NVLink preflight | exact-node PythonCM ProgramRunner | CODE PASS / LIVE GATE |
| Fabric probe | selected PhysicalNode through PythonCM ProgramRunner; no node SSH | CODE PASS / LIVE GATE |
| Uptime read | PythonCM ProgramRunner | CODE PASS / LIVE GATE |
| Racks log parsing | log-only `rack_status.sh` mode with CM/scheduler probes disabled | CODE PASS |
| Nscale catalog | Nscale project/cluster is seeded; project/platform/tool mutation is rejected | CODE PASS |
| Scheduler UI | provider-neutral routes and PythonCM wording | CODE PASS |
| AI/help | Nscale-only guidance; no fabricated cluster state; disabled surfaces removed from generated help | CODE PASS |

## Explicitly blocked surfaces

Blocked is an intentional acceptance state: the UI does not offer the action and
the backend rejects direct HTTP calls before execution.

| Surface | Why it is not converted | Enforcement |
|---|---|---|
| NCCL 2.31.2 / full-rack / LLMB NCCL | installed launchers use MPI-over-SSH/rsh or workload-manager setup | all submission routes return blocked |
| NeMo/Nemotron and cross-rack NeMo | Slurm/Pyxis batch and distributed-launch assumptions | all submission routes return blocked |
| Llama/Exemplar Llama | `llmb-run submit` submits to Slurm | all submission routes return blocked |
| GPT-OSS CPU | `srun`/Pyxis recipe; no reviewed direct-enroot equivalent | submission route returns blocked |
| DGXC/Exemplar system-info and generic sysinfo | batch or internal SSH/`pdsh` fan-out | prepare/run/stream routes return blocked |
| ClusterKit | multi-host recipe is not certified free of SSH/workload-manager fan-out | submission route returns blocked |
| NVLoom | script contains sysinfo SSH/`pdsh` and MPI-over-SSH | submission route returns blocked |
| MNUberGEMM | proposed PythonCM command did not preserve pinned-container workload semantics | preset and direct submission routes return blocked |
| Validation presets | no installed preset has a certified ProgramRunner-only implementation | list empty; prepare/run blocked |
| Arbitrary workflows | YAML permits arbitrary shell/wrappers and lacks recipe certification/admission control | workflow list empty; create/run/advance/cancel blocked |
| Interactive terminal | direct interactive SSH is not a PythonCM ProgramRunner operation | UI/deep links removed; WebSocket creation blocked |
| Bring-up and node/fleet reports | current collectors/scripts use legacy fan-out or direct file retrieval | routes blocked; UI route removed where applicable |
| Bug-report collection/bundle | collection/retrieval requires unvalidated file transfer and direct remote reads | async/bundle routes blocked; UI action disabled |
| BMC recovery and MAC change | direct management-plane recipes bypass PythonCM | routes blocked; UI actions disabled |
| NVSwitch audit/fix/SDN/system reset/check | installed scripts contain `cmsh`/`pdsh` or unreviewed recovery steps | routes blocked; UI actions disabled |
| IMEX reset/check | installed scripts are not ProgramRunner-only | routes blocked; UI actions disabled |
| NVLMapper and precheck | unreviewed direct/legacy scripts | routes blocked; UI actions disabled |
| Tools downloads/installers/Grafana launcher | head-local subprocesses or compute build-host `pdsh`; not PythonCM operations | Tools route removed; every `/api/tools/*` status and mutation surface blocked |
| Generic project/platform/tool administration | Nscale is the only target and runtime project switching would change execution context | UI routes removed; catalog mutation APIs blocked |

## Remaining live acceptance gates and limitations

These are not represented as complete:

1. **BCM command credentials.** Certificate-only inventory worked, but BCM 11
   `cmproc`/ProgramRunner operations require a login cookie or username/password.
   Configure `NVIS_BCM_COOKIE_FILE`, or `NVIS_BCM_USERNAME` with
   `NVIS_BCM_PASSWORD_FILE`.
2. **Approved reserved node.** Execute/cancel/drain/resume/power tests mutate or
   occupy production resources. No such test was performed without an approved
   target.
3. **ProgramRunner result storage.** The helper writes per-node result files under
   `RACK_PYTHONCM_JOBS_DIR`. Live acceptance must prove that this directory is
   visible with identical semantics from the BCM head and selected compute node,
   or the helper must be changed to collect output through a supported PythonCM
   result API. Unit tests cannot prove the Nscale mount topology.
4. **Submit/manifest crash window.** PythonCM returns the native UUID only after
   `parallel.execute`. A helper crash between native execution and durable
   manifest write can leave an untracked command. There is no transactional
   execute-and-persist API in the current wrapper.
5. **No atomic node claims.** Rack Status has advisory rack locks, not a durable
   compare-and-set admission controller for exact PhysicalNodes. This is one
   reason multi-node validation remains blocked.
6. **PythonCM is not a recipe translator.** ProgramRunner can execute an exact
   command on selected entities; it does not automatically convert a Slurm/Pyxis,
   MPI-over-SSH, or LLMB submission recipe into an equivalent direct workload.

The BCM 11 Developer Manual documents PythonCM and points to the installed
`parallel-execute*.py` examples on the BCM head, but it does not make the
application's site-specific result-directory mount assumption valid. See the
[NVIDIA BCM 11 Developer Manual](https://docs.nvidia.com/base-command-manager/manuals/11/developer-manual.pdf).

## Compatibility names versus execution

The shared repository still has Slurm-era schema and source names, including
some `/api/slurm-*` HTTP aliases, `sinfo`/`squeue`-shaped payloads, stored
`slurm_state` fields, parser/helper filenames, legacy provider code, and legacy
tests/scripts. They are not evidence of a Slurm call, but they are also not
being hidden in this audit.

For Nscale:

- the active frontend uses `/api/scheduler-*` endpoints;
- every `/api/slurm-*` compatibility URL returns HTTP 404 in PythonCM mode;
- the API reference hides `/api/slurm-*` aliases;
- startup/factory enforcement makes the Slurm scheduler unselectable;
- the host boundary rejects Slurm/`cmsh`/`pdsh` executables and known wrappers;
- registry rows must be explicitly PythonCM-provenanced and UUID-addressed.

Removing every dormant compatibility module and every historical schema token
would be a separate destructive API/data migration. It is not required to prove
the Nscale runtime is PythonCM-only, and it has not been represented as done.

## Evidence

- Focused PythonCM zero-Slurm route/runtime audit: **91 passed**. This includes
  enumerating every registered `/api/slurm-*` method and proving it returns 404
  before its handler runs.
- Focused OpenAPI: **21 passed, 1 skipped**; validation preset gates:
  **15 passed**; workflow enforcement: **80 passed**.
- Frontend: **379 passed** across 53 files.
- Production TypeScript/Vite build: **passed**; generated Nscale help registry:
  **59 entries**.
- Frontend lint: **0 errors** (existing warnings remain in dormant source files).
- Backend production modules: Ruff **passed**.
- Full backend suite: **1,501 passed, 1 skipped**.
- Changed shell scripts: `bash -n` **passed**; backend byte compilation and diff
  whitespace checks **passed**.
- Artifact hashes and remote upload/readback are release handoff steps and are
  reported with the package; they are not evidence of live cluster acceptance.

No live ProgramRunner execution, cancellation, drain/resume, or power mutation
is claimed in this audit.
