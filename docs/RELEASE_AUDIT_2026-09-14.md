# Rack Status 16K release audit

**Audit date:** 2026-09-14

**Audited tree:** Nscale TX Rack Status, branch `16k-port`

**Baseline commit:** `b335d6e` (`fix: enforce complete 224-rack inventory`)
**Scope:** every tracked modification and non-ignored new file in the release candidate, relative to the baseline above

## Executive verdict

This release candidate is a substantial functional upgrade, not a small UI patch. The audited
working tree changes 106 tracked files (7,178 insertions and 1,007 deletions) and adds 24 source,
test, configuration, and documentation files. The main outcomes are:

1. A production-scale Validation Status floor map for all 14 SUs, 224 racks, and 4,032 expected
   compute nodes, with current and historical validation evidence, exact failure attribution,
   filtering, investigation, ETA logic, and CSV/PDF/JPEG exports.
2. Live Slurm state **and reason** in the rack node view. Slurm remains the authority for scheduler
   allocation state even when PythonCM remains the primary Rack Status command provider.
3. Exact-node, fail-closed launch contracts for single-rack and multi-rack NCCL, NeMo,
   MNUberGEMM, and Llama validation, including container provenance, allocation-scoped staging,
   submit identity, job receipts, and terminal-state validation.
4. Durable, idempotent validation evidence for NCCL, NeMo, MNUberGEMM, DCGM, and OVA-local
   single-node health checks. A newer failure can now override an older acceptance pass without
   deleting history.
5. Bounded bring-up aggregation and stale-while-refresh caching intended to prevent a browser's
   30-second refresh from multiplying expensive `verdict-scan` work across the app server.
6. Corrected Nscale observability URLs, rack/SU labels, embedded Grafana dashboards, parser
   attribution, log discovery, and tool polling behavior.

The source-level blockers found during the audit were corrected in the isolated release tree, and
the final automated test matrix is green. A second security pass also removed embedded NVSwitch/BMC
credentials, hardened deployment SSH and web-token handling, and required the distributable Git
bundle to start from a fresh sanitized root commit so predecessor secrets cannot travel in history.

## Findings and resolution

### R1. Scheduled evidence refresh sudo mail — resolved in release

The audit found `bin/rack-validation-evidence-refresh.sh` invoking:

```text
/usr/bin/sudo -n /usr/local/bin/docker exec rack-status-rack-status-1 ...
```

The isolated release now uses direct `/usr/local/bin/docker exec` through the service account's
Docker-group membership. Its source comment explicitly forbids sudo from the timer and requires a
socket-permission failure to fail closed. Deployment verification must still execute this exact
command as the unit's `User` before enabling the timer. Sudo mail remains enabled.

### R2. Recurring app-server journal access — resolved in release

The audit found `bin/scripts/rack_status_hourly_audit.py` executing:

```text
journalctl -u nvis-pythoncm-broker.service --since "1 hour ago" --no-pager
```

That probe was local to the **app server**, not the head node, but still conflicted with the
Alloy/NHC-only recurring-monitoring policy. The isolated release removes the journal request and
derives the bounded recent error counts from container logs already collected by the audit.

Compute-side diagnostic scripts also contain `journalctl`/`systemctl` calls. Those are
operator-launched, allocation/node-local diagnostics rather than browser refresh polling. They
should remain separately documented and permission-scoped; they are not evidence that the UI is
querying the head-node journal.

### D1. Reported `sudo docker ps` mail source — traced and stopped

The audited repository contains no `sudo docker ps --filter name=rack-status-rack-status-1`
command. Read-only process and tmux evidence traced the exact 30-second loop to an out-of-tree
Codex audit session named `rack-status-audit`, created on 2026-09-06. It was not Rack Status UI
code, browser refresh logic, systemd, or a command executed on the head node. That loop was stopped
on 2026-09-14. Deployment acceptance still searches installed units, timers, cron, tmux, and
running command lines so an out-of-tree observer cannot be mistaken for product behavior again.

### R3. Initial multi-rack compatibility regressions — resolved and covered

The first full audit run completed with **1,589 passed, 1 skipped, and 10 failed**. The failures
clustered in multi-rack NCCL/NeMo preparation and compatibility behavior: exact-campaign handling
became mandatory in paths whose tests still expect legacy `ct_blob`/single-rack behavior. This is
not a reason to weaken exact-node admission. The release fix must preserve both contracts:

- rack-detail `rack` requests retain full-rack, 18-node semantics;
- multi-rack requests carrying a campaign ID remain pinned to the exact eligible campaign nodes;
- backward-compatible calls without a campaign must have one explicit documented behavior;
- stale campaign IDs and ineligible racks continue to fail closed.

The release updates the app/API tests to carry or mock the exact campaign, moves legacy NeMo
naming expectations to the shared compatibility helper, and adds exact-node NeMo preparation
coverage. The final isolated-tree gates are:

- backend after final security remediation: **1,608 passed, 1 skipped, 0 failed**;
- frontend: **62 files, 469 tests passed**;
- TypeScript type-check and production build: passed;
- `git diff --check`, changed-shell syntax, Python compileall, JSON parsing, and no-vault scan:
  passed;
- ESLint: **0 errors**, 59 existing-style warnings recorded.

### R4. Embedded infrastructure credentials and unsafe release history — resolved in artifacts

The independent release-security pass found literal administrative credentials in four legacy
NVSwitch/BMC helpers and in predecessor Git history. The release replaces them with protected
mode-0400/0600 credential-file inputs, uses `sshpass -f` rather than password-bearing argv, creates
Redfish temporary inputs with mode 0600, and fails closed when files are absent or unsafe. A new
scanner and credential regression test cover the current tree. All three distinct predecessor
literals were derived without printing them and have zero occurrences in the sanitized tree.

The distributable Git bundle is created from a new one-commit repository populated only from the
sanitized release tree. It therefore contains none of the predecessor objects. The affected
non-factory credentials must still be rotated by their owner; cleaning source does not invalidate
a credential that was previously exposed.

### R5. Deployment SSH and dashboard token handling — resolved for unattended deployment

Deployment script synchronization now requires an explicit non-root identity, pinned regular
`known_hosts`, `BatchMode=yes`, and `StrictHostKeyChecking=yes`. It rejects root and identity
mismatches. Dashboard authentication supports a mounted token file; Compose and the production
build helper no longer require the token value in the container environment. Compatibility with
the empty legacy environment key remains for rolling migration, after which the old token should
be rotated. Permissive legacy interactive-terminal transport is documented as an operator-only
exception and is not authorized for deployment or background automation.

## Architecture and authority boundaries

The changes intentionally separate three concerns that had previously been conflated:

| Concern | Authority in this release | Important boundary |
|---|---|---|
| Physical inventory, device state, power/control operations | PythonCM/BCM | Uses the PythonCM query/exec brokers and existing restricted host execution; it is not scheduler-idle truth. |
| Current allocatability, Slurm state, Slurm reason, exact validation admission | Slurm | Targeted, bounded `sinfo`/accounting queries and Slurm/Pyxis job submission only where the recipe requires it. |
| Service health and standard platform monitoring | Existing NHC/Alloy/AIC stack | Rack Status should consume exposed evidence, not create a second recurring head-node journal monitor. |

`RACK_SCHEDULER=pythoncm` remains the command-provider setting. The new
`RACK_NATIVE_SINFO_SOURCE=slurm` independently makes the rack availability snapshot use Slurm's
4,032-node inventory. This hybrid is deliberate: a PythonCM physical state such as `up` does not
mean that Slurm considers the node idle, responsive, or eligible for an exact allocation.

The default SSH identity changes from `hlakshminara` to the shared `cmods` account. The release
does not introduce a requirement for unrestricted sudo on the head node. Slurm-specific workload
submission, exact allocation checks, and PythonCM execution should continue through the existing
reviewed brokers/clients. Any new `nvis-ova-monitoring` privilege is outside this release and must
be separately reviewed.

## Enhancement audit

### 1. Validation Status and fleet visualization

The former Post Run Node Report becomes a Validation Status experience while retaining the
detailed node investigation and hardware/network audit sections.

- The topology contract is 14 ordered SU rows, 16 racks per SU, and 18 node positions per rack.
  Production ordering is explicit: `dh02-a` through `dh02-k` map to SU1-SU11, followed by
  `dh01-a`, `dh01-b`, and `dh01-c` as SU12-SU14. Unknown physical rows are shown as unmapped;
  they are not assigned an SU by lexical accident.
- The on-screen representation uses one accessible rack control with compact node cells, reducing
  DOM cost while still exposing raw evidence in hover/focus and the node detail dialog.
- Views include All validations, Node Health, Slurm, DCGM, DCGM r1, DCGM r3, NCCL, NeMo 56B,
  Llama 3.1 405B, MNUberGEMM, plus discovered validation domains such as `llm-pretrain` and
  `llm-pretrain-solo`.
- Status normalization is centralized. PASS/idle/up are green; explicit failures/down/drain/
  cancellation are attention; warning/stale/incomplete are warning; actual executing states are
  running; missing required evidence is NOT_RUN/unknown and never promoted to healthy.
- All-view precedence is fail closed. Superseded indexed runs remain available for history but do
  not inflate the current failure rate or override a newer authoritative attempt.
- Browser refresh is 30 seconds and guarded against overlapping requests. Node selection lazily
  fetches full history while the map uses the compact current-state response.
- SU and cluster ETA calculations use real timestamped pass transitions. They abstain for too few
  samples, missing data, blocked work, or a stalled completion stream.
- CSV export uses versioned schema `rack-status-validation-v2`, sanitizes spreadsheet formulas,
  and carries source, evidence time, Job ID, reason, metric, provenance, and prediction fields.
  JPEG is 3,300 x 2,550 with 300-DPI metadata; PDF is 11 x 8.5 landscape with exception details.

The implementation note records prior live QA at 4,032-node fixture scale and detailed 2026-09-02
deployment evidence. Those historical numbers are useful provenance, not a substitute for this
release's final validation on the current artifact.

### 2. Slurm state and reason in Rack View

The empty per-node action column is replaced with live scheduler information:

- `GET /api/scheduler-node-status?nodes=...` accepts an explicit, de-duplicated list, validates
  each node name, and caps the request at 512 nodes.
- `_run_sinfo_nodes` asks Slurm for `%N|%T|%E`, retains caller order, clears only null reasons,
  marks missing nodes `UNKNOWN / Not reported by Slurm`, timestamps the snapshot, and exposes
  timeout/stderr without inventing state.
- `schedulerService.ts` performs a no-store request and turns non-OK responses into visible errors.
- `NodeSelector` shows a colored Slurm state tag, the full Slurm reason, refresh timestamp/error,
  and a manual refresh control. Node terminal access remains available only for an up node.
- Rack-level availability counts and the Slurm tray column now derive from the same Slurm node
  rows, avoiding a misleading mixture of PythonCM counts and Slurm text.
- `idle*` retains the Slurm NOT_RESPONDING marker and is not accepted as idle for validation.

This feature performs a targeted Slurm query for the currently displayed nodes. It does not query
`journalctl`, and it does not require unrestricted head-node sudo.

### 3. Authoritative availability and node-down reconciliation

`native_status.py` separates the availability source from the command provider and adds explicit
fleet accounting:

- live total, idle/responding, allocated/mixed/other, drained, other-down, invalid, and
  no-response-overlap counts;
- one Slurm-derived tray column per rack;
- optional reconciliation against the existing node-down Google Sheet, cached for 120 seconds;
- mismatch/delta and sheet timestamp surfaced in Worker Status;
- a Sheets token mounted read-only as `/run/secrets/gsheets-token` rather than embedded in source;
- transient Sheet failures (429/5xx/network) retried with bounded exponential delay, while
  permission failures are not retried.

Risks: the spreadsheet ID and default host token path are site-specific. A missing/unreadable
token must degrade to an explicit reconciliation-unavailable state, never block rack inventory or
mark unknown nodes healthy. Secret material must remain excluded from Git and release tarballs.

### 4. Durable validation results and evidence

The results-history schema and ingestion model are extended rather than replacing old records:

- ingestion checkpoints contain source ID, fingerprint, path, mtime, size, collected time, row
  count, and update time;
- a whole indexed batch is transactional; invalid data rolls back before the checkpoint advances;
- identical evidence is idempotently skipped;
- result queries can filter by source;
- `latest_runs_by_rack` returns the newest verdict from a named authoritative source separately
  from the older monotonic-acceptance query;
- NCCL, NeMo, and MNUberGEMM collector rows have stable current rack/stage identities, allowing a
  newer failure or incomplete run to supersede a prior pass without deleting the pass history;
- parser schema support extends through export version 8 and includes MNUberGEMM rank samples and
  qualified-rank counts.

DCGM ingestion reads only the bounded `validated-racks.json` collector index. It does not rescan
the large validation log lake, journals, switches, or remote hosts. It retains explicit r1/r3/r4
test identity, artifact path, verdict, mapping method, Slurm context, and retry history. `.prevN`
attempts are history-only; one newest non-history attempt per node/test is authoritative. Missing
required DCGM checks become NOT_RUN.

The new `healthcheck_evidence.py` reads the OVA-local, read-only
`/cluster/OVA/healthchecks/logs/<node>/.last-check` mirror. It validates node names, caches by
marker stat signature, emits one authoritative event per node, provides node/rack filters, and
uses the mirrored `health.log` only to summarize the latest failure. This is consumption of an
existing single-node health-check result, not a new NHC replacement or remote poller.

### 5. Evidence attribution and log selection

The release closes several ways a real validation result could be lost or assigned to the wrong
rack:

- deterministic Nscale SU/rack campaign directories are understood in addition to the legacy
  flat log tree;
- generic scheduler filenames are attributed through durable activity/task metadata and exact
  stdout paths, not by guessing a rack from arbitrary text;
- only reviewed NCCL/NeMo result basenames are eligible; staging/setup logs are excluded;
- full-rack selection verifies the bounded HOSTLIST/node count instead of trusting a filename;
- MNUberGEMM rack attribution accepts only an exact campaign path, a single-rack validated
  HOSTLIST, or unambiguous task nodes; conflicting sources abstain;
- the raw-log ZIP and Excel exports use the same `*.out` population shown in parser results;
- parser results expose scheduler Job IDs and launcher tails even when a wrapper exits in error;
- a compact `view=map` report removes duplicate rack trees and superseded rows for routine
  refresh, while exact-node detail retains complete history.

### 6. Parser correctness and performance

`nvis_logscan.py`, `parser.sh`, and `universal_parser.py` consolidate repeated full-file reads into
a bounded-memory single scan, while retaining an environment-controlled legacy shell fallback.
The scan classifies every readable file, preserves a universal inventory, captures sanitized
head/tail evidence, extracts NCCL and NeMo series, and avoids treating generic NCCL debug chatter
inside training logs as an NCCL result.

MNUberGEMM semantics are materially stricter:

- sample count is no longer presented as rank count;
- expected ranks derive from topology (`nodes * 4`);
- positive GFLOP/s work is required;
- explicit `QUAL_FAIL`, `MNUB [E]`, CUDA/ECC/Xid/abort failures win over progress;
- normal teardown passes only when all expected ranks emitted QUAL_OK;
- an expected wall-time termination is timeout only after positive work; all-zero work fails;
- partial positive output without terminal evidence remains a hint/unknown, not a fabricated pass.

Five-collective parsing similarly requires numeric raw correctness/performance evidence; wrapper
PASS markers alone do not manufacture a parser pass.

### 7. Multi-rack campaign and exact target selection

The campaign API can load a reviewed manifest or derive a live scheduler snapshot. Each rack has
an exact eligible node list, reason, and expected nodes-per-rack (default 16). UI requests include
the campaign ID, and launch preparation rejects stale IDs, racks outside the campaign, insufficient
idle nodes, invalid/nonresponding states, and missing exact inventory.

The boundary is explicit:

- multi-rack work uses exactly the campaign's 16 eligible nodes per rack;
- a rack with 16 idle nodes remains eligible even if two other trays are unavailable;
- single-rack validation keeps full 18-node rack semantics;
- bulk frontend actions use backend-authoritative physical inventory and never synthesize
  full Nscale hostnames;
- missing inventory blocks power/validation actions with a visible refresh instruction.

The initial regression failures show that legacy no-campaign API behavior needs a final explicit
compatibility decision. Do not silently reconstruct an exact multi-rack target with a hardcoded
`ct01-ct18` joiner.

### 8. NCCL launch hardening

The Rack Table's default full-rack NCCL action now selects the audited container dispatcher rather
than assuming an operator-specific LLMB workspace exists. The dispatcher:

- resolves the exact Slurm hostlist before any submit;
- rejects unsafe/home log directories and verifies shared-group write/setgid permissions;
- verifies a pinned source recipe/hash and staged image size/SHA/runtime versions;
- performs staging and GPU/IB readiness inside the exact Slurm allocation;
- records stage and workload Job IDs plus resolved stdout paths;
- rejects cancelled/failed terminal state even if `sbatch --wait` itself returns zero;
- never replays an ambiguous submit;
- uses the direct reviewed five-collective recipe when LLMB is unavailable, and retains an
  explicit 24-test success contract only when the LLMB adapter is actually used.

Transport contracts are named and checked:

- `TRAY_NVLS`: one node, MNNVL off, NVLS on;
- `RACK_NVLS`: multiple nodes in one physical rack, NVLS on, MNNVL automatic discovery;
- `RACK_MNNVL`: explicit single-rack legacy opt-in only;
- `MULTIRACK_HYBRID`: at least two racks, MNNVL off across racks, NVLS on, four IB rails.

The five collective workload uses all-reduce, all-gather, reduce-scatter, all-to-all, and
broadcast with the reviewed 8 KiB through 32 GiB / 500-iteration profile. A multi-rack selection
is one reviewed hybrid communicator, not one unsafe MNNVL domain spanning physical racks.

### 9. NeMo 56B launch hardening

NeMo launch changes preserve full NVLink/NVLS behavior within a single rack while using the
reviewed cross-rack transport contract for multi-rack runs. The launcher:

- receives exact campaign nodes and a compute-visible shared runtime tree;
- stages/verifies the image in the allocation, with four healthy GPUs required per node;
- runs an allocation gate before training and fails closed on reset-required or missing GPUs;
- uses the configured non-root Slurm submit UID/GID/home and shared cmods log permissions;
- keeps MNNVL inside each rack, four-rail IB between racks, and disables only unsafe cross-rack
  NVLS multicast where rack clique IDs are inconsistent;
- exports an explicit 30-step target and extends wrapper timeout so a healthy job is not reported
  failed while it continues unobserved;
- emits anchored machine-readable success/failure evidence from every terminal branch;
- keeps child Job IDs and stdout receipts when staging/training fails or is cancelled.

The new single-node `nemo-single-56b-30steps.sh` reuses the proven four-GPU memory recipe and
shared-log trace mount, with a terminal marker and fail-closed exit behavior.

### 10. MNUberGEMM launch hardening

MNUberGEMM targets are grouped per physical rack. CLI callers have their rack groups derived and
validated; the union must exactly equal the requested node set. Campaigns fan out as independent
rack jobs so a single slurmd/prolog failure does not hold every rack. The dispatcher rechecks the
pinned image even when it was pre-staged, prepares allocation-scoped IMEX channel 0 without
persistent service edits, applies a bounded 30-minute burn/startup/teardown envelope, and records
each rack's Job ID/output.

The batch and parser now distinguish warmup/progress from success as described in the parser
section. This prevents all-zero or partially qualified runs from becoming false acceptance.

### 11. Llama 3.1 405B and single-node launch fixes

The Llama preset enforces exactly 256 nodes / 1,024 GPUs, pins the source and staged image, hashes
the exact node set, exports the reviewed MNNVL/NVLS/userbuffer contract, and performs node
preflight without stopping/starting DCGM services. The wrapper initializes its own pinned Slurm
environment; the backend no longer prepends a `module load` command that fails in non-login
workers. Single-rack compatibility paths no longer depend on the process working directory or
legacy RAID/log paths.

### 12. Container staging, IMEX, provenance, and permissions

Container staging executes as an exact-node Slurm allocation so each target verifies its own
image, GPU visibility, NCCL/CUDA versions, and IB devices. Every expected node must emit positive
markers. The new `validation_imex_channel0_prepare.sbatch` performs bounded allocation-scoped
channel-device recovery and deliberately avoids persistent `systemctl`/udev changes.

Validation provenance hashes are updated for every invoked live script. Profiles now include the
actual staging, sysinfo, and IMEX helpers they transitively invoke. The backend refreshes the
provenance manifest by content when the bind-mounted file changes, avoiding stale `.pyc` reuse;
an invalid changed manifest fails before scheduler submission.

Log directories are refused under login homes, created/fixed only at the exact requested path,
and verified group-writable/setgid for the Slurm submit group. The installer must pre-create the
shared log and PythonCM job directories with the documented owner/group/mode rather than broadly
chmod parent trees.

### 13. PythonCM execution reliability and no-replay behavior

The PythonCM scheduler adds a non-mutating execution-capability probe. NVLink preflight can select
the reviewed exact-node Slurm fallback **before** submission if PythonCM execution is unavailable.
Once a submit may have been accepted, failures are never automatically replayed through another
provider. Parent activity phase is reconciled from child tasks, and external activity metadata is
re-read without rewriting non-external records.

The scheduler inventory cache now keeps callable objects rather than reusable numeric `id()`
values and performs invalidation under the same lock as cache reads. This removes a race that
could replay an old eligible state after a query-source swap.

### 14. Bring-up integration and CPU containment

The browser continues to refresh Validation Status every 30 seconds, but the expensive bring-up
fleet operation is decoupled from that cadence:

- `RACK_BRINGUP_HOME=/cluster/bringup` is mounted read-only;
- `RACK_BRINGUP_SCAN_JOBS=16` overrides `verdict-scan`'s default `nproc` behavior, avoiding a
  128-worker burst on this app server;
- `RACK_BRINGUP_FLEET_CACHE_TTL_SEC=300` caches the result independently of normal status caches;
- cold concurrent callers single-flight behind one build;
- a stale value is returned immediately while one daemon refresh runs;
- decoration-only pseudo-node CSV rows are discarded;
- a missing optional bring-up tree produces an explicit empty state instead of a failed UI action.

This is a significant CPU-spike mitigation. It does not eliminate the need to measure the actual
collector runtime and CPU at 4,032 nodes. A deployment test should open multiple auto-refreshing
browsers and prove that only one bounded scan occurs per five-minute TTL.

### 15. Post-run reports, findings, and exports

Reports explicitly exclude non-compute Slurm hosts from validation counts and preserve the
excluded names/count in JSON and CSV metadata. Superseded indexed failures remain visible in
history but do not inflate current repeat-failure flags/trends. Deep-audit and node issue data are
combined with validation events; exact node and rack filters remain available. Comment deletion
now checks HTTP errors rather than silently reporting success.

### 16. Observability UI and metrics fixes

- Native metric requests now use the actual host-network VictoriaMetrics listener
  `http://127.0.0.1:8428`, replacing a Docker Desktop hostname/historical port that returned 502.
- Metrics discover racks with `count by (su,rack)` and display stable SU plus warehouse/expansion
  hall rack labels.
- Logs and Infrastructure accept both flat and sectioned Grafana maps, resolve relative dashboard
  URLs against the browser-facing Grafana origin, expose Open in Grafana links, and honor the
  backend's embedding-allowed health result.
- `backend/grafana_embed_map.json` provides the Nscale panel map. Its UIDs, public origin, and
  embedding configuration must be verified against the deployment's Grafana instance.
- Events pagination avoids rendering thousands of current-state rows at once.

No browser refresh path introduced by these UI changes invokes `journalctl`.

### 17. Tooling, streaming, and UX resilience

Tool downloads/installers now use authenticated `apiFetch`. LLMB status polling validates JSON,
surfaces server errors, obeys a server-advertised deadline bounded between one minute and 48 hours,
and tells the user when the server job may still be running. Live-log viewers have a capacity gate
and release their slot on completion, preventing viewer threads from starving normal API work.
Formatting preserves launcher Job IDs and error tails. Settings/help text adds the new exact-node
campaign configuration and corrected preset descriptions.

## Security review

### Positive controls

- Explicit node-name regex and 512-node limit on the new Slurm-status endpoint.
- Exact inventory/campaign targets; full-hostname sites fail closed when inventory is absent.
- Stale campaign IDs and mismatched rack attribution are rejected.
- Source image size/SHA and invoked-script provenance are pinned.
- Submit dry-run/probe occurs before real submission; ambiguous submits are not replayed.
- Shared log directories are narrowly permissioned and home paths are refused.
- Google token and SSH/BCM material are mounted as read-only files; no literal secret was added to
  the audited `rack-config.env` or Compose diff.
- NVSwitch/BMC secrets are accepted only from protected files and the distributable Git history is
  regenerated from the sanitized tree.
- Unattended script deployment requires a non-root user and pinned SSH host keys.
- CSV output guards spreadsheet formula injection.
- External evidence is transactional and idempotent.

### Open security/operations risks

1. Verify the R1 direct-Docker and R2 no-journal implementations are the bytes
   installed before enabling their timers.
2. Confirm the release archive excludes `.env`, token/key files, `.cm`, runtime SQLite databases,
   logs, generated rollback data, and browser authentication artifacts.
3. The Compose file contains a site-specific spreadsheet identifier and a default token path under
   `/home/hlakshminara`; parameterize these in a portable deployment.
4. `RACK_SSH_USER=cmods` requires a scoped key, known_hosts pinning, and narrowly reviewed remote
   commands. Do not replace this with unrestricted head-node sudo.
5. Node-local diagnostics that use `sudo -n`, `systemctl`, or journal/dmesg must remain explicit
   operator actions or allocation jobs, never hidden refresh-time operations.
6. API authorization must continue to wrap new scheduler, validation, export, and tool routes.
   The route-level application guard should be verified with unauthenticated integration tests.
7. Generated Grafana URLs must remain same-site/allowlisted to avoid turning configuration into an
   arbitrary iframe/open redirect surface.
8. Rotate the three affected non-factory NVSwitch/BMC credentials before enabling those tools;
   sanitizing the artifact does not revoke credentials previously present in repository history.

## Test and evidence coverage

The changed test suite covers the following high-risk contracts:

- Slurm node order/state/reason and idle-star/nonresponding admission;
- exact campaign node propagation, stale/ineligible rack rejection, and single-rack semantics;
- container stage node-by-node markers, digest/runtime checks, allocation GPU recovery, cancelled
  root jobs, and no-submit dry runs;
- NeMo single/multi-rack transport and 30-step terminal evidence;
- MNUberGEMM positive work, topology rank counts, full qualification, hard failures, and expected
  wall-time behavior;
- Llama image/path/preflight contracts;
- exact log/rack attribution and conflict abstention;
- DCGM checkpoint idempotence, atomic rollback, retry history, and newer-failure authority;
- OVA health-check current evidence and filters;
- post-run compact responses and superseded-history handling;
- bring-up bounded concurrency, independent TTL, single-flight, stale-while-refresh, and missing
  optional integration;
- PythonCM probe, fallback-before-submit, no replay, and inventory-cache concurrency;
- Validation Status normalization, required missing evidence, domain selection, SU ordering, ETA
  abstention, formula-safe export, PDF/JPEG format, and floor-map accessibility;
- rack/SU metrics labeling, Slurm-over-BCM node status, installer/unknown handling, logs mapping,
  event pagination, and frontend launch payloads.

### Required final release gates

Run these on the final Git-ready tree and retain their complete logs/checksums with the artifacts:

1. Full backend pytest: zero failures.
2. Full frontend unit suite: zero failures.
3. TypeScript type-check and production Vite build.
4. ESLint (errors must be zero; warnings explicitly recorded).
5. `git diff --check`, `bash -n` for every changed shell script, and ShellCheck where available.
6. Validation provenance verification against the exact packaged script bytes.
7. Secret scan of tracked and packaged content.
8. Tar extraction into an empty directory followed by install/deploy dry run.
9. Git bundle clone plus `git fsck` and a clean `git status`.
10. Authenticated and unauthenticated API smoke tests.
11. Production-scale 4,032-node map and export test.
12. Exact-node dry runs for NCCL, NeMo, MNUberGEMM, and Llama with **no** `sbatch` in dry-run mode.
13. AIC/noise test: browser auto-refresh for at least two fleet-cache TTLs while confirming no
    sudo security mail and no unexpected app/head-node journal calls.
14. CPU test with multiple simultaneous clients: one bring-up scan at a time, at most 16 workers.
15. Deploy drift/build-info/image/Compose verification using the repository deployment scripts.

## Deployment and rollback concerns

- This is a backend, frontend, script, configuration, and Compose release. It requires a rebuilt
  image/container, not just an SPA copy.
- Preserve the `rack-status_rack-data` volume because it contains durable activities, history, and
  ingestion checkpoints. Back it up before migration and verify checkpoint schema after startup.
- Build SPA assets as one coherent content-hashed set. Do not pair a new index with old assets.
- Synchronize `bin/scripts` to the compute-visible validation script root and verify byte-for-byte
  checksums before allowing launches.
- Pre-stage the pinned large container images outside an interactive web request, then validate
  size/SHA/runtime on target allocations.
- Verify `/cluster/bringup` and `/cluster/OVA/healthchecks/logs` are read-only in the container;
  validation logs remain the only intended read/write cluster mount.
- Confirm cmods UID/GID/home and setgid log ownership on both app server and Slurm side.
- Keep a previous image tag, Compose inspect, source commit/bundle, and persistent-volume archive.
- A rollback must account for the additive results-history checkpoint schema. Prefer rolling the
  application image back while preserving a tested compatible volume copy rather than deleting
  current evidence.

## Complete changed-file inventory

This inventory is part of the audit so no enhancement disappears behind a high-level summary.

### Backend implementation

`backend/api/slurm_queries.py`, `backend/app.py`, `backend/bringup.py`,
`backend/cluster_report.py`, `backend/config_env.py`, `backend/external_validation_evidence.py`,
`backend/healthcheck_evidence.py` (new), `backend/native_status.py`, `backend/node_issues.py`,
`backend/nvlink_preflight.py`, `backend/post_run_report.py`, `backend/results_history.py`,
`backend/scheduler_runtime.py`, `backend/schedulers/pythoncm.py`, `backend/slurm_query.py`,
`backend/universal_parser.py`, `backend/validation_activities.py`,
`backend/validation_presets.py`, and `backend/validation_provenance.py`.

### Backend tests

`backend/tests/test_app_api.py`, `test_bringup.py`, `test_cluster_report.py`,
`test_dcgm_validation_evidence.py` (new), `test_external_validation_evidence.py`,
`test_five_collective_wrapper_parser.py` (new), `test_healthcheck_evidence.py` (new),
`test_lake_capture.py`, `test_llmb_nccl_container.py`, `test_mnubergemm_parser_semantics.py` (new),
`test_multirack_campaign.py`, `test_multirack_nemo_allocation_preflight.py` (new),
`test_multirack_nemo_target.py`, `test_native_status_summary.py`,
`test_nemo_validation_evidence.py` (new), `test_node_issues.py`, `test_node_report_job.py`,
`test_nvlink_preflight.py`, `test_nvlink_preflight_api.py`,
`test_parser_export_attribution.py` (new), `test_pick_log_full_rack.py`, `test_post_run_api.py`,
`test_post_run_report.py`, `test_pythoncm_hybrid.py`, `test_pythoncm_scheduler.py`,
`test_results_history_completion.py`, `test_scheduler_runtime.py`,
`test_single_node_llama_scripts.py` (new), `test_slurm_command_routing.py`,
`test_validation_activities.py`, `test_validation_log_dir_permissions.py` (new), and
`test_validation_presets.py`.

### Validation and operations scripts

`bin/rack-validation-evidence-refresh.sh`, `bin/scripts/ingest_16k_validation_evidence.py`,
`llama405b_fp8_1024.sbatch`, `llama405b_node_preflight.sh`, `mnubergemm.sbatch`,
`nemo-llma3_8b.sh`, `nemo-nemotron5h_56b-mbridge-fp8.sh`,
`nemo-single-56b-30steps.sh` (new), `nvftp_nccl_five_collective_20260824.sbatch`,
`nvis_logscan.py`, `parser.sh`, `run-llama.sh`, `run-llmb-nccl-container.sh`,
`run-nemo-xrack.sh`, `run-validation-preset.sh`, `run_llmb_nccl.sh`,
`run_mnubergemm_stress.sh`, `run_nccl_mpi.sh`, `run_singlerack.sh`, `sysinfo.sh`,
`validation_container_stage.sbatch`, and `validation_imex_channel0_prepare.sbatch` (new).

### Frontend implementation

`frontend/src/App.tsx`, `components/ParserResultsDialog.tsx`, `components/RackTable.tsx`,
`components/Sidebar.tsx`, `components/SqueuePanel.tsx`, `components/WorkerStatus.tsx`,
`components/analytics/ValidationFloorMap.tsx` (new),
`components/observability/MetricChartGrid.tsx`, `components/rack/CommandStreamDialog.tsx`,
`components/rack/NodeSelector.tsx`, `components/rack/NvlinkPreflightPanel.tsx`,
`components/rack/ValidationPresetDialog.tsx`, `components/rack/ValidationTab.tsx`,
`content/registry/analytics.ts`, `lib/formatJobOutput.ts`, `lib/metricsRack.ts`,
`lib/rackCommandBodies.ts`, `lib/rackTopology.ts`, `lib/validationExport.ts` (new),
`lib/validationPresets.ts`, `lib/validationStatus.ts` (new), `routes.ts`,
`services/nodeIssuesService.ts`, `services/schedulerService.ts` (new),
`services/validationActivitiesService.ts`, `types/api.ts`, `views/AnalyticsView.tsx`,
`views/ClusterDashboardView.tsx`, `views/EventsView.tsx`, `views/InfrastructureView.tsx`,
`views/LogsView.tsx`, `views/MetricsView.tsx`, `views/MultirackView.tsx`,
`views/PostRunNodeReportView.tsx`, `views/RackDetailView.tsx`, `views/SettingsView.tsx`,
`views/SingleRackView.tsx`, and `views/ToolsView.tsx`.

### Frontend tests

`frontend/src/components/analytics/ValidationFloorMap.test.tsx` (new),
`components/rack/NodeSelector.test.tsx` (new),
`components/rack/ValidationPresetDialog.test.tsx`, `components/rack/ValidationTab.test.tsx`,
`lib/formatJobOutput.test.ts`, `lib/metricsRack.test.ts`, `lib/rackCommandBodies.test.ts`,
`lib/rackTopology.test.ts`, `lib/validationExport.test.ts` (new),
`lib/validationStatus.test.ts` (new), `views/EventsView.test.tsx` (new),
`views/LogsView.test.tsx` (new), `views/MultirackView.test.tsx`, and
`views/PostRunNodeReportView.test.tsx`.

### Configuration and documentation

`backend/grafana_embed_map.json` (new), `docker-compose.yml`, `help_content.json`,
`rack-config.env`, and `docs/VALIDATION_STATUS_IMPLEMENTATION_2026-09-02.md` (new).

## Release acceptance statement

The audited implementation addresses the requested Validation Status, Slurm reason, job-launch,
evidence-integrity, and CPU-containment enhancements with strong fail-closed tests. Source-level
findings R1-R3 are resolved and the final automated matrix is green; the exact results must remain
attached to the exact commit/bundle. Deployment finding D1 must be closed on the installed host.
Most importantly, the 30-second rack refresh and
the separate app-server service audits must not be conflated: the refresh should consume cached
API evidence; it must not invoke sudo, Docker inspection, or journal queries on either the app
server or the head node.
