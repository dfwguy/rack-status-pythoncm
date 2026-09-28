---
description: Docker deployment, remote path config, and host command execution patterns
globs: host_exec.py,platforms/**/*.py,Dockerfile,docker-compose.yml,build-docker.sh,gunicorn.conf.py,config/**
alwaysApply: false
---

# Docker & Host Execution

## Command Execution

All host commands (Slurm, cmsh/maas, pdsh, shell scripts) MUST route through `host_exec.run_on_host()` or `host_exec.popen_on_host()`. Never call `subprocess.run()` directly for cluster commands.

```python
# GOOD
proc = host_exec.run_on_host(cmd, timeout=120)

# BAD
proc = subprocess.run(cmd, capture_output=True, text=True)
```

This bites hardest with `cwd`: a working directory on the cluster does not exist in the container, so a local subprocess fails before it runs anything.

Pass per-call vars as `env_extra`, not `env`. `env` is a local-subprocess argument and is dropped over SSH; `env_extra` is exported on the far side.

## Filesystem Access

Same rule for files. Anything that may live on the execution host goes through `host_exec`, never `pathlib`:

| Instead of | Use |
|---|---|
| `p.is_file()` / `p.is_dir()` | `host_exec.is_file(p)` / `host_exec.is_dir(p)` |
| `p.glob()` / `p.iterdir()` | `host_exec.stat_dir(p, patterns=…, maxdepth=…)` — one round trip, returns mtime+size |
| `p.read_text()` | `host_exec.read_text(p)` |
| `p.write_bytes()` | `host_exec.write_bytes(p, data)` |
| `p.mkdir(parents=True)` | `host_exec.makedirs(p)` |
| `p.resolve()` / `expanduser()` | `_rpath(p)` in `app.py` — textual, never touches the local filesystem |
| `shutil.copy2(a, b)` | a `cp` via `run_on_host` when both ends are on that host |

`.resolve()` on a `PurePosixPath` raises `AttributeError`, so a missed call site fails loudly instead of quietly answering about the wrong machine.

## Roles

A role names a place in the deployment's terms — `control`, `scripts`, `logs`, `bcm`, or any name a site invents — so call sites never hardcode which host that is:

```python
host_exec.run_on_host(cmd, cwd=_scripts_dir(), role="scripts")
host_exec.stat_dir(log_dir, patterns=("*.txt",), role="logs")
host_exec.role_root("logs")     # configured base dir, "" when unset
host_exec.describe_roles()      # per role: endpoint, hop path, root
```

Roles are declared per cluster in the catalog (`connection.endpoints` + `connection.roles`), editable at runtime via `GET/PUT /api/catalog/topology`. An endpoint's `via` names the endpoint it is reached through, so N hops is N links. An unknown role falls back to `control`. `role=` and the older `profile=` select from the same namespace.

Careful: `script` (the privileged root/pdsh trust domain) and `scripts` (where the script tree lives) are different axes despite the near-identical names.

## Environment Forwarding

In SSH mode, env vars prefixed `RACK_*`, `NEMO_*`, `VALIDATION_*`, `SQUEUE_*` are automatically exported before the remote command. To forward additional prefixes, edit `_FORWARD_ENV_PREFIXES` in `host_exec.py`.

## RACK_REMOTE_BASE_PATH

Single env var that resolves all script paths and the default log directory to the head node filesystem:
- `SCRIPTS_DIR` = `{RACK_REMOTE_BASE_PATH}/bin/scripts/`
- `DEFAULT_LOG_DIR` = `{RACK_REMOTE_BASE_PATH}/var/logs/`
- Individual `RACK_*_SCRIPT` env vars still override if set.

It is the single-host precursor of role roots: one tree on one host. When no topology is declared these are surfaced as the `scripts` and `logs` role roots anyway, so call sites can be written role-first either way. To place them on different hosts, declare roles.

## Script Existence Checks

Use `_script_exists(path)` instead of `path.is_file()` for scripts. In SSH mode it returns `True` (scripts are on the remote host). In local mode it checks the filesystem.

## Platform Providers

```python
plat = _get_platform()
proc = host_exec.run_on_host(plat.sinfo_cmd("-N -h"), timeout=120)
cmd = plat.pdsh_cmd(hosts, remote_cmd, fanout=32)
```

When adding new cluster commands: add method to `PlatformProvider` (base.py), implement in `BCMProvider` (bcm.py) and `MAASProvider` (maas.py).

## Docker Config

- `config/` on the host is mounted read-only at `/app/config`: `rackstatus.toml` (every setting;
  create the minimal one with `python3 backend/cli.py init-config`) and optional `tls/`. Only
  `config/rackstatus.toml.example` is tracked. There is no env file; `.env` holds only what Compose
  interpolates (bind address, host port, SSH key path).
- `build-docker.sh` — dev (demo), prod (config/ mount + rack-data volume), build, save, load, stop, logs
- Multi-stage Dockerfile: `node:24-slim` SPA build, `python:3.12-slim` docs build, `ubuntu:22.04` runtime
- The SPA is built with Vite `base: "/"`; `/spa/` is a Flask route serving the same app
- **HTTPS only.** `gunicorn.conf.py` sets `certfile`/`keyfile` from `startup_gate.ensure_tls()` when
  gunicorn loads it: `RACK_WEB_TLS_CERT`/`KEY`, else `config/tls/`, else a self-signed pair
  generated under `var/secrets/tls/`. Never decide TLS in the ENTRYPOINT — gunicorn fixes its
  listener before `on_starting`, which is how a first boot once served plain HTTP.
- The image's `HEALTHCHECK` is `bin/healthcheck.py` (HTTPS on loopback, any answer below 500).

## Auth in Docker

Nothing to configure, and nothing to switch off. The app authenticates against its own identity
store over SRP (`backend/identity/`); shared-token and PAM login were removed. The first start
with an empty store prints a one-time `admin` password to the container log. The public endpoints
are `web_auth.AUTH_BYPASS_ENDPOINTS`.

## Path Resolution

- There is no `is_remote()` predicate any more, and no code should branch on local vs remote. `host_exec` holds a connection and applies the right mechanism; `_resolve_log_dir()` validates through `host_exec.is_dir()`, which answers for whichever host owns the path.
- Use `host_exec.exec_kind()` (`"local"` / `"ssh"`) for diagnostics and status payloads only, never to choose a code path.
- All `_*_script_path()` functions resolve from `SCRIPTS_DIR`, which derives from `RACK_REMOTE_BASE_PATH` (or the `scripts` role root).
- The frontend Settings page persists `log_dir` to localStorage; it must be an absolute path on the host that owns the `logs` role.
