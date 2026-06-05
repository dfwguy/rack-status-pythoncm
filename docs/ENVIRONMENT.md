# NVIS Rack Status configuration reference

This document lists **environment variables**, **credential files**, and related knobs under **`/home/cmsupport/nvis-rack-status`** only (Python: `backend/app.py`, `backend/native_status.py`, `backend/rack_web/`; shell: `bin/scripts/`). Third-party code under **`.venv/`** is not catalogued here.

**Truthy** (unless noted): `1`, `true`, `yes`, or `on` (case-insensitive).

For architecture and UI overview, see the root [README.md](../README.md).

---

## 1. Status table: native Python vs shell

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_STATUS_USE_SHELL_TABLE`** | *(unset)* | When **unset/false**, `/api/status` uses **`native_status.py`**. When **truthy**, runs **`rack_status.sh`** and parses its table — legacy path. |

Both paths share many **`RACK_STATUS_*`**, **`RACK_CMSH_*`**, Slurm, and log-probe settings below.

---

## 2. Pass-through to `rack_status.sh` / probes (includes `VALIDATION_TEST`)

Most subprocesses use **`env=os.environ.copy()`** (or the shell inherits the same environment as the nvis-rack-status process). Any variable in the **nvis-rack-status daemon’s** environment is therefore visible to:

- **`rack_status.sh`** (full table or **`--probe-json`** / **`--tmp-status`**),
- **`parser.sh`** when invoked from the API,
- other scripts unless the code strips keys.

### 2.1 `VALIDATION_TEST` — which NCCL / LLM signals count as “done”

Defined in **`bin/scripts/validation_test_env.sh`** and sourced by **`rack_status.sh`** (and thus used by probes).

| Aspect | Detail |
|--------|--------|
| **Purpose** | Comma-separated **validation suite**: which NCCL busbw columns must be present and whether LLM / Nemotron must be **OK** and finished for a log line to count as **`nccl_ok`** / **`llm_ok`** in probe JSON. |
| **Default when unset** | `nccl_all_reduce,nccl_alltoall,nccl_allgather,llm` |
| **Tokens** (case-insensitive) | `nccl_all_reduce` \| `all_reduce` \| `allreduce`; `nccl_alltoall` \| `alltoall` \| `nccl_all_to_all`; `nccl_allgather` \| `all_gather` \| `allgather`; `llm` \| `nemotron`. Omit NCCL tokens → no busbw requirement for that collective; omit `llm` → LLM not required. |
| **Megatron “finished”** | Uses **`NEMO_TARGET_STEPS`** (default **`2010`**) with last `iteration cur/tot` line in the log. |

**Set on the nvis-rack-status service** (e.g. systemd `Environment=`) to change behavior for **all** racks and probes. Example:

```bash
export VALIDATION_TEST=nccl_all_reduce,llm
```

### 2.2 `RACK_MULTIRACK_VALIDATION_TEST` (app.py only)

When **`app.py`** runs **`rack_status.sh --probe-json`** on a **multirack** log basename and **`VALIDATION_TEST`** is **not** already set, the app sets:

`VALIDATION_TEST=<RACK_MULTIRACK_VALIDATION_TEST>` (default **`nccl_all_reduce`**).

See also **`RACK_MULTIRACK_PROBE_TIMEOUT`** (§7): used as the **timeout for every** `rack_status.sh --probe-json` invocation from the app (minimum 60 seconds enforced in code), not only multirack logs.

### 2.3 `rack_status.sh` / `parser.sh` toggles and display

| Variable | Default | Effect |
|----------|---------|--------|
| **`USE_SQUEUE`** | `1` | `0` or **`--no-squeue`** → skip squeue-based “ongoing” hints. |
| **`USE_CMSH`** | `1` | `0` or **`--no-cmsh`** → skip cmsh rack list / `ds`. |
| **`USE_SINFO`** | `1` | `0` or **`--no-sinfo`** → skip sinfo column. |
| **`SQUEUE_USER`** | falls back to **`USER`** | `squeue -u …` filter. |
| **`SINFO_OPTS`** | *(unset)* | Appended to `sinfo -N -h -o '%N %t'` in `rack_status.sh`. |
| **`NEMO_TARGET_STEPS`** | `2010` | Megatron completion threshold (see §2.1). |
| **`NO_COLOR`** | *(unset)* | Disables ANSI colors when non-empty (standard). |
| **`FORCE_COLOR`** | `0` in scripts | Set `FORCE_COLOR=1` to force color when stdout is not a TTY (`rack_status.sh` / `parser.sh` use `force_c="${FORCE_COLOR:-0}"`). |

### 2.4 `RACK_CMSH_RACK_LIST_FILE` (implicit)

Not set directly in docs as an env you must export: **`app.py`** sets it on subprocesses to **`var/cache/cmsh_rack_list.txt`** when the cache warm succeeded (**`RACK_CMSH_RACK_LIST_CACHE_DISABLED`** is false). **`rack_status.sh`** reads it to avoid repeated `cmsh -c "rack; list"`.

---

## 3. Credential and config files

### 3.1 `NVLMAPPER_RC` / `~/.nvlmapperrc`

Used by **`bin/scripts/run_nvlmapper.sh`**.

| Mechanism | Purpose |
|-----------|---------|
| **`NVLMAPPER_RC`** | Path to credentials; default **`/root/.nvlmapperrc`**. |
| **Format** | `key=value` lines. **`username=`** (or **`user=`**) and **`password=`** required; merged into generated YAML before **`nvlmapper`** runs. |

### 3.2 `PROMETHEUS_RC` / `~/.prometheusrc`

Used by **`bin/scripts/launch-grafana-bright-prometheus.sh`** (not by Flask). After `source`, expects **`PASSWORD`**; file’s **`USER`** becomes Prometheus basic-auth user (login **`USER`** is restored). See §16.

### 3.3 NEMO / validation cluster scripts

| Variable | Where | Role |
|----------|-------|------|
| **`NEMO_SQSH_SRC`** | **`run_singlerack.sh`**, **`run-nemo-xrack.sh`**, **`run-llama.sh`** | Required: path on head/login to `.sqsh`; rsync’d to nodes under `/raid/data/`. |
| **`HC_CHECKER_SCRIPT`** | **`nemo-nemotron5h_56b-mbridge-fp8.sh`** | Optional post-job checker on nodes. |
| **`NEMORUN_HOME`**, **`CONT`**, **`MAX_STEPS`**, **`PYTHONPATH`** | Inside Slurm job scripts | Job layout and training length — site-specific. |
| **`SLURM_SUBMIT_DIR`**, **`SLURM_JOB_NODELIST`**, **`SLURM_NNODES`**, **`SLURM_JOB_SCRIPT`**, … | Slurm | Standard job environment when scripts run under **`sbatch`**. |
| **`NODELIST`** | **`sysinfo.sh`**, **`imex_check.sh`** | Node list for pdsh; may be set explicitly or falls back to **`SLURM_JOB_NODELIST`**. |
| **`PDSH_W`** | **`hw_check_pdsh.sh`** | Hostlist for pdcp/pdsh (set by caller / API path). |

---

## 4. Web server and authentication

| Variable | Default | Effect |
|----------|---------|--------|
| **`PORT`** | `5000` | Listen port (`python3 backend/app.py`). |
| **`FLASK_DEBUG`** | *(unset)* | `1` → Flask debug (**not** for production). |
| **`PROJECT_NAME`**, **`RACK_WEB_PROJECT_NAME`** | *(unset)* | Optional page title suffix. |
| **`RACK_WEB_VERSION`** | `dev` | `/api/app-stats`. |
| **`RACK_WEB_AUTH_DISABLED`** | *(unset)* | Truthy → no HTTP login. |
| **`RACK_WEB_AUTH_USER`** | `root` | PAM user. |
| **`RACK_WEB_PAM_SERVICE`** | `login` | PAM stack. |
| **`RACK_WEB_PAM_HELPER_PYTHON`** | `/usr/bin/python3` | Interpreter for `backend/rack_web/pam_helper.py`. |
| **`RACK_WEB_SECRET_KEY`** | *(auto on first start)* | Flask session signing and NVFTP vault encryption. If unset, the app creates `var/secrets/.rack_web_secret_key` on first launch (gitignored, persisted on the `var/` volume in Docker). Set this env var explicitly to use your own key. |
| **`NVIS_VAULT_MASTER_KEY`** | *(empty)* | Optional override for vault encryption instead of `RACK_WEB_SECRET_KEY`. |
| **`NVIS_TOKEN_VAULT_DIR`** | `var/secrets/` | Directory for encrypted token vault files (**never commit** — see `.gitignore` and `scripts/check_no_vault_in_git.sh`). |

### NVFTP cache sync (`var/cache` → SFTP)

Requires `RACK_WEB_SECRET_KEY`, **Settings → `RACK_PROJECT_TOKEN`**, vault NVFTP token, and **`RACK_NVFTP_SFTP_USER`**. Uploads run every **`RACK_NVFTP_SYNC_INTERVAL_SEC`** (default **7200** = 2 hours) to:

`/nvis-project-view/{RACK_PROJECT_TOKEN}/cache/…` mirrors every file under `var/cache/` plus `rack_comments.json` (optional `{RACK_NVFTP_REMOTE_BASE}/` prefix). `POST /api/nvftp-sync/run` with `{"force": true}` re-uploads all files.

| Variable | Default | Purpose |
|----------|---------|---------|
| **`RACK_PROJECT_TOKEN`** | *(empty)* | Remote folder name; empty disables sync |
| **`RACK_NVFTP_SFTP_USER`** | *(empty)* | SFTP username (token from vault is the password) |
| **`RACK_NVFTP_REMOTE_BASE`** | *(empty)* | Optional prefix before `/nvis-project-view/{RACK_PROJECT_TOKEN}/…` |
| **`RACK_NVFTP_SYNC_INTERVAL_SEC`** | `7200` | Sync interval in seconds (min 300) |
| **`RACK_NVFTP_SFTP_HOST`** | `transfer.nvftp.nvidia.com` | SFTP host |
| **`RACK_NVFTP_SFTP_PORT`** | `22` | SFTP port (set in shell env if needed) |
| **`RACK_NVFTP_SYNC_DISABLED`** | `0` | `1` to disable background sync |
| **`RACK_NVFTP_SYNC_FORCE`** | `0` | `1` to upload every cache file on each sync (not only changed files) |
| **`RACK_WEB_SESSION_HOURS`** | `8` | Session lifetime. |
| **`RACK_WEB_COOKIE_SECURE`** | *(unset)* | Truthy → `Secure` cookies. |
| **`RACK_WEB_ACTIVE_CLIENT_TTL_SEC`** | `120` | Client heartbeat TTL in `/api/app-stats`. |

---

## 5. Log directory, status scripts, and caching

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_STATUS_LOG_DIR`** | `APP_DIR/var/logs` | Default log root. |
| **`RACK_STATUS_SCRIPT`** | `bin/scripts/rack_status.sh` | Shell table script path. |
| **`RACK_STATUS_EXTRA_ARGS`** | *(unset)* | Extra args for `rack_status.sh`. |
| **`RACK_STATUS_ALLOWED_PREFIXES`** | *(unset)* | Allowed `log_dir` path prefixes (comma-separated). |
| **`RACK_STATUS_TIMEOUT`** | `300` | `rack_status.sh` timeout (shell mode). |
| **`RACK_STATUS_DEFAULT_REFRESH`** | `30` | UI default refresh (clamped 5–86400). |
| **`RACK_STATUS_BACKGROUND_DISABLED`** | *(unset)* | Truthy → no `rack-status-bg` cache thread. |
| **`RACK_STATUS_BACKGROUND_INTERVAL_SEC`** | `30` | Background loop interval. |
| **`RACK_LOG_ANALYSIS_CACHE_DISABLED`** | *(unset)* | Truthy → disables log analysis cache (shell + tmp status). |
| **`RACK_LOG_ANALYSIS_CACHE_MAX_FILES`** | `200` | Cache file cap. |
| **`RACK_LOG_ANALYSIS_SQUEUE_TIMEOUT_SEC`** | `60` | squeue helper timeout in log analysis. |
| **`RACK_CMSH`** | `cmsh` | cmsh binary. |
| **`RACK_CMSH_RACK_LIST_CACHE_DISABLED`** | *(unset)* | Truthy → skip rack-list file warm / unset `RACK_CMSH_RACK_LIST_FILE` for children. |
| **`RACK_CMSH_RACK_LIST_TIMEOUT_SEC`** | `120` | Warm timeout. |
| **`RACK_CMSH_INCLUDE_R00`** | *(unset)* | Truthy → keep `pN-r00` in lists. |
| **`RACK_PHYSICAL_RF0_MAP_DISABLED`** | *(unset)* | Truthy → skip RF0 map. |
| **`RACK_PHYSICAL_RF0_TIMEOUT`** | `180` | RF0 map timeout. |
| **`RACK_PHYSICAL_RF0_LOOSE_HOSTNAMES`** | *(unset)* | Truthy → looser RF0 hostname match. |

### Parser and export

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_PARSER_SCRIPT`** | `bin/scripts/parser.sh` | Parser path. |
| **`RACK_PARSER_EXTRA_ARGS`** | *(unset)* | Extra parser args. |
| **`RACK_PARSER_TIMEOUT`** | `300` | Parser timeout. |
| **`RACK_PARSER_EXPORT_GLOB`** | `*.txt` | Export glob. |
| **`RACK_PARSER_EXPORT_DISABLED`** | *(unset)* | Truthy → disable Excel export API. |
| **`RACK_PARSER_EXPORT_TIMEOUT`** | `900` | Export timeout. |
| **`RACK_LOG_EXPORT_DISABLED`** | *(unset)* | Truthy → disable log zip export. |
| **`RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER`** | *(unset)* | Truthy → export `*1Rack*` without NEMO `done` filter. |
| **`RACK_EXPORT_SUCCESS_GREP_TIMEOUT`** | `180` | Export grep timeout. |
| **`RACK_EXPORT_NODES_PER_RACK`** | `18` | Nodes per rack in export. |
| **`RACK_LOG_INLINE_MAX_BYTES`** | `10485760` | Inline log read cap. |

### Comments

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_COMMENTS_FILE`** | `APP_DIR/rack_comments.json` | Comments JSON path. |
| **`RACK_COMMENT_MAX_LEN`** | `4000` | Max comment length. |

---

## 6. Grafana / time zones (parser UI)

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_WEB_GRAFANA_URI`** | *(empty)* | Grafana base URL; empty → browser host + **`RACK_WEB_GRAFANA_PORT`**. |
| **`RACK_WEB_GRAFANA_PORT`** | `3000` | Port when URI empty. |
| **`RACK_WEB_GRAFANA_DASH_UID`** | `bright-gpu-per-host` | Dashboard UID for `/d-solo/`. |
| **`RACK_WEB_GRAFANA_DASH_SLUG`** | `bright-gpu-thermal-and-power` | Slug. |
| **`RACK_WEB_GRAFANA_EMBED_DISABLED`** | *(unset)* | Truthy → no iframes. |
| **`RACK_WEB_GRAFANA_TIME_PAD_SEC`** | `120` | Time window padding (seconds). |
| **`RACK_WEB_GRAFANA_TIMEZONE`** | *(unset)* | `browser`, `utc`, or IANA; else follows **`RACK_PROMETHEUS_LOG_TIMESTAMP_TZ`**. |
| **`RACK_PROMETHEUS_LOG_TIMESTAMP_TZ`** | `UTC` | Log timestamp parsing zone. |

Legacy name **`RACK_APP_GRAFANA_URL`** → use **`RACK_WEB_GRAFANA_URI`**.

---

## 7. Slurm (dashboard host) + `native_status.py`

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_SLURM_MODULE`** | `slurm` | `module load` name. |
| **`RACK_SLURM_SKIP_MODULE_LOAD`** | *(unset)* | Truthy → skip `module load`. |
| **`RACK_SLURM_SCONTROL`**, **`RACK_SLURM_SCANCEL`**, **`RACK_SLURM_SQUEUE`**, **`RACK_SLURM_SINFO`** | binary names | Override Slurm CLI paths. |
| **`RACK_SLURM_SCONTROL_TIMEOUT`** | `120` | Slurm CLI timeouts. |
| **`RACK_SLURM_DRAIN_REASON`** | `admin` | Default drain reason. |
| **`RACK_SLURM_NODES_STATE_DISABLED`** | *(unset)* | Truthy → disable drain/undrain. |
| **`RACK_SLURM_NODES_STATE_DRY_RUN`** | *(unset)* | Truthy → dry-run drain/undrain. |
| **`RACK_SLURM_SCANCEL_DISABLED`**, **`RACK_SLURM_SQUEUE_DISABLED`**, **`RACK_SLURM_SINFO_R_DISABLED`**, **`RACK_SLURM_SCONTROL_SHOW_JOB_DISABLED`** | *(unset)* | Per-API killswitches. |
| **`RACK_VALIDATION_CT_RANGE`** | `01-18` | CT bracket for hostlists. |
| **`SINFO_OPTS`** | *(unset)* | Extra `sinfo` args (`native_status.py` + `rack_status.sh`). |
| **`SQUEUE_USER`**, **`USER`** | `USER` | `squeue` user filter (`native_status.py` + scripts). |
| **`RACK_NATIVE_CMSH_TIMEOUT_SEC`** | `120` | Native `cmsh` rack list. |
| **`RACK_NATIVE_DS_TIMEOUT_SEC`** | `300` | Native `cmsh ds`. |
| **`RACK_NATIVE_SINFO_TIMEOUT_SEC`** | `120` | Native `sinfo`. |
| **`RACK_NATIVE_SCONTROL_TIMEOUT_SEC`** | `60` | Native `scontrol` hostnames. |
| **`RACK_NATIVE_SQUEUE_TIMEOUT_SEC`** | `60` | Native `squeue`. |
| **`RACK_SQUEUE_JOB_LOOKUP_DISABLED`** | *(unset)* | Truthy → skip per-job squeue enrichment. |
| **`RACK_NATIVE_SQUEUE_JOB_LOOKUP_TIMEOUT_SEC`** | `45` | That lookup’s timeout. |
| **`RACK_SACCT_TIMEOUT`** | `90` | `sacct` timeout. |
| **`RACK_SCONTROL_HOSTNAMES_TIMEOUT`** | `120` | Hostlist expansion. |
| **`RACK_MULTIRACK_LOG_RACK_SCAN_BYTES`** | `524288` | Bytes scanned for rack ids in multirack logs. |
| **`RACK_MULTIRACK_SITE_PREFIX`** | `p5` | Site prefix for multirack pod-style names. |
| **`RACK_MULTIRACK_VALIDATION_TEST`** | `nccl_all_reduce` | Default `VALIDATION_TEST` for multirack probes (§2.2). |
| **`RACK_MULTIRACK_PROBE_TIMEOUT`** | `180` | `rack_status.sh --probe-json` timeout from app (min 60). |

---

## 8. Power, GPU, services, uptime

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_POWER_DISABLED`**, **`RACK_POWER_TIMEOUT`**, **`RACK_POWER_RESET_SLEEP_SEC`**, **`RACK_POWER_DRY_RUN`** | — | Power APIs. |
| **`RACK_GPU_RESET_DISABLED`**, **`RACK_GPU_RESET_SCRIPT`**, **`RACK_GPU_RESET_TIMEOUT`**, **`RACK_GPU_RESET_DRY_RUN`** | — | GPU reset. |
| **`RACK_SERVICES_RESTART_DISABLED`**, **`RACK_SERVICES_RESTART_TIMEOUT`**, **`RACK_SERVICES_RESTART_DRY_RUN`** | — | Services restart. |
| **`RACK_UPTIME_DISABLED`**, **`RACK_UPTIME_PDSH_TIMEOUT`**, **`RACK_UPTIME_DRY_RUN`** | — | Rack uptime pdsh. |

---

## 9. Hardware check

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_HW_CHECK_SCRIPT`**, **`RACK_HW_CHECK_DISABLED`**, **`RACK_HW_CHECK_TIMEOUT`**, **`RACK_HW_CHECK_DRY_RUN`** | — | Async HW check. |
| **`RACK_HW_CHECK_HELP_DISABLED`**, **`RACK_HW_CHECK_HELP_TIMEOUT`** | — | `/api/hw-check-help`. |

**Set by nvis-rack-status (do not set manually for normal use):** **`RACK_WEB_HW_CHECK=1`** on nodes; **`RACK_WEB_PRINT_HELP=1`** on dashboard for `--help` only.

---

## 10. NVSwitch / NVLink / IMEX

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_NVSWITCH_AUDIT_SCRIPT`**, **`RACK_NVSWITCH_AUDIT_DISABLED`**, **`RACK_NVSWITCH_AUDIT_TIMEOUT`**, **`RACK_NVSWITCH_AUDIT_LINEBUFFER`** | — | NVSwitch audit. |
| **`RACK_NVSWITCH_NODESET_TIMEOUT`** | `30` | nodeset expansion. |
| **`RACK_NVSWITCH_SDN_RESET_SCRIPT`**, **`RACK_NVSWITCH_SDN_RESET_TIMEOUT`** | — | SDN reset. |
| **`RACK_NVSWITCH_SYSTEM_RESET_SCRIPT`**, **`RACK_NVSWITCH_SYSTEM_RESET_TIMEOUT`** | — | System reset. |
| **`RACK_IMEX_RESET_SCRIPT`**, **`RACK_IMEX_RESET_TIMEOUT`** | — | IMEX reset. |
| **`RACK_IMEX_CHECK_SCRIPT`** | *(bundled)* | IMEX check script path (no separate `_DISABLED` in code). Async IMEX check uses **`RACK_SYSINFO_TIMEOUT`** for its subprocess timeout (same as sysinfo). |

**`NVSWITCHES_UPHY_CHECK_PARALLEL`** (in **`nvswitches_rack_audit.sh`**, default **`24`**): parallel UPHY `nv show` fan-out. **`VERBOSE`**, **`BMC_FAIL_LOG`**, **`FORCE_PATCH`** — optional diagnostics / fix behavior inside that script.

---

## 11. NVLMAPPER

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_NVLMAPPER_DISABLED`**, **`RACK_NVLMAPPER_REPORTS_BASE`**, **`RACK_NVLMAPPER_TIMEOUT`** | — | Async NVLMAPPER staging. |

Credentials: **`NVLMAPPER_RC`** (§3.1). Template IPs live in **`run_nvlmapper.sh`**.

---

## 12. Bug reports / Download reports

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_BUG_REPORT_REMOTE_DIR`**, **`RACK_BUG_REPORT_DISABLED`**, **`RACK_BUG_REPORT_TIMEOUT`**, **`RACK_BUG_REPORT_LIST_TIMEOUT`**, **`RACK_BUG_REPORT_PDSH_FANOUT`**, **`RACK_BUG_REPORT_DRY_RUN`**, **`RACK_BUG_REPORT_SSH_USER`**, **`RACK_BUG_REPORT_SSH_OPTS`**, **`RACK_BUG_REPORT_SSH_CAT_TIMEOUT`** | — | Generate / list / download. |

---

## 13. Validation, stress, llama, xrack, sysinfo

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_VALIDATION_SCRIPT`**, **`RACK_VALIDATION_DISABLED`**, **`RACK_VALIDATION_TIMEOUT`** | — | Singlerack. |
| **`RACK_NEMO_XRACK_SCRIPT`**, **`RACK_NEMO_XRACK_DISABLED`**, **`RACK_NEMO_XRACK_TIMEOUT`** | — | Multirack NEMO. |
| **`RACK_LLAMA_SCRIPT`**, **`RACK_LLAMA_DISABLED`**, **`RACK_LLAMA_TIMEOUT`** | — | Llama launcher. |
| **`RACK_MNUBERGEMM_STRESS_SCRIPT`**, **`RACK_MNUBERGEMM_STRESS_DISABLED`**, **`RACK_MNUBERGEMM_STRESS_TIMEOUT`** | — | Stress job. |
| **`RACK_SYSINFO_SCRIPT`**, **`RACK_SYSINFO_DISABLED`**, **`RACK_SYSINFO_TIMEOUT`**, **`RACK_SYSINFO_LINEBUFFER`** | — | Sysinfo / streams. |

---

## 14. Log tail / streaming

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_TAIL_LOG_DISABLED`**, **`RACK_TAIL_INITIAL_LINES`**, **`RACK_TAIL_LINEBUFFER`** | — | Tail endpoints and buffering. |

---

## 15. Grafana Docker helper (`launch-grafana-bright-prometheus.sh`)

| Variable | Default | Effect |
|----------|---------|--------|
| **`PROMETHEUS_RC`** | auto-resolve | Credentials file for Prometheus datasource. |
| **`GRAFANA_CONTAINER`**, **`GRAFANA_HTTP_PORT`**, **`GRAFANA_IMAGE`**, **`GRAFANA_STATE_DIR`** | see script | Docker run. |
| **`PROMETHEUS_URL`**, **`PROM_DS_UID`** | Prometheus URL / DS UID | Provisioning. |
| **`TMPDIR`** | system | Used when building default state dir path. |
| **`GF_SECURITY_ADMIN_USER`**, **`GF_SECURITY_ADMIN_PASSWORD`** | `admin` / `admin` | Grafana admin in container. |
| **`GF_AUTH_ANONYMOUS_ENABLED`**, **`GF_AUTH_ANONYMOUS_ORG_NAME`**, **`GF_AUTH_ANONYMOUS_ORG_ROLE`** | anonymous viewer | Embed access. |

The script sets **`GF_SECURITY_ALLOW_EMBEDDING=true`** in `docker run` (not overridable via env in the current script text).

---

## 16. Disable-flag index (`grep` aid)

Search in **`app.py`** for: `_DISABLED`, `_DRY_RUN`, `_TIMEOUT`, `_DISABLED` on Slurm, **`RACK_TAIL_LOG_DISABLED`**, **`RACK_PARSER_EXPORT_DISABLED`**, **`RACK_LOG_EXPORT_DISABLED`**, **`RACK_NVLMAPPER_DISABLED`**, **`RACK_UPTIME_DISABLED`**, **`RACK_POWER_DISABLED`**, **`RACK_GPU_RESET_DISABLED`**, **`RACK_HW_CHECK_*`**, **`RACK_SYSINFO_DISABLED`**, **`RACK_VALIDATION_DISABLED`**, **`RACK_NEMO_XRACK_DISABLED`**, **`RACK_LLAMA_DISABLED`**, **`RACK_MNUBERGEMM_STRESS_DISABLED`**, **`RACK_NVSWITCH_AUDIT_DISABLED`**, **`RACK_BUG_REPORT_DISABLED`**, **`RACK_SERVICES_RESTART_DISABLED`**, **`RACK_SLURM_*_DISABLED`**.

---

## Appendix A — Every `RACK_*` name referenced under `nvis-rack-status/` (sorted)

Use this as a checklist against **`app.py`** / **`native_status.py`**. Defaults and semantics are in §§4–14.

```
RACK_BUG_REPORT_DISABLED
RACK_BUG_REPORT_DRY_RUN
RACK_BUG_REPORT_LIST_TIMEOUT
RACK_BUG_REPORT_PDSH_FANOUT
RACK_BUG_REPORT_REMOTE_DIR
RACK_BUG_REPORT_SSH_CAT_TIMEOUT
RACK_BUG_REPORT_SSH_OPTS
RACK_BUG_REPORT_SSH_USER
RACK_BUG_REPORT_TIMEOUT
RACK_COMMENT_MAX_LEN
RACK_COMMENTS_FILE
RACK_CMSH
RACK_CMSH_INCLUDE_R00
RACK_CMSH_RACK_LIST_CACHE_DISABLED
RACK_CMSH_RACK_LIST_FILE          # set on child processes by app.py (not usually user-exported)
RACK_CMSH_RACK_LIST_TIMEOUT_SEC
RACK_EXPORT_NODES_PER_RACK
RACK_EXPORT_SUCCESS_GREP_TIMEOUT
RACK_GPU_RESET_DISABLED
RACK_GPU_RESET_DRY_RUN
RACK_GPU_RESET_SCRIPT
RACK_GPU_RESET_TIMEOUT
RACK_HW_CHECK_DISABLED
RACK_HW_CHECK_DRY_RUN
RACK_HW_CHECK_HELP_DISABLED
RACK_HW_CHECK_HELP_TIMEOUT
RACK_HW_CHECK_SCRIPT
RACK_HW_CHECK_TIMEOUT
RACK_IMEX_CHECK_SCRIPT
RACK_IMEX_RESET_SCRIPT
RACK_IMEX_RESET_TIMEOUT
RACK_LLAMA_DISABLED
RACK_LLAMA_SCRIPT
RACK_LLAMA_TIMEOUT
RACK_LOG_ANALYSIS_CACHE_DISABLED
RACK_LOG_ANALYSIS_CACHE_MAX_FILES
RACK_LOG_ANALYSIS_SQUEUE_TIMEOUT_SEC
RACK_LOG_EXPORT_DISABLED
RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER
RACK_LOG_INLINE_MAX_BYTES
RACK_MULTIRACK_LOG_RACK_SCAN_BYTES
RACK_MULTIRACK_PROBE_TIMEOUT
RACK_MULTIRACK_SITE_PREFIX
RACK_MULTIRACK_VALIDATION_TEST
RACK_MNUBERGEMM_STRESS_DISABLED
RACK_MNUBERGEMM_STRESS_SCRIPT
RACK_MNUBERGEMM_STRESS_TIMEOUT
RACK_NATIVE_CMSH_TIMEOUT_SEC
RACK_NATIVE_DS_TIMEOUT_SEC
RACK_NATIVE_SCONTROL_TIMEOUT_SEC
RACK_NATIVE_SINFO_TIMEOUT_SEC
RACK_NATIVE_SQUEUE_JOB_LOOKUP_TIMEOUT_SEC
RACK_NATIVE_SQUEUE_TIMEOUT_SEC
RACK_NEMO_XRACK_DISABLED
RACK_NEMO_XRACK_SCRIPT
RACK_NEMO_XRACK_TIMEOUT
RACK_NVLMAPPER_DISABLED
RACK_NVLMAPPER_REPORTS_BASE
RACK_NVLMAPPER_TIMEOUT
RACK_NVSWITCH_AUDIT_DISABLED
RACK_NVSWITCH_AUDIT_LINEBUFFER
RACK_NVSWITCH_AUDIT_SCRIPT
RACK_NVSWITCH_AUDIT_TIMEOUT
RACK_NVSWITCH_NODESET_TIMEOUT
RACK_NVSWITCH_SDN_RESET_SCRIPT
RACK_NVSWITCH_SDN_RESET_TIMEOUT
RACK_NVSWITCH_SYSTEM_RESET_SCRIPT
RACK_NVSWITCH_SYSTEM_RESET_TIMEOUT
RACK_PARSER_EXPORT_DISABLED
RACK_PARSER_EXPORT_GLOB
RACK_PARSER_EXPORT_TIMEOUT
RACK_PARSER_EXTRA_ARGS
RACK_PARSER_SCRIPT
RACK_PARSER_TIMEOUT
RACK_PHYSICAL_RF0_LOOSE_HOSTNAMES
RACK_PHYSICAL_RF0_MAP_DISABLED
RACK_PHYSICAL_RF0_TIMEOUT
RACK_POWER_DISABLED
RACK_POWER_DRY_RUN
RACK_POWER_RESET_SLEEP_SEC
RACK_POWER_TIMEOUT
RACK_PROMETHEUS_LOG_TIMESTAMP_TZ
RACK_SERVICES_RESTART_DISABLED
RACK_SERVICES_RESTART_DRY_RUN
RACK_SERVICES_RESTART_TIMEOUT
RACK_SLURM_DRAIN_REASON
RACK_SLURM_MODULE
RACK_SLURM_NODES_STATE_DISABLED
RACK_SLURM_NODES_STATE_DRY_RUN
RACK_SLURM_SCONTROL
RACK_SLURM_SCONTROL_SHOW_JOB_DISABLED
RACK_SLURM_SCONTROL_TIMEOUT
RACK_SLURM_SCANCEL
RACK_SLURM_SCANCEL_DISABLED
RACK_SLURM_SINFO
RACK_SLURM_SINFO_R_DISABLED
RACK_SLURM_SKIP_MODULE_LOAD
RACK_SLURM_SQUEUE
RACK_SLURM_SQUEUE_DISABLED
RACK_SACCT_TIMEOUT
RACK_SCONTROL_HOSTNAMES_TIMEOUT
RACK_STATUS_ALLOWED_PREFIXES
RACK_STATUS_BACKGROUND_DISABLED
RACK_STATUS_BACKGROUND_INTERVAL_SEC
RACK_STATUS_DEFAULT_REFRESH
RACK_STATUS_EXTRA_ARGS
RACK_STATUS_LOG_DIR
RACK_STATUS_SCRIPT
RACK_STATUS_TIMEOUT
RACK_STATUS_USE_SHELL_TABLE
RACK_SQUEUE_JOB_LOOKUP_DISABLED
RACK_SYSINFO_DISABLED
RACK_SYSINFO_LINEBUFFER
RACK_SYSINFO_SCRIPT
RACK_SYSINFO_TIMEOUT
RACK_TAIL_INITIAL_LINES
RACK_TAIL_LINEBUFFER
RACK_TAIL_LOG_DISABLED
RACK_UPTIME_DISABLED
RACK_UPTIME_DRY_RUN
RACK_UPTIME_PDSH_TIMEOUT
RACK_VALIDATION_CT_RANGE
RACK_VALIDATION_DISABLED
RACK_VALIDATION_SCRIPT
RACK_VALIDATION_TIMEOUT
RACK_WEB_ACTIVE_CLIENT_TTL_SEC
RACK_WEB_AUTH_DISABLED
RACK_WEB_AUTH_USER
RACK_WEB_COOKIE_SECURE
RACK_WEB_GRAFANA_DASH_SLUG
RACK_WEB_GRAFANA_DASH_UID
RACK_WEB_GRAFANA_EMBED_DISABLED
RACK_WEB_GRAFANA_PORT
RACK_WEB_GRAFANA_TIME_PAD_SEC
RACK_WEB_GRAFANA_TIMEZONE
RACK_WEB_GRAFANA_URI
RACK_WEB_HW_CHECK                 # set by hw_check_pdsh.sh on targets (see §9)
RACK_WEB_PAM_HELPER_PYTHON
RACK_WEB_PAM_SERVICE
RACK_WEB_PRINT_HELP               # set by app for checker.sh --help (see §9)
RACK_WEB_PROJECT_NAME
RACK_WEB_SECRET_KEY
RACK_WEB_SESSION_HOURS
RACK_WEB_VERSION
```

---

## Appendix B — Other symbols read by nvis-rack-status `app.py` / `native_status.py`

| Symbol | Where | Role |
|--------|-------|------|
| **`FLASK_DEBUG`** | `app.py` `__main__` | Enables debug server. |
| **`PORT`** | `app.py` `__main__` | Listen port. |
| **`PROJECT_NAME`** | `app.py` | Title suffix (same role as **`RACK_WEB_PROJECT_NAME`**; either can be set). |
| **`USER`** | `native_status.py`, `app.py` | Fallback for Slurm / squeue user. |
| **`SQUEUE_USER`** | `native_status.py`, `app.py` | Overrides **`USER`** for squeue filter. |
| **`SINFO_OPTS`** | `native_status.py`, `rack_status.sh` | Extra sinfo arguments. |
| **`VALIDATION_TEST`** | Inherited by **`rack_status.sh`** / probes | §2.1 — comma-separated suite tokens. |
| **`NEMO_TARGET_STEPS`** | Inherited by **`rack_status.sh`** / **`validation_test_env.sh`** | Default `2010`; Megatron completion threshold. |
| **`USE_SQUEUE`**, **`USE_CMSH`**, **`USE_SINFO`** | **`rack_status.sh`**, **`parser.sh`** | Default `1` each; set `0` or use `--no-*` flags to skip subsystems. |
| **`NVLMAPPER_RC`** | **`run_nvlmapper.sh`** | §3.1. |
| **`PROMETHEUS_RC`**, **`PASSWORD`**, **`USER`** | **`launch-grafana-bright-prometheus.sh`** | §3.2 / §15 (Grafana helper). |
| **`NO_COLOR`** | Set by `app.py` on some subprocesses | Disables color in child tools. |

---

*Generated from repository sources; if a variable is missing, search **`/home/cmsupport/nvis-rack-status/backend/app.py`** and **`backend/native_status.py`** for the exact spelling.*

---

### Rack locks

The rack-locks layer (`rack_locks.py`, `GET/POST/DELETE /api/rack-locks[/<rack>]`) is
described in detail in the project README under **Rack Locks (multi-operator
coordination)**. The relevant env knobs:

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_LOCK_MAX_AGE_SEC`** | `21600` (6 h, min 60 s) | Implicit locks derived from in-flight jobs older than this are filtered out of `GET /api/rack-locks` (defends against leaked jobs that never reach a terminal phase). |
| **`RACK_LOCKS_FILE`** | `var/cache/rack_locks.json` | Where explicit claims are persisted (atomically, via `fcntl.flock`). |

Non-env defaults baked into `rack_locks.py`:

- Default claim TTL: `CLAIM_DEFAULT_TTL_SEC = 1800` (30 min). The HTTP caller can
  request any TTL, capped server-side at `CLAIM_MAX_TTL_SEC = 21600` (6 h).
- Heartbeat-based early release: claims are dropped when the owning browser
  `client_id` has been absent from `_app_clients_last_seen` for more than
  `OPERATOR_OFFLINE_SEC = 300` (5 min).

Operator identity is supplied by the SPA via the `X-Rack-Operator` HTTP header
(`Settings → General → Operator`). The matching `X-Rack-Client-Id` (or `client_id`
query param) is used for the heartbeat check above.

---
---

## NVIS hostname templates (Firmus / CBR / Bright)

| Variable | Default (Bright) | Purpose |
|----------|------------------|---------|
| **`NVIS_RACK_ID`** | `p[\d+]-r[\d+]` | Rack row id pattern (`p5-r03`, `CBR01-DH01-SVR-CN0102`, …) |
| **`NVIS_CT_ID`** | `p[\d+]-r[\d+]-ct[\d+]` | Full compute-tray hostname pattern |
| **`NVIS_NVSW_ID`** | *(optional)* | NVSwitch hostname pattern (reserved) |

Templates use literal text plus `[\d+]` (default, cmsh spelling) or `[01-99]` (fixed two-digit) slots. Configure in the SPA **Settings → Job environment** or via `NVIS_JOB_ENV_OVERRIDES_FILE`. Legacy `RACK_CMSH_DS_NODE_*` regex vars are superseded when NVIS templates are set.

See `backend/nvis_id_templates.py` and `backend/tests/test_nvis_id_templates.py`.

