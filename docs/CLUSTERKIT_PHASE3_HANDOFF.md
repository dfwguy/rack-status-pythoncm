# ClusterKit execution milestone — 2026-10-20

Scope: offline implementation of Phase 3 on the transferred NC repository. No
live Slurm job, allocation, MPI workload, deployment, service change or system
configuration change was performed for this implementation. Earlier U5 inventory
and the separately approved one-CPU availability probe are separate evidence.

## What is implemented

- `backend/clusterkit_planner.py`: print-only plans now reference reviewed static
  one-node preflight or generalized pairwise entrypoints. Exact 1/2/4/8-node
  resources, no GPUs/exclusive allocation/requeue. Profile-driven paths, HCA,
  architecture, parameters and time bounds; no hostname-derived rack membership.
- `backend/clusterkit_execution.py`: reconstruct/validate the complete plan,
  explicitly prepare a fresh attempt (never submit), snapshot and hash execution
  sources, reject changed source/init after preparation, verify exact allocation,
  orchestrate bounded Slurm steps, select/verify per-node global RoCE-v2 GIDs,
  preserve assigned CPU affinity, run one PMIx v3 rank/node and capture outcome.
- `bin/scripts/clusterkit_preflight.sbatch`, `clusterkit_pairwise.sbatch`:
  static reviewed entrypoints; no shell generation or `--wrap`.
- `bin/scripts/clusterkit_rank.sh`: static HPC-X initialization for each Slurm rank.
  Paths/arguments remain literal positional arguments. No SSH fanout or eval.
- `bin/scripts/clusterkit_execute.py`: administrator `prepare` operation and
  compute-only runtime modes. There is deliberately **no submit operation**.
- `backend/clusterkit_results.py`: normalized preflight, process exit, node/GID/
  placement and per-stage pair coverage; bandwidth and latency must both cover
  every expected pair. Zero/missing/nonfinite metrics cannot produce functional
  PASS. Performance remains UNVERIFIED; enabled thresholds fail closed at planning
  until the acceptance service exists.
- `backend/clusterkit_metrics.py`: legacy upper-triangle cell reader extracted
  unchanged from `backend/app.py`. The legacy API imports this same function.
  This is a source-only refactor; no running deployment was touched.
- `backend/clusterkit_profile.py`: cap CPU recipe memory/message sizes and
  iteration counts to fit the reviewed bounded execution recipe.
- Offline tests in `backend/tests/test_clusterkit_execution.py`; existing modular
  planner tests updated for the new reviewed entrypoints. Original successful
  NC two-node shell scripts and legacy planning tests remain byte-for-byte intact.

## Safety and supported policy

Current executable recipe is CPU/RoCE, expected Ethernet, matching global RoCE-v2
GID per node, and rc,sm,self UCX transports. Architecture is configured (ARM or
x86), not hardcoded. HCA may be explicitly configured or discovered; discovery
requires exactly one usable matching active/link-up port, otherwise it refuses.
An optional configured HCA also acts as an explicit discovery constraint.
Unsupported IB/auto-GID/transport recipes produce planning blockers, not silent
policy substitution. CPU binding reuses the proven Slurm CPU-map verification;
SMT/nonidentity/alternate-topology systems fail closed pending an explicit map.

Preflight checks architecture, port ACTIVE and physically LinkUp, link layer,
matching global GID/interface address, assigned CPU, HPC-X executables and MPI/
UCX versions, and the selected UCX device. Every allocated node must produce
valid evidence and distinct GID addresses before any MPI benchmark starts.
One-node preflight never invokes ClusterKit or claims inter-node performance.

All-pairs mode uses installed ClusterKit defaults: no --quick, --fromfile,
--bycore, --bygpu, stress, recovery or retests. Each 2/4/8-node stage expects
1/6/28 unordered pairs independently for bandwidth AND latency. Path comparison
is a separate stage in the same allocation, only when enabled by site policy.
A failed baseline stops further benchmark stages; bounded read-only post-test
observations may still run. They cannot change the primary failure into success.

Inherited UCX, OMPI_MCA and CK overrides are removed before loading HPC-X; PMIx
endpoint environment is retained. The selected device/GID and reviewed transport
and binding settings are then applied explicitly per rank. CK_OUTPUT_SUBDIR is
unset to preserve the proven output-path fix. No GPU allocations are requested.

Default 300-second allocation accommodates the reviewed short recipe. Planning
requires a sufficient time budget: 60 seconds for preflight, or 90 seconds plus
one/two stage budgets of (2 * test_limit_seconds + 30). Runtime further limits
steps to remaining wall time, reserving ten seconds for step cleanup and
fifteen seconds for finalization. Timeout cleanup
only terminates/reaps this runner's own step process group; no scancel, other-job
cancellation, node-state changes, repairs or automatic retries are implemented.
SIGTERM is recorded where possible. SIGKILL/node loss can prevent finalization:
Phase 4 scheduler reconciliation must mark those interrupted attempts, not infer
PASS from absent summary/output.

## Attempt artifacts

Preparation exclusively creates `<results_root>/<label>-<uuid>/`, writes the
exact plan/effective policy, source manifest and source snapshots. It refuses
even an existing empty attempt directory. Batch execution exclusively claims
that attempt and preserves raw allocation environment, scontrol allocation,
Slurm MPI plugin inventory, phase logs, per-node before/after records,
per-stage rank-placement records, bounded UCX logs, unmodified ClusterKit vendor
JSON/text, outcome.json and summary.json. No artifact is opened for replacement.

Slurm creates its stdout file under the newly prepared attempt. The plan preview
itself creates no directories/files and does not submit or reserve anything.
The per-attempt job ID is captured from Slurm at batch entry; final Slurm state
is deliberately UNKNOWN/reconciliation-required until queried outside the job.
A successful process exit alone does not establish coverage or acceptance.

## Evidence and limits of validation

Final local result: **83 tests passed** (29 execution/results + 34 modular
configuration/planning + 20 original legacy tests), plus shell syntax and Python
in-memory compile checks. Shellcheck is unavailable. Full route-parity testing
was attempted but could not start because pytest is not installed. No app/service
was started to work around missing dependencies.

- Offline tests cover all sizes, limits, injection-resistant argument handling,
  per-rank distinct GID indices through rank 7, exact allocations, no GPU/
  exclusive requests, stale source/init, duplicate GIDs, changed GIDs, refused
  ambiguous HCA discovery, preflight gating, primary failure, timeout handling,
  SIGTERM, missing/failed pair coverage, and functional vs performance status.
- Real shell-wrapper test loads ONLY an isolated fake HPC-X script and fake CLI;
  real MPI/Slurm/ClusterKit are never started by the test suite.
- Saved NC job 197016 vendor JSON was read only. Its baseline and single-path
  bandwidth/latency each decode as exactly one pair with unchanged vendor values.
  This is historical parser compatibility evidence, not a live test of new code.
- Current U5 print-only previews were run as cmods using read-only sinfo and an
  audit guard that rejects writes, environment mutation and scheduler/MPI/SSH
  execution. Preflight, explicit 2/4/8 and configured-group 4 plans passed.
- TinyDB/Flask/pytest are missing here. No dependency install was attempted.
  Full app/API/route-parity integration remains for the development environment.
  The shared legacy cell function is checked against the saved baseline AST;
  the original legacy suite tests still run without those app dependencies.

## U5 declarations versus observations

User-provided mapping: `sys1-gso1-p-phy-gpu55-su20` through `gpu72-su20` form U5.
Do not derive that mapping from the hostname suffix. The preview-only profile
records that source explicitly; it was not written to active Settings/catalog.
The group contains the supplied 18 members; its preview allowlist excludes
`gpu63-su20` pending clarification of the three dashboard flags the user showed.
The current preview selections use gpu55 through gpu62 for up to eight nodes.

Observed: the five guarded Slurm previews accepted their selected nodes at the
recorded snapshot times. That neither reserves nodes nor verifies their fabric
health. The earlier successful availability probe was gpu1-su20, outside U5.
HCA roce_vf_r1:1 and the HPC-X path in the preview fixture are site declarations
from the successful SU19 path, not compute-side verification on U5.

## Planned live rollout — separate approvals required

After review and team reservation, refresh inventory/profile and preview each
request. Approve and run the new one-node preflight first. Review its evidence;
then separately approve a two-node regression THROUGH THE NEW execution path.
After review, separately approve four nodes and verify six pairs per metric and
stage. Only then separately approve eight nodes and verify 28 pairs. No entire
rack/SU run is authorized or part of this milestone.

The installed HPC-X ClusterKit README explicitly documents an even count >=2,
one process/node and default full pair coverage (lines 33–40). No IB-team input
is needed to choose that documented mode; actual 4/8 output still needs staged
live verification. Fabric-team input remains necessary for the intended U5 rail,
interpretation of bandwidth aggregation/direction and the earlier 200-Gb/s-versus-
reported-throughput discrepancy, and accepted warmup/iterations/thresholds.
Physical rack/SU scale requires reviewed authoritative topology and acceptance.

## Administrator rehearsal commands (DO NOT submit without approval)

Resolve the administrator profile through the existing Settings/catalog ladder.
For isolated previews only, an administrator can set RACK_CLUSTERKIT_SITE_PROFILE
in that command's environment; do not expose this as an ordinary run input.

```
python3 -B bin/scripts/plan_clusterkit.py plan --local-inventory \
  --request '{"scope":"preflight","nodes":["EXACT_NODE"],"label":"preflight"}'
```

The CLI emits a PRINT-ONLY marker followed by JSON. Save **only the JSON** as a
reviewed plan file. The path must be inside the transferred workspace for this
AppServer workflow. Once staging is authorized, the administrator can run:

```
python3 -B bin/scripts/clusterkit_execute.py prepare --plan REVIEWED_PLAN_JSON
```

This creates a fresh attempt and prints the exact sbatch argv/quoted display;
it does not submit. Recheck current policy/inventory before approving submission.
The printed command is the only intended manual launch path at this milestone.
Do not run compute-only modes directly on the AppServer. If files/profile/init
change after preparation, obtain a fresh preview/attempt; do not repair or reuse
an existing attempt. There is no new backend submit endpoint in this phase.

## Remaining product work

This completes the offline Phase 3 execution milestone, not the entire seven-
phase feature. Phase 4 still needs existing workflow + durable validation activity
integration, current-profile and inventory revalidation at submission, authorization
for administrator policy versus ordinary launches, preview/confirmation binding,
job reconciliation/status/result endpoints, and approved legacy-route migration.
Existing legacy routes intentionally remain unchanged and do not yet inherit the
new profile's limits. The global HTTP bootstrap still needs a genuinely zero-write
planning route. Do not advertise ordinary-user execution or server-wide policy
enforcement before these concerns are implemented.

Phase 5 still needs backend result retrieval/ingestion, interrupted-attempt
reconciliation and verified performance threshold evaluation. The local normalized
summary introduced here is the execution artifact contract for that service.
Phase 6 frontend follows those stable/authenticated contracts and the existing
Rack Status contexts, preview confirmation and validation patterns. No UI or
running deployment was modified here.

## Offline reproduction and transfer

Use PYTHONDONTWRITEBYTECODE=1 and a TMPDIR under the repository exports directory:

```
python3 -m unittest discover -s backend/tests -p test_clusterkit_execution.py -v
python3 -m unittest discover -s backend/tests -p test_clusterkit_modular_plan.py -v
python3 -m unittest discover -s backend/tests -p test_clusterkit_suite_plan.py -v
bash -n bin/scripts/clusterkit_preflight.sbatch bin/scripts/clusterkit_pairwise.sbatch \
  bin/scripts/clusterkit_rank.sh
```

No Git metadata exists in the transferred repository. The export includes changed
source, this handoff, evidence, per-file hashes and patches against recorded local
baselines. Apply/review on the real GitLab branch on the development machine;
do not claim a commit ID or push from this AppServer. New backend modules require
proper deployment packaging/bind mounts later; do not copy this source into the
running Rack Status deployment as an ad hoc update.
