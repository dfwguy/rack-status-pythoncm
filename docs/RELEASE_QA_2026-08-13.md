# Rack Status v3 release QA — 2026-08-13

## Release decision

**READY FOR INTERNAL GITLAB CHECK-IN.** The application suites, production SPA/API surface, real Slurm audit cycle, deployment verifier, and live telemetry ownership cutover all pass. The external telemetry ownership files are captured in a separate checksummed handoff package because `/opt/observability/nscaletx` is not itself a Git worktree.

The remaining handoff action is administrative: import the Rack Status branch/tag and the telemetry package into their authoritative internal GitLab/configuration repositories. No push is claimed in this report; DNS for `gitlab-master.nvidia.com` was unavailable from the preparation host.

PythonCM and Clush are explicitly parked by product direction and are not release gates for this candidate.

## Scope and safety

QA covered the source tree at `/opt/observability/nvis-rack-status-v3`, the running Rack Status service on port 5056, its live APIs and Chromium SPA, the current VictoriaMetrics data on port 8429, and the telemetry-generation source under `/opt/observability/nscaletx`.

The integration phase deliberately rebuilt and recreated Rack Status, synchronized reviewed head-node scripts, launched bounded audit validation jobs, restarted central Alloy, and stopped the four redundant SU5 central-pull shards. It did not delete WAL/TSDB data or mutate SU6/SU7 telemetry ownership. Exact backups and rollback commands are recorded below. The successful final audit did not drain its target.

Status meanings:

- **PASS** — implementation, automated coverage, and available live evidence agree.
- **PARTIAL** — implemented and testable, but a live operational gate or artifact is incomplete.
- **FAIL** — a reproducible defect or release-blocking mismatch exists.
- **PARKED** — intentionally deferred and excluded from this release decision.

## Feature-by-feature matrix

| # | Enhancement / requirement | Status | Where to see it | QA evidence / remaining work |
|---:|---|---|---|---|
| 1 | Node-level Rack Status fix and completed-test persistence | PASS | `/compute/racks` and any rack detail | Live `/api/status` was fresh, all three workers had completed at least one collection, and only genuine current pending/ongoing rows remained. The former all-racks-pending regression was not reproduced. |
| 2 | PythonCM conversion | PARKED | N/A | Explicitly deferred. Current Slurm access still uses the configured 25.05 binary path plus `SLURM_CONF`. |
| 3 | Clush support | PARKED | N/A | Explicitly deferred. |
| 4 | CUDA 13.3+ and NCCL 2.31.2 validation | PASS | Validation preset dialog, preset `nccl-2.31.2` | The live preset is offered for node/rack/multirack; version and preflight tests pass. |
| 5 | Exact Llama 3.1 405B validation | PASS | Validation preset dialog, preset `llama31-405b-fp8` | Source enforces the intended 256-node/1,024-GPU/50-iteration recipe and its approved container/NCCL contract rather than accepting arbitrary Llama-like runs. |
| 6 | GPT-OSS CPU microbenchmark validation | PASS | Validation preset dialog, preset `gpt-oss-cpu` | Live preset is present for node/rack/multirack and unit coverage passes. |
| 7 | DGXC Exemplar `system_info` validation | PASS | Validation preset dialog, preset `exemplar-system-info` | The v26.06.01 system-info contract is present, runnable at node/rack/multirack scope, and covered by tests. |
| 8 | Local and NFS container sources | PASS | Preset preparation dialog | Backend and shell contracts support both source modes. Validation checks path, size and digest where available. |
| 9 | Rsync staging to avoid cold starts | PASS | Preset preparation dialog | Staging to `/tmp/containers` is implemented; device preflight includes `/dev/infiniband`. Tests cover stage/preflight failure paths. |
| 10 | NeMo and MNUberGEMM containers | PASS with version note | Presets `nemo-26.04.01` and `mnubergemm-26.03-v1.17` | NeMo is 26.04.01. The actual available MNUberGEMM image is 26.03-v1.17; the earlier text saying 26.04.1 should not be advertised as an image that does not exist. |
| 11 | Multi-node per rack and multi-rack selection | PASS | `/compute/multirack` and validation dialog | Backend composition, selection, and frontend multirack tests pass. Live presets correctly advertise allowed scopes. |
| 12 | Repeatable deep hardware audit | PASS | Rack detail → Hardware → Deep Audit | Workflow and deep-audit APIs are registered. Completed evidence persists with full output, node verdicts, and scheduler reference. Observed d01 hardware runs include jobs 147895 and 147894. |
| 13 | Repeatable forced-NVLink/network audit | PASS | Rack detail → Networking → Deep Audit | Full SU7 QA evidence is present: run `4d45a689-...` tested all 18 j21 nodes; all passed 4 GPUs, 72 NVLinks, 4 active IB ports, zero bad counters and forced nvbandwidth data path rc=0. |
| 14 | Results button beside Start Tests | PASS | Hardware/Networking Deep Audit cards | Frontend component/tests show Results adjacent to launch. API state exposes full output and job ID immediately while a run is active or after completion. |
| 15 | Hardware/network status reports in analytics/reports | PASS | Rack detail audit history; `/analytics/reports`; `/analytics/post-run-nodes` | Audit state and full results are persisted. Generated-report archive API is healthy, though it currently contains zero saved generated reports. |
| 16 | Post-run node punchcard, participation/failure trend, flag after repeated failures | PASS | `/analytics/post-run-nodes` | SPA/API work without the Google sheet. Live h03-11 example: 17 participations, 4 attributed failures, flagged, current drain state/reason, and job history. Failure attribution requires node-specific evidence rather than assigning every collective victim. |
| 17 | Click-through issue explanation and launch-on-trend workflow | PASS | `/analytics/post-run-nodes`, then rack audit cards | Node job IDs, categorized evidence, trend/flag state and current Slurm reason are exposed; audit launch remains a separate deliberate action. |
| 18 | Current Slurm comment/markdown and user marking/draining | PASS | Post-run node details and mark action | Current Slurm state/reason is displayed. Mark/drain API is tested, records the issue, and applies a structured reason. A false intermediate GPU-cgroup QA finding was immediately reversed and invalidated; the final GPU-aware audit passed without a drain. |
| 19 | Metrics rack monikers (`d01`, `e01`, `f01`, `g01`, `h01`) | PASS at UI normalization layer | `/observability/metrics` | Unit tests pass for WH→short-moniker conversion and live source labels include d/e/f/g/h families. Both direct and legacy label forms normalize to the expected short rack name. |
| 20 | SU1–SU5 exporter recovery and live metrics | PASS | `/observability/metrics` and `/api/metrics/health` | All four fresh metric families agree per SU. Direct on-node Alloy owns SU1–SU5; the 60-second-fresh duplicate DCGM and node-exporter queries are empty. See telemetry section. |
| 21 | Generated report archive | PASS | `/analytics/reports` | List/download/delete route contracts, retention (`max_keep=40`) and atomic payload/metadata writes pass. Live archive was empty at capture time, so no live download artifact was available to inspect. |
| 22 | Durable history/state across service recreation | PASS | All live views above | Reports, node issues, post-run history and workflow run files live below `/app/var`; `/app/var` is the named `rack-data` volume. Deployment verification found all durable routes mounted. |
| 23 | Backend/API/frontend integrity | PASS | Entire application | 1,202 backend tests passed; all 309 frontend tests passed; production build, 9/9 deployment checks, and the complete 23-route Chromium sweep passed. |
| 24 | Source/live deployment permanence | PASS | Deployment drift report and handoff artifacts | Reviewed head scripts exactly match source; only five intentional production-only archives remain. The external telemetry ownership sources and hashes are captured in a separate deterministic package for import into the authoritative configuration repository. |
| 25 | Docs-to-feature coverage | PASS | Help → Observability/Metrics | Metrics help now describes live VictoriaMetrics, rack/time filtering, short rack monikers, and truthful no-data behavior; the generated help payload was refreshed and tested. |
| 26 | Google punchcard import | PASS as optional baseline only | Node-issue baseline sync API | The product post-run view does not depend on the sheet. Sheet import remains optional seed/baseline input, matching the requirement to build a native view rather than show an imported punchcard. |

## Deterministic automated QA

All commands were run from `/opt/observability/nvis-rack-status-v3` unless noted.

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest -q backend/tests --disable-warnings` | **PASS** — 1,202 passed, 5 skipped in 330.11 s |
| `.venv/bin/ruff check backend --exclude backend/tests --output-format concise` | **PASS** |
| Full frontend Vitest suite, deterministic rerun | **PASS** — 47 files, 309 tests |
| Frontend typecheck and production Vite build | **PASS** — non-blocking warning for chunks over 500 kB |
| `bin/verify-deploy.sh` | **PASS** — 9/9 checks |
| `git diff --check` | **PASS** at capture time |
| `bash -n` for validation/deep-audit/deployment scripts | **PASS** |
| Python import/static checks and OpenAPI inspection | **PASS** — live OpenAPI returned 193 paths |
| Authenticated Chromium sweep | **PASS** — all 23 documented hash routes; no page error, visible API error, or API 5xx |

The first frontend full-suite attempt had one ReportsView archive timing failure; that test passed in isolation and the complete deterministic rerun passed 309/309. It is classified as a non-reproduced suite timing event. Non-fatal React `act()` warnings remain in tests for `useBringupNode`, `ValidationPresetDialog`, and `NodesView`.

The production build emits a bundle-size warning for the main JS chunk. This is a performance follow-up, not a correctness gate.

## Live API and persistence evidence

- `/api/openapi.json`: HTTP 200, 193 paths.
- `/api/validation-presets`: HTTP 200, six expected presets.
- `/api/workflows` and workflow detail: HTTP 200; hardware and network workflows each have two valid steps.
- `/api/deep-audits?limit=10`: HTTP 200. A completed d01 run retained per-node JSON, full output and scheduler reference 147895. A full j21 network run retained all 18 pass verdicts.
- `/api/node-issues?rack=h03`: HTTP 200 with current node rollups.
- `/api/post-run-node-report?node=ink50-dh01-h03-11`: HTTP 200 with 17 participations, four attributed failures, flag state, live Slurm state/reason and prior jobs.
- `/api/reports/generated`: HTTP 200 with `max_keep=40`; zero current archives.
- `/api/status`: HTTP 200 and fresh. At capture, workers had all completed at least once and had no errors. Only d01/d03 were genuinely ongoing and f19/g01 genuinely pending at one stage.
- `/api/metrics/health`: HTTP 200 and the VictoriaMetrics upstream was healthy.

Persistence paths:

| Data | Durable path |
|---|---|
| Generated reports | `/app/var/reports` |
| Node issues/marks | `/app/var/cache/node_issues.json` |
| Post-run history | `/app/var/history/post_run_nodes.json` |
| Workflow/deep-audit runs | `/app/var/workflows/runs` |

Writes use bounded retention and atomic replacement/locking where required. The running service mounts `/app/var` from named volume `rack-data`.

## Slurm routing QA

The configured platform command prepends the canonical environment and uses explicit binaries:

```text
export SLURM_CONF=/cm/shared/apps/slurm/var/etc/slurm/slurm.conf
/cm/local/apps/slurm/25.05/bin/{srun,sinfo,sacct,scontrol}
```

Each configured binary reported Slurm 25.05.8. A read-only probe without `SLURM_CONF` failed controller/config discovery, while the configured route succeeded. This confirms the explicit route is required and is not cosmetic. The workflow engine composes commands through the platform `srun_cmd`; real browser-launched audits produced the expected Slurm job IDs and GPU allocations.

## SU1–SU5 telemetry acceptance

Captured from VictoriaMetrics at approximately 2026-08-13 19:50 UTC. Counts are distinct node identities, not raw series:

| SU | node-exporter | cgroup-exporter | DCGM | Alloy | Current interpretation |
|---|---:|---:|---:|---:|---|
| SU1 | 282 | 282 | 282 | 282 | Healthy direct ownership |
| SU2 | 254 | 254 | 254 | 254 | Healthy direct ownership |
| SU3 | 222 | 222 | 222 | 222 | Healthy direct ownership |
| SU4 | 288 | 288 | 288 | 288 | Healthy direct ownership |
| SU5 | 166 | 166 | 166 | 166 | Healthy direct ownership for all currently reachable/reporting nodes |

Changing counts such as SU1 281→282 or earlier lower SU2 values track nodes returning from BCM maintenance/reboot. They are changes in current reachability, not reappearance of the cloned-identity defect: all four metric families move together under the correct SU/node identity.

Before cutover, SU4 g01–g03 and the reporting SU5 nodes had duplicate central-pull and direct-push paths. After cutover:

- the 60-second-fresh `count by(node,gpu)` duplicate DCGM query returns `[]`;
- the 60-second-fresh `count by(node,su)` duplicate node-exporter query returns `[]`;
- all four metric families have the same fresh distinct-node count per SU;
- the four former SU5 central shards report `running=false` and restart policy `no`;
- SU5 on-node Alloy continues to write directly to `http://10.40.112.3:8429/api/v1/write`;
- historical duplicate samples remain queryable at their original timestamps, as expected.

### Exact root cause

1. Central Alloy globbed every file under `/etc/alloy/dcgm-sd/*.json`, including `su4-g01-g03.json`, while on-node Alloy already owned SU4.
2. Four restart-always Prometheus containers (`nvis-prom-SU5-dgxnet17` through `20`) centrally scraped node/DCGM/cgroup for SU5 while on-node Alloy already owned SU5.
3. The shard generators had no ownership exclusion, so regenerating configuration could recreate the overlap.

### Permanent ownership fix packaged and live-validated

The following host-source files changed and are captured in the separate checksummed telemetry handoff package:

| Source file | SHA-256 after fix | Change |
|---|---|---|
| `platform/alloy/config.alloy` | `c3873063ca79cffac960bf307d11aeefa110c40b7ad6ffd2e7ef036a8db4908b` | Select only `su6.json` for central fallback; SU1–SU5 remain direct-push owned. |
| `platform/prometheus/gen-shards.sh` | `2490f17f94e51aca477e293d26d5d0e12c73cd77948258a14bed831cd51e86d4` | Default-exclude SU1–SU5; keep SU6/SU7 eligible for central fallback; add explicit `--direct-push-sus`; explicit empty string provides full legacy fallback. |
| `platform/prometheus/gen_shards.py` | `b103c315796765a75c31a0a87b2d8d9eb85747b02ee0e30286a4049dbae79f2e` | Same ownership and SU6/SU7 fallback contract as shell generator. |
| `platform/prometheus/validate-ingestion-ownership.py` | `cd556ec8e358bbfc36c996860a9dc46b7c082d22f79ba97b624ec11b3981f7cd` | New pre-deploy gate: fail if central SD overlaps a direct-push SU. |

Validation performed:

- `bash -n`, Python compilation and Alloy formatting: PASS.
- Shell and Python generators produced equivalent candidates containing only four SU7 shards from the current roster; 288 hosts / 1,152 GPUs; no SU1–SU5 central targets. This proves SU7 remains eligible for central fallback even though the cutover below does not deploy or restart SU7.
- Ownership validator: PASS, 1,152 entries across 13 target files (12 shard files plus SU6 fallback).
- Negative control with `DIRECT_PUSH_SUS=''`: correctly FAIL, detecting 864 overlap target entries.
- SU6 fallback file and all existing Prometheus TSDB directories remain untouched.

Because `/opt/observability/nscaletx` has no Git metadata, the exact files, hashes, acceptance evidence, and rollback instructions were packaged as `/home/nvis/nscaletx-telemetry-ownership-20260813.tar.gz`. Import that package into the authoritative configuration repository during handoff.

## Live telemetry ownership cutover — executed and verified

The smallest cutover was executed from `/opt/observability/nscaletx/platform`. It preserved SU6 fallback, SU7 configuration, and every shard TSDB. Runtime/source evidence is at `/opt/observability/nscaletx/backups/telemetry-ownership/20260813T195725Z`.

### 1. Back up source/runtime evidence and render a non-live candidate

```bash
cutover_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
cutover_backup="/opt/observability/nscaletx/backups/telemetry-ownership/${cutover_stamp}"
install -d -m 0750 "$cutover_backup"
cp -a alloy/config.alloy prometheus/gen-shards.sh prometheus/gen_shards.py \
  prometheus/validate-ingestion-ownership.py prometheus/docker-compose.prom-shards.yml \
  prometheus/shards "$cutover_backup/"
docker inspect nvis-alloy nvis-prom-SU5-dgxnet17 nvis-prom-SU5-dgxnet18 \
  nvis-prom-SU5-dgxnet19 nvis-prom-SU5-dgxnet20 \
  > "$cutover_backup/docker-inspect.json"

candidate_dir="$(mktemp -d)"
prometheus/gen-shards.sh \
  --out "$candidate_dir/shards" \
  --compose "$candidate_dir/docker-compose.prom-shards.yml"
prometheus/validate-ingestion-ownership.py \
  --sd-dir "$candidate_dir/shards/sd" \
  --alloy-sd-dir alloy/dcgm-sd \
  --alloy-files su6.json
docker exec nvis-alloy alloy fmt /etc/alloy/config.alloy >/dev/null
```

Observed gate: generator reported no SU1–SU5 central shards; validator printed `PASS`; Alloy formatting exited zero.

### 2. Cut over collection ownership

```bash
docker update --restart=no \
  nvis-prom-SU5-dgxnet17 nvis-prom-SU5-dgxnet18 \
  nvis-prom-SU5-dgxnet19 nvis-prom-SU5-dgxnet20
docker stop \
  nvis-prom-SU5-dgxnet17 nvis-prom-SU5-dgxnet18 \
  nvis-prom-SU5-dgxnet19 nvis-prom-SU5-dgxnet20
docker restart nvis-alloy
```

Effects:

- SU5 legacy central pull stops; on-node Alloy remains the owner.
- SU5 direct delivery continues to `10.40.112.3:8429/api/v1/write`; current fresh direct series should not pause.
- Central Alloy reloads the source config and drops the g01–g03 pull targets.
- SU6 central fallback remains selected through `su6.json`.
- Prometheus shard TSDB paths below `/data/prometheus/SU5-*` are not removed or rewritten.
- SU7 containers/configuration are not changed by this cutover. The generator still emits SU7 central-fallback shards because only SU1–SU5 are excluded by default.

### 3. Verify after at least two scrape intervals

```bash
vm='http://127.0.0.1:8429/api/v1/query'
curl -fsS --get "$vm" --data-urlencode \
  'query=count by(su) (count by(node,su) (node_exporter_build_info{su=~"SU[1-5]"}))'
curl -fsS --get "$vm" --data-urlencode \
  'query=count by(su) (count by(node,su) (cgroup_exporter_build_info{su=~"SU[1-5]"}))'
curl -fsS --get "$vm" --data-urlencode \
  'query=count by(su) (count by(node,su) (alloy_build_info{su=~"SU[1-5]"}))'
curl -fsS --get "$vm" --data-urlencode \
  'query=count by(su) (count by(node,su) (DCGM_FI_DEV_GPU_TEMP{su=~"SU[1-5]"}))'
curl -fsS --get "$vm" --data-urlencode \
  'query=count by(node,gpu) (DCGM_FI_DEV_GPU_TEMP{su=~"SU[1-5]"} and timestamp(DCGM_FI_DEV_GPU_TEMP{su=~"SU[1-5]"}) > time()-60) > 1'
curl -fsS --get "$vm" --data-urlencode \
  'query=count by(node,su) (node_exporter_build_info{su=~"SU[1-5]"} and timestamp(node_exporter_build_info{su=~"SU[1-5]"}) > time()-60) > 1'
```

Observed result: both 60-second-fresh duplicate queries returned an empty result. Each family reported SU1=282, SU2=254, SU3=222, SU4=288 and SU5=166. Direct series remained fresh, the SU6 fallback definition remained selected, and the SU7 candidate remained valid. Historical duplicate samples remain queryable for their original timestamps, which is expected.

### Rollback

If direct SU5 metrics become stale, restore runtime ownership without deleting any TSDB:

```bash
docker update --restart=always \
  nvis-prom-SU5-dgxnet17 nvis-prom-SU5-dgxnet18 \
  nvis-prom-SU5-dgxnet19 nvis-prom-SU5-dgxnet20
docker start \
  nvis-prom-SU5-dgxnet17 nvis-prom-SU5-dgxnet18 \
  nvis-prom-SU5-dgxnet19 nvis-prom-SU5-dgxnet20
```

For SU4 central-pull rollback, restore the pre-change Alloy source from the authoritative backup/source (the target expression was `/etc/alloy/dcgm-sd/*.json`) and restart `nvis-alloy`. For an intentional full legacy-generator rollback, set `DIRECT_PUSH_SUS=''`; the negative-control gate will then report the overlap by design. Do not delete `/data/prometheus/SU5-*`.

## Production head-script drift

Initial QA found three stale live scripts. They were synchronized outside this read-only QA task, and the final rerun of `bin/prod-drift.sh --surface scripts --ref HEAD` found **zero changed files** across 111 compared files. Exact final hashes:

| Script | Source SHA-256 | Live SHA-256 | Final classification |
|---|---|---|---|
| `nccl_campaign.sh` | `d1bc3c03fd95286c5f0aac6b3fe792fe7f93071df982692d299e69dbe2642480` | `d1bc3c03fd95286c5f0aac6b3fe792fe7f93071df982692d299e69dbe2642480` | **Match — resolved** |
| `run-llama.sh` | `b9065d3fb9c008a2658827b6d645937c4eb6eafacaebad41066d126828db9456` | `b9065d3fb9c008a2658827b6d645937c4eb6eafacaebad41066d126828db9456` | **Match — resolved** |
| `slurm-init.inc.sh` | `0a0e1b5b33882ae3d14b219e4d14caeefaf88bd7e993c848b018fd965603a698` | `0a0e1b5b33882ae3d14b219e4d14caeefaf88bd7e993c848b018fd965603a698` | **Match — resolved** |

Five production-only files remain. None should be harvested as a new source implementation:

| Production-only file | SHA-256 | Classification |
|---|---|---|
| `llama405b_fp8_1024.runtime-hotfix.sbatch` | `5d1c835eedc6bf995ec5e4c9e57dfe34e604bf5df99d11b02a5cb34ecb2d8bb3` | Redundant archive; byte-identical to the canonical current file |
| `run-nemo-xrack.sh.pre-filename-fix-20260810T0520Z` | `a6a98cf5608210cf4ef7ff60e36fb66af5f472a8a4219167eb1ba02f7f085117` | Intentional prechange evidence |
| `run-nemo-xrack.sh.pre-gpu-recovery-check-20260810T053556Z` | `4fdf2030086868ecde243483656e20362be5079581e8cd1104b94206c47a6fa2` | Intentional prechange evidence |
| `run-nemo-xrack.sh.pre-local-cache-20260810T0508Z` | `cc440114307fe9f3430dd2e8de24a4f3c563754ac4a57a82ce9a86c74219c129` | Intentional prechange evidence |
| `run_llmb_nccl.sh.12082026` | `64a6addacacec2d7c88181319e42f373f0cb07357f4022c828284f0f2ca44242` | Intentional backup/evidence |

New canonical validation/deep-audit scripts also SHA-match their deployed copies. There is no remaining changed-live-script release gate.

## Documentation and operational follow-ups

1. Import the packaged `/opt/observability/nscaletx` ownership changes into authoritative version/configuration control, including the validator as a mandatory pre-deploy step.
2. Make the site compose override's non-secret bind-mount contract reproducible. Keep credentials out of Git.
3. Resolve the frontend main-chunk warning when convenient; this is not blocking functional release.
4. Clean up React `act()` warnings so future async regressions are easier to distinguish from noise.

## Final browser and real-job evidence

- The authenticated Chromium sweep opened all 23 documented SPA hash routes. No route showed the error boundary, visible `API error`, or an API response at 500 or above.
- The sweep exposed and drove the fix for a real Operations defect: remote workspace `PurePosixPath` values were being inspected as local paths, and a shared-NFS symlink directory was rejected. The final implementation uses the execution-host transport for one bounded directory stat and remote image readability checks; four focused regressions were added.
- The final browser-launched hardware audit on `ink50-dh01-d01-32` is workflow `6c66e4ee-92ee-4106-9b72-d1162baa53c7`, Slurm job `147895`: `COMPLETED`, exit `0:0`, one node, four GB300 GPUs, four GPUs detected, temperatures 25/27/26/25 °C, zero Xids, zero fatal PCIe events, no warnings and no drain. Final node state was `idle`, reason `none`.
- SU7 hardware and forced-NVLink/network audits produced Slurm jobs `147865` and `147866`, both with four GPU resources. The network audit observed four GPUs, 72 active NVLinks, four of four active IB ports, zero bad counters and forced data-path rc=0.
- The known failed-launch artifacts (`execve(): 0`) were traced to spaced optional `srun` values; production now emits `--kill-on-bad-exit=0` and `--immediate=60`. Invalid QA artifacts were marked invalid and are excluded from current report rollups.

## Final release gates

- [x] Execute and verify telemetry ownership cutover; zero current SU1–SU5 duplicate identities.
- [x] Confirm SU1–SU5 distinct/fresh counts agree across all four metric families after cutover.
- [x] Package the four telemetry ownership source files, hashes, acceptance evidence and rollback instructions.
- [x] Head scripts synchronized: zero changed files in the final drift comparison; only intentional production-only evidence remains.
- [x] Correct Metrics help content and regenerate the shipped help payload.
- [x] Rerun backend tests, deterministic frontend suite/build, `bin/verify-deploy.sh`, live API smoke, authenticated browser sweep and `git diff --check`.
- [ ] Push the reviewed branch/tag and import the telemetry package when internal GitLab DNS is available.
