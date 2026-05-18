# NVIS Rack Status — Release Notes

**Version:** `v1.0.0` (suggested)  
**Branch:** `main` @ `5b6765c`  
**Since:** `08c0077` — *Merge `feature/integrate-macswap-bmc-recovery` into `feature/react-frontend-nvis-ux`* (2026-05-13)  
**Through:** `5b6765c` — *Merge `feature/firmus-ct-naming-convention` into `main`* (2026-05-18)

---

## Summary

This release period brings the **React 19 dashboard** to `main`, **hardware operations** (BMC recovery, MAC swap), a **workflow designer and runner**, **multi-operator rack locks**, **CI/Docker** updates, and **NVIS hostname templates** for Bright and Firmus/CBR clusters.

---

## Features (chronological)

### 1. Hardware — BMC recovery & MAC swap (`5da3923`, `08c0077`)

- **BMC Recovery** on the rack Hardware tab: per-node fan-out via `bmc-recovery.sh` (RF0), async jobs with status polling.
- **MAC Change** (single node): dialog with device MAC and up to three interface rows; runs vendored `macswap` via `module load python3` on BCM head nodes.
- APIs: `POST /api/bmc-recovery-async`, `POST /api/mac-change-async`, shared reset-job polling.
- Settings env: `RACK_BMC_RECOVERY_SCRIPT`, `RACK_MACSWAP_SCRIPT`, timeouts, dry-run, `RACK_MACSWAP_PYTHON_MODULE`.

### 2. Workflow engine — editor & execution (`cd5e30c`, `4668c3d`, `e5ce781`)

- **Visual workflow editor**: KUI node properties, YAML frontmatter, predefined scripts (`GET /api/tools/available`), Mermaid validation, delete confirmation, version bump on save.
- **Step types**: automated (script/AI), manual, decision, **delay** (sleep for testing).
- **Execution**: per-run dirs under `var/workflows/runs/<run_id>/`, step `.log` output, `GET /api/workflow-runs/<id>/output/<step>`.
- **Run panel**: execution log tab, timeline, step output dialog, run ID in history, working Run button, refresh in workflow list.
- **Docker**: `rack-data` volume; `var/` mounted for persistent workflow state.

### 3. React SPA as the dashboard (`5cd526d`, `7e189ac`)

- Legacy HTML dashboard removed; **Vite + React 19 + Kaizen UI** served at `/`.
- NVIDIA Kaizen UI + MUI (tables, drawer, dialogs); Lucide icons only.
- Rack table, multirack view, parser/Grafana embeds, settings, rack detail tabs.
- `start.sh` auto-detects OS for **demo vs cluster** mode (`e3a0f9b`).

### 4. Rack advisory locks (`7556e2a`, `1338f63`)

- **Advisory** per-rack claims: who is working on which rack (≤10 operators, shared system user).
- Persistence: `var/cache/rack_locks.json` + `fcntl.flock`; TTL, heartbeat early release, force take-over.
- APIs: `GET/POST/DELETE /api/rack-locks[/<rack>]`.
- UI: lock icons on table, Claim/Release on rack detail, conflict dialog (“Run anyway”), toolbar active-users popover.
- Operator name + auto-claim in Settings; `X-Rack-Operator` / `X-Rack-Client-Id` on API calls.
- Workflow automated host steps acquire/release claims.

### 5. NVIS hostname templates — Firmus / CBR (`70028a2`…`5b6765c`)

- **`nvis_id_templates.py`**: `NVIS_RACK_ID`, `NVIS_CT_ID`, `NVIS_NVSW_ID` with `[01-99]` slots (replaces `RACK_CMSH_DS_NODE_*` regex settings).
- Bright defaults: `p[01-99]-r[01-99]-ct[01-99]`; CBR example: `CBR[01-99]-DH[01-99]-SVR-CN…`.
- Shared naming for cmsh `ds`, validation targets, multirack log labels, Open Log discovery, pdsh wildcards, Grafana PromQL embeds.
- **Slurm/pdsh**: expand `-w` via `scontrol show hostnames` (preserve `NodeName` casing); `NVIS_SLURM_HOSTLIST`, module-load controls.
- **Validation scripts**: RDMA/UCX/ClusterKit profiles (`nvis-rdma-common.inc.sh`, HCA presets, RoCE numactl).
- Default `VALIDATION_TEST` for singlerack: `nccl_all_reduce,llm` when unset.
- `export-dashboard-bundle.sh --dev` (optional `.git/` in handoff zip).
- SPA static assets (`/spa/`, `/assets/`) exempt from auth redirect (fixes blank login).

---

## Other improvements

| Area | Change |
|------|--------|
| **CI** | Coverage badges; workflow rules so lint/test run in MR pipelines; DinD TLS fix; **Kaniko** replaces DinD for docker-build; docker-build on all branch pushes |
| **Tests** | AI chat + workflow API tests; `test_nvis_id_templates.py`, `test_pdsh_slurm_expand.py` |
| **Docs** | README rewrite; `docs/ENVIRONMENT.md` NVIS naming + rack locks |
| **Dev** | `start.sh` defaults: project name `Rack Status`, logs under `var/logs`; `.gitignore` `.local/` |

---

## Configuration highlights

**Firmus / CBR naming (Settings → Job environment):**

```bash
NVIS_RACK_ID=CBR[01-99]-DH[01-99]-SVR-CN[01-99][01-99]
NVIS_CT_ID=CBR[01-99]-DH[01-99]-SVR-CN[01-99][01-99][01-99]
```

**Bright (defaults if unset):**

```bash
NVIS_RACK_ID=p[01-99]-r[01-99]
NVIS_CT_ID=p[01-99]-r[01-99]-ct[01-99]
```

**Slurm on PATH already:**

```bash
RACK_SLURM_SKIP_MODULE_LOAD=1
```

**RoCE / Spectrum-X ClusterKit:**

```bash
RACK_CLUSTERKIT_LINK_TECH=roce   # or spx
```

See [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md).

---

## Upgrade notes

1. Rebuild frontend: `cd frontend && npm ci && npm run build`
2. Migrate Settings from `RACK_CMSH_DS_NODE_*` to `NVIS_*` templates on non-Bright sites
3. Set operator display name in Settings for rack locks
4. Ensure `ipmitool` on head node for BMC recovery; BCM `python3` module for macswap

---

## Commits included

Full list (`08c0077^..HEAD`, 33 commits):

```
5da3923 feat: add macswap and bmc-recovery cards to Hardware tab
08c0077 Merge branch 'feature/integrate-macswap-bmc-recovery' into 'feature/react-frontend-nvis-ux'
cd5e30c feat: workflow editor improvements and step output capture
4668c3d feat: execution log tab, delay step, run button fix, refresh + run ID
7edfb2a fix: add requests and pyyaml to CI test dependencies
86ed770 test: add comprehensive tests for AI chat and workflow features
5c76663 ci: add coverage badges and reporting to CI pipeline
ebc9c9f docs: rewrite README to reflect AI assistant, workflow engine, and current architecture
9e33556 fix: frontend coverage badge regex to match vitest text output
563d9d8 fix: add default reporter to frontend test so coverage text outputs to stdout
d084c7b fix: frontend coverage badge reads from coverage-summary.json directly
eacd132 fix: use --coverage.reporter=text-summary for frontend coverage badge
75895f8 chore: remove local run state files, gitignore workflow runs
5cd526d feat: remove legacy HTML dashboard, serve React SPA at root
e3a0f9b fix: start.sh auto-detects OS for demo vs cluster mode
8d07d45 fix: add workflow rules so lint/test jobs run in MR pipelines, allow docker-build failure
958a35e fix: disable Docker TLS for DinD compatibility with runner
291330d ci: trigger fresh MR pipeline after approval settings update
7e189ac Merge branch 'feature/react-frontend-nvis-ux' into 'main'
7556e2a feat: rack-level advisory locks for multi-operator coordination
1338f63 Merge branch 'feature/rack-locks' into 'main'
08c31b3 ci: switch docker-build from DinD to Kaniko
b80f767 ci: run docker-build on all branch pushes
e5ce781 Merge branch 'feature-workflow' into 'main'
70028a2 Add NVIS rack/CT naming templates and unit tests.
16fcca6 Wire native_status and scripts to NVIS naming templates.
dd49734 Integrate NVIS naming in app.py while preserving rack locks.
3d526dc Update frontend and demo paths for NVIS naming templates.
b44db61 Docs and tests: NVIS naming reference; keep rack locks on main paths.
0067c9f Enhance NVIS naming and export script functionality.
d3b51f8 Refactor NEMO log handling and improve naming conventions.
94daae5 Update default project name and log directory in start.sh for consistency.
fccd47b fix: set CBR env vars and clear naming cache in cmsh rack extraction test
5b6765c Merge branch 'feature/firmus-ct-naming-convention' into 'main'
```

---

## Compare commands

```bash
git log --oneline 08c0077c6739495a87de8e51a325b0c32d0e9a4d..HEAD
git diff 08c0077c6739495a87de8e51a325b0c32d0e9a4d..HEAD --stat
```

---

*Baseline `08c0077` is the macswap/BMC-recovery integration point on the React frontend line. Features landed on other branches before that point are out of scope for this document.*
