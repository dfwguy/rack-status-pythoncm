# NVIS Rack Status configuration reference

## `config/rackstatus.toml` and the required-setting preflight

Every variable in this document can also be set in **`config/rackstatus.toml`** (create the minimal
one with `python3 backend/cli.py init-config`, copy more from `config/rackstatus.toml.example`, or
point elsewhere with `NVIS_RACKSTATUS_CONFIG`). The file is the
**baseline**: anything already present in the environment at launch wins, so this document remains
the authority on what each variable *means* and the file only changes where a value can come from.

Mechanically, the loader materializes the file into `os.environ` with `setdefault` semantics before
any other first-party module is imported. That matters for two reasons an operator can observe:

* **An empty value is a value.** `FOO=""` in the environment is *not* overwritten by the file,
  because several readers here distinguish empty from unset.
* **The file can set `NVIS_LOG_LEVEL`**, because it is read before logging is configured. The
  loader's own diagnostics are buffered and replayed once a handler exists, so they appear slightly
  after other startup lines.

Types map as you would expect, with one refusal: `true`/`false` become `1`/`0`, integers render in
base 10, arrays join on `,` with no spaces — and **floats are rejected**. Both float-shaped settings
here are version strings (`NVIS_NCCL_MIN_CUDA_VERSION`, `RACK_WEB_TLS_MIN_VERSION`) and rendering
one changes its meaning: `13.30` becomes `13.3`, and a shell comparison that splits on `.` then
reads a different number. Quote them.

**Secrets are refused from the file.** A variable whose name matches `PASSWORD|SECRET|TOKEN|API_KEY|
CREDENTIAL` — or that is explicitly declared secret — makes startup fail if it appears there. The
file is exactly the artifact that ends up in bug reports and support bundles, and it is not covered
by `.gitignore` once copied. `RACK_PROJECT_TOKEN` is the documented exception: it matches the
pattern but is an NVFTP folder name, so it is declared non-secret.

### Settings that can refuse a launch

| Setting | Required when | Why it cannot be defaulted |
|---|---|---|
| `RACK_WEB_OIDC_ISSUER` | *(always read; derived from the bind address when unset)* | OIDC Discovery §4.3 requires it to be the URL discovery is served from; deriving it from `Host` would let a caller choose our token issuer |
| `RACK_SSH_HOST` | the resolved control connection is SSH and `RACK_STATUS_DEMO` is falsy | no default can reach a customer's head node |
| `RACK_WEB_TLS_KEY` | `RACK_WEB_TLS_CERT` is set in the environment | a certificate with no key cannot serve |

All missing settings are reported **together**, then startup exits `78`. A setting with a working
default is deliberately absent from this table even when it is "required in practice": missing and
defaulted are different states, and only the first is a fault.

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
| **`NEMO_SQSH_SRC`** | **`run_singlerack.sh`**, **`run-nemo-xrack.sh`**, **`run-llama.sh`** | Required: path on head/login to `.sqsh`; staged into each node's image cache (`NVIS_NODE_CONTAINER_DIR`) unless a same-size copy is already there. |
| **`NVIS_NODE_CONTAINER_DIR`** | every container launcher (`lib/node_images.inc.sh`) | Where compute nodes cache container images. Set per cluster as `connection.node_container_dir` (Projects → cluster → *Node image cache*); default `/tmp/containers`, `/opt/containers` on Nscale NC. On a P2P cluster (`NVIS_NODE_IMAGES_P2P=1`) launchers never copy it from the head; they check each node's file against the image catalog published beside the scripts the project runs, `<scripts>/config/image-catalog.tsv` (size, then SHA256). |
| **`NVIS_FABRIC_NETWORK`** | `lib/fabric.inc.sh` (`nvis_fabric_network`; every NCCL/LLMB/ClusterKit launcher) | `IB` or `RoCE` (also `auto`, the default when unset): the cluster's fabric network, which decides how the scripts find the compute interfaces. From the cluster's `fabric.scaleout` (**Fabric network** in the cluster form), else the Settings override, else the platform's `fabric.scaleout`. Replaces `NVIS_RDMA_LINK_TECH`, which is read only when this is unset. |
| **`NVIS_FABRIC_HCA`** | `lib/fabric.inc.sh` | The compute plane as an `NCCL_IB_HCA` value (e.g. `roce_vf_r`), set per cluster as `fabric.compute_hca`. Blank = detect: on RoCE the scan keeps ACTIVE ports with a routable GID off the default-route netdev and pins their common name prefix. An explicit `NCCL_IB_HCA` still wins. |
| **`NVIS_FABRIC_NET_PLUGIN`** | `lib/fabric.inc.sh` | The NCCL net plugin a containerized job's ranks load, set per cluster as `fabric.net_plugin`: a path **inside the image** (Nscale NC: `/opt/hpcx/nccl_spectrum-x_plugin/lib/libnccl-net-spcx.so`) or `none` for NCCL's built-in IB/RoCE transport. `nvis_fabric_container_env` exports it as `NCCL_NET_PLUGIN` (an explicit one still wins) and the LLMB launcher forces it into the job's `--export`; the same function sets `MELLANOX_VISIBLE_DEVICES=all` on IB or RoCE so the container gets its RDMA devices. Blank = NCCL's own choice. |
| **`NVIS_FABRIC_ADDR_FAMILY`** | `lib/fabric.inc.sh` | `ipv4` or `ipv6`, set per cluster as `fabric.addr_family`; exported as `NCCL_IB_ADDR_FAMILY` (`AF_INET`/`AF_INET6`). Blank = read off the compute GIDs (Nscale NC's are IPv6-only). |
| **`NVIS_FABRIC_EXPECT_RATE`** | `lib/fabric.inc.sh` (`nvis_fabric_report`, `fabric_probe.sh`), `nvlink_network_deep_audit.sh` | The per-rail line rate expected, in Gb/s. The cluster's `fabric.rail_rate_gbps` wins; otherwise the platform's, projected only where the cluster runs the platform's own fabric protocol. Unset, the network audit holds each RoCE rail to the fastest rail on the node. A rate configured for a different protocol than the node runs is ignored with a warning. |
| **`NVIS_NODE_IMAGE_VERIFY`** | `lib/node_images.inc.sh` | `sha256` (default) hashes each node's image against the catalog pin; `size` checks only the byte size, for a quick re-launch. |
| **`NVIS_NODE_CONFIG_DIR`** | `lib/node_config.inc.sh` | Where scripts read settings Rack Status publishes for the cluster. Default `<this scripts copy>/config` — `project-<id>/config` for a bootstrapped project, else the platform layer's or `multi-platform/config` — on the shared `/home`, so the head and every node read one set. Override only for tests or relocated scripts. |
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
| **`RACK_WEB_SECRET_KEY`** | *(auto on first start)* | Flask session signing and NVFTP vault encryption. If unset, the app creates `var/secrets/.rack_web_secret_key` on first launch (gitignored, persisted on the `var/` volume in Docker). Set this env var explicitly to use your own key. |
| **`NVIS_VAULT_MASTER_KEY`** | *(empty)* | Optional override for vault encryption instead of `RACK_WEB_SECRET_KEY`. |
| **`NVIS_TOKEN_VAULT_DIR`** | `var/secrets/` | Directory for encrypted token vault files (**never commit** — see `.gitignore` and `scripts/check_no_vault_in_git.sh`). |

### Session lifetimes

| Variable | Default | Ceiling | Purpose |
|----------|---------|---------|---------|
| **`RACK_WEB_ACCESS_TTL_SECONDS`** | `3600` (1 hour) | `604800` (7 days) | Access-token lifetime. The SPA renews it silently — proactively before it expires and reactively on a 401 — so this is rarely the number an operator feels. Short by design: it bounds how long a *leaked, unrevoked* token keeps working. |
| **`RACK_WEB_REFRESH_TTL_SECONDS`** | `604800` (7 days) | `2592000` (30 days) | Refresh-token lifetime — **this is the session length.** The refresh token is the session's source of truth: the SPA stays signed in for exactly this long (a refresh never rotates the refresh token, so there is no sliding window), then requires a fresh login. Change *this* to make operators stay signed in longer or shorter. |

Both are clamped to their ceiling and floored at 60 seconds, with a warning naming the value in
force. A too-generous setting is not a startup failure; it is also not silently obeyed.

**What these bound, and what they don't.** Raising them does *not* slow a revocation, and does not
let an authorisation decision go stale. Tokens carry no grants — only `sub`, `typ` and timing — so
group membership and policy are read from the store on every request, and the revocation record is
consulted on every verify. Disabling, deleting or resetting a user cuts every live session
immediately, whatever life the token had left.

What a longer lifetime does extend is the window in which a **stolen token that nobody has revoked**
still works. That is the trade-off to weigh: one hour is four times fifteen minutes for that case,
and seven days is a genuinely long-lived bearer credential. The revocation fast path is what makes
the longer defaults defensible, which is why it is load-bearing rather than a nicety.

*Raised from 15 minutes / 12 hours on 2026-09-01.* The original short access TTL was chosen on the
reasoning that it bounded FR-6 staleness — reasoning that described a design where grants ride
inside the token, which is not what was built. It cost a re-authentication mid-task several times a
day and, until the SPA gained automatic renewal, presented as the product logging you out at random.

### OIDC provider (Phase 2.5) — off by default

| Variable | Default | Purpose |
|----------|---------|---------|
| **`RACK_WEB_OIDC_ISSUER`** | *(none)* | **Required when enabled.** This deployment's own base URL, e.g. `https://rack.example.com`. `http://localhost:PORT` is accepted, since that is how an operator reaches a tunnelled deployment. |
| **`RACK_WEB_SIGNING_KEY_DIR`** | `var/secrets/oidc/` | Where the ES256 key pair lives. Generated on first boot. |
| **`RACK_WEB_SIGNING_KEY_VALIDITY_SECONDS`** | `46656000` (18 months) | Key validity. Signing stops at `not_after − T_max`, so at the 30-day refresh ceiling a key signs for ~17 months. |
| **`RACK_WEB_OIDC_CLIENTS_FILE`** | `var/secrets/oidc/clients.json` | Registered relying parties. Absent = no clients, which is a valid state. |

**With the provider on and no issuer, the app refuses to start.** That is deliberate, and it
replaced a much worse behaviour: measured 2026-09-03, the app booted perfectly in that state —
`GET /login` returned 200 and `bin/verify-deploy.sh` passed — and then *every* sign-in failed at
token-mint time. A deployment green by every health check in which nobody can log in. Startup now
refuses instead, naming the variable, so the failure is one a deploy is built to catch.

The issuer cannot be defaulted or derived. OIDC Discovery 1.0 §4.3 requires it to be the URL
discovery is served from, and reading it from the `Host` header would let a caller choose the issuer
of our tokens. It is operator-configured, always.

**Turning this on ends every live session, once.** HS256 and ES256 tokens do not verify against
each other, and there is no dual-verification mode: honouring the old tokens would mean keeping the
symmetric secret alive, which is the thing being removed. Everyone signs in again. **Flipping the
flag back does not undo it** — it signs everyone out a second time. Treat it as a scheduled cutover,
not a runtime toggle.

**One flag governs signing and the endpoints together**, deliberately. The dangerous state is not
on or off but half: a deployment signing HS256 while publishing a JWKS advertises a key that
verifies nothing, and every consumer then fails in a way that looks like our bug.

**The issuer is configured, never derived from the request.** A `Host` header is attacker-controlled
and the issuer is a security boundary — deriving it would let someone make us mint a token claiming
to come from an origin they control.

`clients.json` registers relying parties. `redirect_uris` are matched by **exact string**; wildcards
are refused at load with an explanation, because an over-broad match hands the authorization code to
an attacker-chosen origin:

```json
{"clients": [
  {"client_id": "grafana", "client_type": "confidential", "client_secret": "…",
   "redirect_uris": ["https://grafana.example.com/login/generic_oauth"]},
  {"client_id": "cli", "client_type": "public",
   "redirect_uris": ["http://localhost:8123/callback"]}
]}
```

**Guidance for consumers**, adopted from Microsoft's for Entra rather than invented: cache keys
individually by `kid`, refresh about hourly, and **do not depend on how many keys the JWKS holds** —
it is two in steady state and three during a rotation, and an emergency rotation can change that at
any time.

### Authentication: there is nothing to configure

**Removed 2026-09-08: `RACK_WEB_AUTH_DISABLED` and `RACK_WEB_AUTH_USER`.** Authentication has no off switch. The gate runs for every request, `start.sh` no longer exports the flag on macOS, and a first boot creates an administrator and the policy that grants them — so a local run is a real, authenticated, policy-enforcing deployment. Tests opt out explicitly with `identity.testing.unauthenticated_client(app)`; nothing else can.

**Removed 2026-09-08: `RACK_WEB_OIDC_ENABLED`.** The OIDC provider is unconditional — asymmetric signing, JWKS, discovery and the provider endpoints are always present, and there is no legacy token shape to fall back to. `RACK_WEB_OIDC_ISSUER` is still read, and is now derived from the bind address when unset rather than being required.

**Removed 2026-09-01 (FR-18): `RACK_WEB_AUTH_MODE`, `RACK_WEB_AUTH_TOKEN`, `RACK_WEB_PAM_SERVICE`
and `RACK_WEB_PAM_HELPER_PYTHON`.** Shared-token and PAM login are gone, along with the vendored
`rack_web/pam_*` bindings and `POST /api/login`. Setting any of those variables now has no effect —
which is the point of FR-12: no environment variable grants access.

Authentication is SRP against the local identity store. Passwords are never transmitted and never
stored; the server keeps a verifier. On the **first start with an empty store** the app creates an
`admin` user and prints a one-time password:

```bash
docker logs <container> 2>&1 | grep -A6 "ONE-TIME ADMINISTRATOR"
```

Sign in with it and the UI will require you to choose your own. The store lives under `var/`, which
in Docker is the `rack-data` named volume, so it survives a `docker compose up -d` recreate — a
store that vanished on redeploy would lock everyone out.

**Upgrading a deployment that used a shared token:** the token stops working in this release, and the
same start that removes it bootstraps the `admin` account (FR-10b). Read the password from the
startup output before anyone needs to sign in. `NVIS_IDENTITY_FILE` overrides the store location if
you keep state elsewhere.

**Losing every administrator credential means redeploying the identity store.** There is no recovery
code and no self-service reset — an authenticated administrator can reset *another* user's
credential (FR-13a), but nothing can help when nobody can sign in.

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
| **`RACK_WEB_TLS_CERT`** | *(unset)* | PEM certificate (chain, if CA-issued). The app serves **HTTPS only**; the certificate is the first of: this variable, `config/tls/server.crt`, or a self-signed pair generated under `RACK_WEB_TLS_DIR` on first boot (`startup_gate.ensure_tls`, called by `gunicorn.conf.py`). Issue one with `bin/gen-tls-cert.py`. **Its SAN must include `localhost`** — that is the name a browser validates through an SSH tunnel. |
| **`RACK_WEB_TLS_KEY`** | *(unset)* | Matching private key (0600). Required whenever the cert variable is set; half a pair is refused at startup. `config/tls/server.key` when the certificate comes from `config/tls/`. |
| **`RACK_WEB_TLS_MIN_VERSION`** | `1.2` | Minimum negotiable TLS version (`1.2` or `1.3`). Enforced explicitly on **both** servers by `backend/tls_server.py` — gunicorn 23 still defaults `ssl_version` to the permissive `PROTOCOL_TLS`, so the floor is not left to the server's or the Python build's default. An unrecognised value warns and keeps 1.2 rather than widening. Also disables TLS compression (CRIME) and client-initiated renegotiation. |
| **`RACK_WEB_COOKIE_SECURE`** | *(auto)* | `Secure` cookies. **Automatic whenever the app serves** (every serving path settles a certificate first); set it explicitly only when TLS terminates in a proxy this process cannot see. |
| **`RACK_WEB_CSP_MODE`** | `enforce` | `enforce`, `report` (send `Content-Security-Policy-Report-Only` — logs violations, blocks nothing) or `off`. `report` lets a new deployment confirm the policy fits its Grafana setup first; `off` is the rollback. |
| **`RACK_WEB_BIND_ADDR`** | `127.0.0.1` | *(compose)* Address the container's port is published on. Loopback by default so the dashboard is reachable only through an SSH tunnel; `0.0.0.0` exposes it on the host's network, which previously was the unintended default. |
| **`RACK_WEB_ALLOWED_ORIGINS`** | *(unset)* | Comma-separated extra browser origins allowed to open `/ws/terminal`. Same-origin is always allowed, so this is only needed when the page and the WebSocket differ (a reverse proxy under another name, a separate dev host). Deploy-time env only — deliberately **not** settable from Settings, since widening it is a privilege change. |
| **`RACK_WEB_ACTIVE_CLIENT_TTL_SEC`** | `120` | Client heartbeat TTL in `/api/app-stats`. |

---

## 5. Log directory, status scripts, and caching

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_STATUS_LOG_DIR`** | `APP_DIR/var/logs` | **Deprecated — do not set.** One global log root for every cluster at once, so a multi-tenant deployment reads a project's logs from a cluster it may not operate. Set the log directory **per cluster** instead (the cluster's `connection.log_dir`, "Log directory" in the cluster editor). Kept as a fallback for deployments still migrating; warns once when used. |
| **`RACK_STATUS_SCRIPT`** | `bin/scripts/rack_status.sh` | Shell table script path. |
| **`RACK_STATUS_EXTRA_ARGS`** | *(unset)* | Extra args for `rack_status.sh`. |
| **`RACK_STATUS_ALLOWED_PREFIXES`** | *(unset)* | Allowed `log_dir` path prefixes (comma-separated). Must include the bring-up `results` root when the Nodes view reads Travis's per-node artifacts. |
| **`RACK_BRINGUP_HOME`** | `/home/cmods/bringup` | Root of Travis's bring-up automation tree on the execution host. `/api/bringup/node` shells out to `<home>/daily-report.sh` and `<home>/status-check.sh`; `/api/bringup/config` probes `<home>/results`. |
| **`RACK_BRINGUP_DISABLED`** | *(unset)* | Truthy → hide the Nodes "Bring-up" panel (`/api/bringup/*` returns disabled). |
| **`RACK_BRINGUP_CACHE_TTL_SEC`** | `30` | TTL for the cached cluster-wide `daily-report.sh -c` (it re-scans every result dir; the cache makes repeated node opens cheap). |
| **`RACK_BRINGUP_REPORT_TIMEOUT`** | `180` | Seconds a fleet-wide bring-up report (`report_job-status.sh`, `report_benchmarks.sh -c`) may run on the head before the call gives up. Also a Settings value. Node Stage 2 runs only its own column (`-t <RACK_NODE_STAGE2_BRINGUP_JOB>`), ~34 s on a 4,608-node fleet. |
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

### Application logging (`NVIS_LOG_*`)

Applied once at startup by `backend/logging_config.py`, so every module's `logging.getLogger`
routes to the configured sinks with no change at the call site. Before this existed there was no
logging configuration at all: with no handler on the root logger Python falls back to
`logging.lastResort` at WARNING, so every INFO and DEBUG record was discarded silently.

Read from the **process environment only**, never through the dashboard settings store — an
authenticated operator must not be able to lower the level, redirect a sink, or empty the
sensitive-key list at runtime.

| Variable | Default | Effect |
|----------|---------|--------|
| **`NVIS_LOG_LEVEL`** | `INFO` | Minimum level reaching any sink. |
| **`NVIS_LOG_SINKS`** | `stdout,file` | Comma-separated from `stdout`, `file`, `syslog`. See the syslog note below. |
| **`NVIS_LOG_FORMAT`** | `text` | `text` for humans, `json` for one-object-per-line SIEM ingest. |
| **`NVIS_LOG_FILE`** | `var/logs/rack-status.log` | Application log. Relative paths resolve against the repo root, not the process CWD. |
| **`NVIS_LOG_MAX_BYTES`** | `5242880` | Rotation size for the application log. Its budget is independent of the audit trail's, so a debug session cannot rotate away audit evidence. |
| **`NVIS_LOG_BACKUPS`** | `3` | Rotation backup count. `3` keeps four files including the active one. |
| **`NVIS_LOG_SYSLOG_HOST`** | `localhost` | Only consulted when `syslog` is selected. |
| **`NVIS_LOG_SYSLOG_PORT`** | `514` | Only consulted when `syslog` is selected. |
| **`NVIS_LOG_SENSITIVE_KEYS`** | *(unset)* | Extra environment variable names to redact, beyond the built-in name match on `PASSWORD`, `SECRET`, `TOKEN`, `API_KEY`, `CREDENTIAL`. |
| **`NVIS_LOG_REDACT_PLACEHOLDER`** | `[REDACTED]` | Replaces a secret's value wherever it appears in a record. |

**The `syslog` sink is best-effort and lossy.** It is UDP, so a dropped datagram cannot be
distinguished from one that was never sent, and it is off by default. Selecting it as the *only*
sink is not honoured: the `file` sink is added back and the substitution is reported at startup,
because a configuration where the sole record of an operator action can vanish without trace is
not a configuration anyone asked for. Durability comes from the file sink, an export API, or a
future cloud target — never from syslog.

The defaults require no network egress, no external service and no credential, so a first run on
an air-gapped cluster works with nothing set.

### The audit trail (`NVIS_AUDIT_*`)

A separate, durable record of operator actions — one JSON object per line, written by
`backend/audit/`. Its rotation budget is deliberately **independent** of the application log's, so
a debug session cannot rotate away the evidence of what someone did. On by default: an audit trail
that has to be switched on is one that is off.

| Variable | Default | Effect |
|----------|---------|--------|
| **`NVIS_AUDIT_ENABLED`** | `1` | Falsy disables the trail entirely. |
| **`NVIS_AUDIT_FILE`** | `var/logs/audit/audit.jsonl` | The trail. Created `0600` inside a `0700` directory regardless of the process umask. |
| **`NVIS_AUDIT_MAX_BYTES`** | `5242880` | Per-file size cap — one half of the budget. |
| **`NVIS_AUDIT_BACKUPS`** | `24` | Rotation backups. The count excludes the active file, so `24` keeps **25** files and the ceiling is **125 MiB**, not 120. Raised from `3` on 2026-09-10: at a measured 1.15 MB/day, three backups held about 17 days, so a review asking about last month found it gone. 125 MiB is roughly 90 days at that rate. Lower it only if disk is genuinely scarce — the trail is the only record of who did what. |
| **`NVIS_AUDIT_RETENTION_DAYS`** | `30` | The other half: a retention target in days. The sink holds whichever of the two binds first and records which one it is. |
| **`NVIS_AUDIT_LONG_RETENTION`** | `0` | Truthy multiplies the backup count, keeping history beyond the default budget. Deliberate, operator-accepted growth — still bounded. |
| **`NVIS_AUDIT_READS`** | `0` | Truthy audits read-only requests too. Off by default because `/api/status` polling would bury the signal. |
| **`NVIS_AUDIT_FLOOD_WINDOW_SEC`** | `60` | Flood-guard window. |
| **`NVIS_AUDIT_FLOOD_THRESHOLD`** | `10` | Refusals per window written individually before the rest collapse into one counted summary. |
| **`RACK_WEB_TRUSTED_PROXIES`** | *(empty)* | Comma-separated hosts or CIDRs whose `X-Forwarded-For` is believed. Empty means **no** forwarded header is ever trusted. Entries broader than `/24` (IPv4) or `/64` (IPv6) are refused at startup and do not take effect — a wide entry would make every forged header authoritative *and* label it `proxy`, which is worse than leaving it unlabelled. |

**Why the budget has two constraints.** A fixed byte budget silently encodes an operator count. At
the measured ~502 bytes per event, 20 MiB is about 41 days at 1,000 events a day — but 1,000 a day
is roughly *two* operators working steadily, and twenty operators reduce the same budget to about
4 days. So the byte budget is derived from the observed event rate against the retention target and
capped by the ceiling, and the sink records which constraint is binding rather than leaving a
30-day claim that quietly stopped being true.

**Why refusals are counted rather than all written.** Recording refused actions is the point of an
audit trail, but against a bounded budget a denial loop would evict the very history it belongs in.
So the first `NVIS_AUDIT_FLOOD_THRESHOLD` refusals for a given principal, action and reason are
written individually — a lone refusal is always visible immediately — and further identical ones
accumulate into a single summary carrying the count.

**A source address is provenance, not authentication.** `src_ip` and its trust label exist to stop
the *trivial* forgery — an operator setting `X-Forwarded-For` and having it recorded as fact — and
to say honestly how the value was derived. They do not authenticate the peer, and no amount of
proxy-list hygiene can: BGP hijacking, a TLS-intercepting proxy, or anyone on the path can forge or
observe a connection. The only mechanism that genuinely binds an identity to a connection is
**mutual TLS**, where both ends present a certificate from a trusted CA. Phase 0.5 already ships the
CA, the certificate tooling and a TLS 1.2 floor, so client certificates are an increment rather than
a new capability — and short-lived client certs compose with the revocation design in
`auth/PHASE3_POLICY_ENGINE_SPIKE.md` §5b. Until then, read `src_ip` as "where the connection appeared to
come from", never as "who this was".

**On source addresses in the audit trail.** An audit event records the TCP peer address it
actually sees. Operators normally reach this dashboard through an SSH tunnel, and in that case
the peer is the **tunnel endpoint** — usually `127.0.0.1` — not the operator's workstation. The
operator's real address exists only in the SSH server's own authentication log, which is outside
this application's view. A forwarded address (`X-Forwarded-For`) is believed only when the peer is
a configured trusted proxy; otherwise the header is recorded as claimed-but-untrusted and the peer
address is what the event carries. An audit field that looks precise and is not would be worse
than one that states its limit.

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
| **`RACK_WEB_GRAFANA_VAR_RACK`** | `Rack` | Template-var name for rack-scoped deep-links (`var-<name>=<rack>`). BCM/NMC dashboards use `Rack`. |
| **`RACK_WEB_GRAFANA_VAR_HOST`** | `hostname` | Template-var name for host-scoped deep-links (`var-<name>=<node>`). BCM/NMC dashboards use `hostname`. |
| **`RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS`** | *(unset)* | JSON list of "Open in Grafana" deep-link dashboards (`{id,label,uid,slug,scope}`, scope=`host`\|`rack`\|`cluster`). Empty → built-in BCM/NMC GB200 catalog. |
| **`RACK_WEB_GRAFANA_DEEPLINK_DISABLED`** | *(unset)* | Truthy → hide the "Open in Grafana" deep-link menus (rack table + parser results). |
| **`RACK_PROMETHEUS_LOG_TIMESTAMP_TZ`** | `UTC` | Log timestamp parsing zone. |

Legacy name **`RACK_APP_GRAFANA_URL`** → use **`RACK_WEB_GRAFANA_URI`**.

---

## 7. Slurm (dashboard host) + `native_status.py`

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_SLURM_MODULE`** | `slurm` | `module load` name. |
| **`RACK_SLURM_SKIP_MODULE_LOAD`** | *(unset)* | Truthy → skip `module load`. |
| **`RACK_SLURM_BIN`** | *(unset)* | Slurm `bin/` directory prepended by shell launchers. An explicit value wins over an older Slurm already on `PATH`. |
| **`RACK_SLURM_SBATCH`**, **`RACK_SLURM_SRUN`**, **`RACK_SLURM_SCONTROL`**, **`RACK_SLURM_SCANCEL`**, **`RACK_SLURM_SQUEUE`**, **`RACK_SLURM_SINFO`**, **`RACK_SLURM_SACCT`** | binary names | Override Slurm CLI paths used by the backend platform layer. Required with `RACK_SLURM_SKIP_MODULE_LOAD=1` when the binaries are not on the non-login shell's `PATH`. |
| **`RACK_SLURM_SCONTROL_TIMEOUT`** | `120` | Slurm CLI timeouts. |
| **`RACK_SLURM_DRAIN_REASON`** | `admin` | Default drain reason. |
| **`RACK_SLURM_NODES_STATE_DISABLED`** | *(unset)* | Truthy → disable drain/undrain. |
| **`RACK_SLURM_NODES_STATE_DRY_RUN`** | *(unset)* | Truthy → dry-run drain/undrain. |
| **`RACK_SLURM_SCANCEL_DISABLED`**, **`RACK_SLURM_SQUEUE_DISABLED`**, **`RACK_SLURM_SINFO_R_DISABLED`**, **`RACK_SLURM_SCONTROL_SHOW_JOB_DISABLED`** | *(unset)* | Per-API killswitches. |
| **`RACK_VALIDATION_CT_RANGE`** | `01-18` | CT bracket for hostlists. |
| **`SINFO_OPTS`** | *(unset)* | Extra `sinfo` args (`native_status.py` + `rack_status.sh`). |
| **`SQUEUE_USER`**, **`USER`** | `USER` | `squeue` user filter (`native_status.py` + scripts). |
| **`RACK_NATIVE_CMSH_TIMEOUT_SEC`** | `120` | Native `cmsh` rack list. |
| **`RACK_NATIVE_DS_TIMEOUT_SEC`** | `300` | Native `cmsh ds` (fallback only: not run when the BCM pythoncm query reports device status). |
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

## 16a. Launch review binding (`RACK_LAUNCH_REVIEW`) — REQ-6

A validation launch takes a rack out of service and consumes real GPU hours, so REQ-6 requires that
the target the operator **reviewed** is the target that runs. The mechanism is a two-call protocol:

```
POST /api/launch/prepare        {"kind": "...", ...launch body}  -> resolved plan + plan_hash
POST /api/run-<tool>-async      {...launch body, "plan_hash": ...} -> re-resolves, compares, launches
```

The launch re-runs the same resolution and compares digests in constant time. A drained rack, a
superseded campaign, or a browser tab left open while someone switched clusters all change the
digest and earn a **409** carrying the new one, so the UI can re-present the changed plan.

| Value | Behaviour |
| :---- | :---- |
| `strict` *(default)* | A launch without a digest is refused; a mismatched digest is refused. |
| `warn` | A launch **without** a digest is allowed and logged; a **mismatched** digest is still refused. |
| `off` | The pre-2026-08-24 contract: no binding. |

**Why `strict` by default.** A permissive default would mean REQ-6 is unmet on a stock deployment,
which is the criticism the requirement exists to answer. The in-repo SPA does prepare→launch for
every one of these endpoints (`frontend/src/lib/launchReview.ts`), so the product's own UI needs no
configuration.

**Why `warn` exists.** A site with its own automation posting directly to these endpoints — a MOP
script, a cron job, an operator's `curl` — would otherwise break on upgrade. `warn` keeps those
launching while every unbound launch is logged, which turns the migration into a worklist rather
than a guess. Note what `warn` does *not* relax: a **mismatched** digest is still refused, because a
mismatch is not a caller that never learned the protocol — it is a caller whose reviewed target no
longer holds, which is exactly the case the requirement is about.

The endpoints covered are the ten legacy per-tool launches. `/api/run-validation-preset-async` is
**not** affected: it has had its own stronger binding since it was written (a digest over
hand-picked semantic fields rather than the resolved argv), and is deliberately not double-gated.

---

## 16b. Data lake egress (`RACK_LAKE_*`) — what leaves the site

The lake sink pushes validation-run provenance (parsed results, raw logs, the sbatch/conf/YAML that
defined a run) to `nvis-lakewriter`, and optionally a record of each API request. It is the
product's only **outbound** data path, so its settings are security settings.

| Variable | Default | Meaning |
| :---- | :---- | :---- |
| `RACK_LAKE_CAPTURE` | **`off`** | Request capture: `off`, `actions`, `all`. **Opt-in.** |
| `RACK_LAKE_URL` | `http://host.docker.internal:9410` | Lake writer endpoint. Use `https://` off-host. |
| `RACK_LAKE_CA_BUNDLE` | *(system trust)* | PEM bundle verifying an `https://` lake. |
| `RACK_LAKE_DISABLED` | *(unset = enabled)* | `1` disables the sink entirely, run pushes included. |
| `RACK_LAKE_CLUSTER` | active catalog cluster/customer | Cluster tag every record is filed under. |
| `RACK_LAKE_TIMEOUT` | `20` | Per-request timeout, seconds. |
| `RACK_LAKE_QUEUE_MAX` | `256` | Bounded async queue depth for request capture. |

**Request capture is opt-in, and that is a change.** It used to default to `actions`, so a
deployment that had never heard of the lake still captured ~100 mutating endpoints and posted them
to `host.docker.internal:9410`. Nothing usually listens there, which made it look harmless — the
protection was the environment rather than a decision.

**Credential-bearing endpoints are never captured, in any mode.** `POST /api/login` carries the
dashboard password in its body and `PUT /api/vault/nvftp-token` carries a token; both were captured
verbatim until 2026-08-24. `lake_sink.NEVER_CAPTURE` refuses them before the body is read, and
`RACK_LAKE_CAPTURE=all` cannot widen scope onto them. This follows the same reasoning as the audit
trail dropping query strings rather than filtering them: a credential is invisible to key-name
redaction as often as not, so the reliable control is not to take the data.

Everything that *is* captured is redacted twice — by key name (`password`, `token`, `secret`,
`api_key`, `auth`, `cookie`, `session`, …) and by value, reusing the same env-sourced secret list
`NVIS_LOG_SENSITIVE_KEYS` feeds to the log redaction filter.

**TLS is verified or it fails.** There is deliberately no skip-verify switch: an unverified TLS
connection to a provenance store authenticates nothing while looking secure in review, so a
misdirected endpoint would be indistinguishable from the real one. Consequences worth knowing:

- Switching `RACK_LAKE_URL` to `https://` without a trusted certificate makes posts **fail**, not
  silently downgrade. That is intended.
- For a self-signed or internal-CA lake, point `RACK_LAKE_CA_BUNDLE` at the CA.
  `bin/gen-tls-cert.py --mode ca` writes a suitable `ca.crt`.
- A `RACK_LAKE_CA_BUNDLE` path that does not exist raises rather than falling back to system trust —
  a typo must not quietly become an unverified connection.
- Plain HTTP to a **non-local** host logs one warning. Plaintext to `host.docker.internal` or
  loopback is not warned about: that is the same machine over Docker's bridge.

**Capture never blocks a response.** Request capture enqueues onto a bounded queue drained by a
daemon thread, because it runs inside `after_request` — a synchronous POST there would put the
lake's availability in the operator's latency path, up to `RACK_LAKE_TIMEOUT` per request. When the
queue is full, records are dropped **and counted**; `GET /api/lake/health` reports the depth and the
drop count, so loss is visible rather than silent. `push_run` remains synchronous for the
results-ingest path, which needs the honest status back.

---

## 16c. Artifact distribution (NVIS-P2P): the shared-state directories

The P2P gateway (`/api/p2p/*`) never calls the cluster's P2P services over the network. Two
directories under the app's state directory are the channel instead: the fleet-snapshot producer
(`deploy/p2p/scripts/fleet-snapshot.py`) writes into one, and the operations worker
(`deploy/p2p/scripts/operations-worker.py`) claims requests from the other. Both run where the P2P
instance lives, so each directory must be the **same path seen from both sides** — on a co-located
deployment, the state directory bind-mounted into the container at `/app/var`. Both are per
cluster: one snapshot file, and one operations subdirectory, per `cluster_id`.

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_P2P_SNAPSHOT_DIR`** | `/app/var/p2p-snapshots` | Where `GET /api/p2p/status` reads `<cluster_id>.json`, the fleet snapshot the producer writes. Point the producer's `--out` at the same directory. |
| **`RACK_P2P_SNAPSHOT_MAX_AGE`** | `900` (seconds) | A snapshot whose `generated_at` is older than this is treated as absent, and the view falls back to the unprovisioned state rather than showing stale coverage indefinitely. Set it comfortably above the producer's interval plus its run time: Nscale NC runs the producer every 15 minutes and its fleet audit of ~800 nodes takes minutes, so it sets `1800`. A value that is not a positive integer is ignored and the default applies. |
| **`RACK_P2P_OPERATIONS_DIR`** | `/app/var/p2p-operations` | Where `POST /api/p2p/operations` (and a campaign launch, which is one) enqueues `<cluster_id>/<op_id>.json`, and where the operation log viewer reads `<op_id>.log`. Point the worker's `--ops-dir` at `<this dir>/<cluster_id>`. |

All three are process-wide and read at import: changing one needs a restart, and the Settings screen
cannot set them.

---

## 17. Execution target: connection topology vs. the flat env contract

How NVIS reaches the cluster can be expressed two ways. The env vars below are the
**single-host** form and remain fully supported; a declared **topology** supersedes
them when present. See [`docs/EXECUTION_ARCHITECTURE.md`](EXECUTION_ARCHITECTURE.md) §7.

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_EXEC_MODE`** | `ssh` | **Deprecated — do not set in new configs; remove it from existing ones.** Logs a warning whenever present. Connections default to `ssh`; set it (`local`) only when the app runs directly on the target host. |
| **`RACK_SSH_HOST`** | *(unset)* | Head-node hostname / IP for the control route. |
| **`RACK_SSH_USER`** | `root` | SSH user. |
| **`RACK_SSH_KEY`** | `/run/secrets/ssh_key` | Private key path. Passed as `-i` only when the file exists. |
| **`RACK_SSH_PORT`** | `22` | SSH port. |
| **`RACK_SSH_OPTS`** | *(unset)* | Extra `ssh` flags (space-separated), e.g. `-J bastion`. Applied to the leg that leaves this machine, in both batch and interactive (terminal) paths. |
| **`RACK_DIRECT_NODE_SSH`** | *(unset)* | Set by the cluster overlay from **Projects → cluster → Connection → Reach compute nodes: Directly from the app server** (`connection.node_exec_direct`); set it in the environment only for a deployment without a catalog cluster. Truthy → the app opens its own ssh to a compute node instead of relaying through the control host, for the terminal and for every node command (`host_exec.run_on_nodes`: services restart, GPU reset). Faster, and it consumes none of the head's `MaxSessions` slots, but the app must hold a credential the node accepts. **No hostname pattern** — it applies to every node target on the deployment (it was previously gated on one site's tray regex and silently did nothing elsewhere). |
| **`RACK_NODE_SSH_USER`** | *(unset)* on a relay; the connection's SSH user (else **`root`**) on a direct leg | Account for the node leg, and the default depends on which leg. Relaying, unset means the hop inherits the control host's ssh user (`host_exec.py:798`). On a **direct** leg — the one `RACK_DIRECT_NODE_SSH` selects — there is no predecessor to inherit from, so it resolves to the cluster connection's own SSH user (whose key the leg uses), and to `root` only when that is unset too. Previously documented as simply *(unset)*, which is only half true and made this read like a setting with no default. It has one, which is why the launch preflight does **not** require it — but `root` is the wrong account at some sites (Nscale TX uses `cmods`), and a wrong value is not something a presence check can catch. |
| **`RACK_NODE_SUDO`** | *(unset)* | Set by the cluster overlay from **Projects → cluster → Connection → Node commands: Run with sudo -n** (`connection.node_sudo`). Truthy → every node command from `host_exec.run_on_nodes` runs as `sudo -n -E bash -c …`, on either route. For an account that is not root but has passwordless sudo on the nodes (Nscale NC connects as an operator account). |
| **`RACK_NODE_SSH_KEY`** | `RACK_SSH_KEY` | Key for a **direct** node leg. Ignored when relaying, since the control host supplies its own credentials. |
| **`RACK_SSH_MULTIPLEX`** | `1` | ControlMaster multiplexing; `0` opts out (see §6 of the architecture doc). Load-bearing for fan-out — a **stale master** degrades every hop and logs `disabling multiplexing` without this ever changing; recovery in §6.1. |
| **`RACK_SCRIPT_SSH_*`** | *(unset)* | Same fields for the privileged (`script`) route. Unset → both routes collapse to control. |
| **`RACK_SCRIPT_SUDO`** | *(unset)* | Truthy → privileged route runs under `sudo -n -E`. |
| **`RACK_REMOTE_BASE_PATH`** | *(unset)* | One tree on one host: `SCRIPTS_DIR` = `…/bin/scripts`, `DEFAULT_LOG_DIR` = `…/var/logs`. Surfaced as the `scripts` / `logs` role roots when no topology is declared. |
| **`RACK_FANOUT_MAX_WORKERS`** | `8` | Concurrency cap for workflow node fan-out (`workflow_engine.py`); keep ≤ the gateway's `MaxSessions`. |
| **`RACK_FS_DIR_CACHE_TTL_SEC`** | `30` | TTL for the `is_dir` cache (each miss is a round trip on a remote target). |

### When the flat form is not enough

It can only say *one hop to one host*, with scripts and logs in the same tree. A
deployment that needs a bastion in front of the head, or the log directory on a
different machine from the script tree, declares **endpoints** (`via` chains them,
N hops) and **roles** (`control`, `scripts`, `logs`, `bcm`, or any site-defined
name) on the cluster document instead — `GET/PUT /api/catalog/topology`. Roles may
carry a `root`, which is what `host_exec.role_root(...)` returns.

Env forwarding still applies over every hop: a knob only reaches a remote script if
its name matches a forwarded prefix (`RACK_`, `NEMO_`, `VALIDATION_`, `SQUEUE_`,
`NVIS_`, `PDSH_`, `NGC_`, `LLMB_`) or the `FORWARD_ENV_NAMES` list in
`backend/transport/factory.py`. Per-call values must be passed as `env_extra`, not
`env` — the latter is a local-subprocess parameter and is dropped when remote.

---

## Appendix A — Every `RACK_*` name referenced under `nvis-rack-status/` (sorted)

Use this as a checklist against **`app.py`** / **`native_status.py`**. Defaults and semantics are in §§4–14.

```
RACK_BRINGUP_CACHE_TTL_SEC
RACK_BRINGUP_DISABLED
RACK_BRINGUP_HOME
RACK_BRINGUP_REPORT_TIMEOUT
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
RACK_LAKE_CA_BUNDLE               # PEM bundle verifying an https:// lake; see §16b
RACK_LAKE_CAPTURE                 # request capture: off (default) | actions | all; see §16b
RACK_LAKE_CLUSTER                 # lake cluster tag; defaults to the active catalog cluster/customer, else "unknown-cluster"
RACK_LAKE_DISABLED
RACK_LAKE_QUEUE_MAX               # bounded async capture queue depth (default 256)
RACK_LAKE_TIMEOUT
RACK_LAKE_URL
RACK_LAUNCH_REVIEW                # reviewed-target binding: strict (default) | warn | off; see §16a
RACK_LLAMA_DISABLED
RACK_LLAMA_SCRIPT
RACK_LLAMA_TIMEOUT
RACK_LOG_ANALYSIS_CACHE_DISABLED
RACK_LOG_ANALYSIS_CACHE_MAX_FILES
RACK_LOG_ANALYSIS_SQUEUE_TIMEOUT_SEC
RACK_LOG_EXPORT_DISABLED
RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER
RACK_LOG_INLINE_MAX_BYTES
RACK_MULTIRACK_JOBS_FILE          # legacy; migrated into RACK_VALIDATION_ACTIVITIES_FILE
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
RACK_P2P_OPERATIONS_DIR
RACK_P2P_SNAPSHOT_DIR
RACK_P2P_SNAPSHOT_MAX_AGE
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
RACK_SLURM_BIN
RACK_SLURM_MODULE
RACK_SLURM_NODES_STATE_DISABLED
RACK_SLURM_NODES_STATE_DRY_RUN
RACK_SLURM_SBATCH
RACK_SLURM_SCONTROL
RACK_SLURM_SCONTROL_SHOW_JOB_DISABLED
RACK_SLURM_SCONTROL_TIMEOUT
RACK_SLURM_SACCT
RACK_SLURM_SCANCEL
RACK_SLURM_SCANCEL_DISABLED
RACK_SLURM_SINFO
RACK_SLURM_SINFO_R_DISABLED
RACK_SLURM_SKIP_MODULE_LOAD
RACK_SLURM_SRUN
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
RACK_VALIDATION_ACTIVITIES_FILE   # durable validation activity registry (default var/cache/validation_activities.json)
RACK_VALIDATION_CT_RANGE
RACK_VALIDATION_DISABLED
RACK_VALIDATION_SCRIPT
RACK_VALIDATION_TIMEOUT
RACK_WEB_ACTIVE_CLIENT_TTL_SEC
RACK_WEB_COOKIE_SECURE
RACK_WEB_GRAFANA_DASH_SLUG
RACK_WEB_GRAFANA_DASH_UID
RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS
RACK_WEB_GRAFANA_DEEPLINK_DISABLED
RACK_WEB_GRAFANA_EMBED_DISABLED
RACK_WEB_GRAFANA_PORT
RACK_WEB_GRAFANA_TIME_PAD_SEC
RACK_WEB_GRAFANA_TIMEZONE
RACK_WEB_GRAFANA_URI
RACK_WEB_GRAFANA_VAR_HOST
RACK_WEB_GRAFANA_VAR_RACK
RACK_WEB_HW_CHECK                 # set by hw_check_pdsh.sh on targets (see §9)
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

### Validation activities

Durable registry for validation-family launches and adopted external logs
(`validation_activities.py`, `GET/DELETE /api/validation-activities[/<id>]`,
`POST /api/validation-activities/adopt`). Canonical id is `activity_id` (UUID);
async endpoints still return the same value as `job_id`. Legacy
`/api/multirack-jobs` remains a compat alias for `scope=multirack`.

| Variable | Default | Effect |
|----------|---------|--------|
| **`RACK_VALIDATION_ACTIVITIES_FILE`** | `var/cache/validation_activities.json` | Durable activity store (fcntl flock, atomic rewrite, cap ~500). |
| **`RACK_MULTIRACK_JOBS_FILE`** | `var/cache/multirack_jobs.json` | Legacy path. If the new file is absent and this exists, records are migrated once into `validation_activities.json` (old file kept as `.bak`). |

Subprocess env may include **`NVIS_ACTIVITY_ID=<uuid>`** so wrappers can embed the activity id later.

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
