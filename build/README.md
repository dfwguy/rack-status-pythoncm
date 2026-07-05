# NVIS Rack Status — Container Build & Run Guide

How to **build**, **package**, and **run** the NVIS Rack Status Docker container, and
the full set of parameters used to configure it.

The image is a multi-stage build (React SPA → Flask runtime on Ubuntu 22.04). It
serves the SPA and the `/api/*` backend on **port 5000** and mirrors the repo layout
inside the container (`/app/backend` for Python, `/app/{bin,static,var}` + `/app/help_content.json`
for runtime assets).

---

## TL;DR

```bash
# Demo (no cluster, no auth) — great for a local look
docker run -d --name rack-status -p 5050:5000 \
  -e RACK_STATUS_DEMO=1 -e RACK_WEB_AUTH_DISABLED=1 \
  -v rack-data:/app/var nvis-rack-status:latest
# → http://localhost:5050/spa/

# Production (SSH to a real head node, token auth)
docker run -d --name rack-status -p 5000:5000 \
  --env-file rack-config.env \
  -e RACK_SSH_HOST=10.0.0.1 \
  -e RACK_WEB_AUTH_TOKEN=<password> \
  -v ~/.ssh/rack_status_key:/run/secrets/ssh_key:ro \
  --restart unless-stopped \
  nvis-rack-status:latest
# → http://<host>:5000/spa/
```

---

## Building the image

```bash
# Helper (recommended) — builds nvis-rack-status:local
./build-docker.sh build

# Or directly
docker build -t nvis-rack-status:local .
```

### Air-gapped bundle (offline hosts)

```bash
./build-docker.sh save           # build + docker save → dist/nvis-rack-status-<version>.tar.gz (+ .sha256)
./build-docker.sh load <tar>     # verify checksum + docker load on the offline host
```

`save` env knobs: `RACK_IMAGE_TAG` (default = `version.py` VERSION), `RACK_IMAGE_OUT_DIR`
(default `dist`), `RACK_IMAGE_COMPRESS` (`1` gzip / `0` plain).

**From CI:** every release publishes the same versioned tarball to the project's
**Package Registry** (`…/packages/generic/nvis-rack-status-image/<version>/`) and links
it on the **GitLab Release** page. See [`INSTALL.md`](../INSTALL.md#air-gapped-deployment-offline-image-bundle).

---

## Run modes

| Mode | When | Key flags |
|---|---|---|
| **Demo / dev** | Local demo, no cluster | `RACK_STATUS_DEMO=1`, `RACK_WEB_AUTH_DISABLED=1`, `RACK_EXEC_MODE=local` |
| **Production** | Real GB200/GB300 cluster | `RACK_EXEC_MODE=ssh`, `RACK_SSH_HOST=…`, token auth, SSH key mount |

The simplest way to launch either is the helper from the repo root:

```bash
./build-docker.sh             # dev mode (demo, no auth, port 5050)
RACK_SSH_HOST=10.0.0.1 RACK_WEB_AUTH_TOKEN=secret ./build-docker.sh prod
```

---

## Parameters

All configuration is via environment variables (`-e VAR=value` or `--env-file rack-config.env`).
This lists the **common** ones; for the exhaustive catalog (timeouts, per-feature disable
flags, Slurm/Grafana knobs) see [`docs/ENVIRONMENT.md`](../docs/ENVIRONMENT.md).

### Core / server

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5000` | Flask listen port **inside** the container (map with `-p`). |
| `RACK_STATUS_DEMO` | `0` | `1` = synthetic demo data, no cluster access needed. |
| `RACK_WEB_VERSION` | `dev` | Version string shown in the UI (`/api/app-stats`). |
| `PROJECT_NAME` / `RACK_WEB_PROJECT_NAME` | — | Optional title suffix shown in the UI. |

### Execution & platform

| Variable | Default | Description |
|---|---|---|
| `RACK_EXEC_MODE` | `local` | `local` = run commands in-container; `ssh` = tunnel to the head node. |
| `RACK_PLATFORM` | `bcm` | `bcm` (Base Command Manager) or `maas` (Canonical MAAS). |
| `RACK_REMOTE_BASE_PATH` | — | Head-node deploy path (ssh mode). All script paths + the default log dir derive from it (`{base}/bin/scripts/`, `{base}/var/logs/`). |

### SSH (cluster access — `RACK_EXEC_MODE=ssh`)

| Variable | Default | Description |
|---|---|---|
| `RACK_SSH_HOST` | — | Head-node hostname/IP **(required in ssh mode)**. |
| `RACK_SSH_USER` | `root` | SSH user on the head node. |
| `RACK_SSH_PORT` | `22` | SSH port. |
| `RACK_SSH_KEY` | `/run/secrets/ssh_key` | Path **inside the container** to the private key (mount it read-only). |

> Env vars prefixed `RACK_*`, `NEMO_*`, `VALIDATION_*`, `SQUEUE_*` are auto-forwarded to
> the remote SSH session.

### Authentication

| Variable | Default | Description |
|---|---|---|
| `RACK_WEB_AUTH_MODE` | auto | `token` (shared password) or `pam` (system PAM). Auto-selects `token` in ssh mode. |
| `RACK_WEB_AUTH_TOKEN` | — | Login password for token mode. |
| `RACK_WEB_AUTH_DISABLED` | `0` | `1` = no login at all (use only for demo). |
| `RACK_WEB_SECRET_KEY` | random | Set a fixed value for stable sessions across restarts. |

### AI assistant (optional)

| Variable | Default | Description |
|---|---|---|
| `RACK_AI_API_KEY` | — | API key (`nvapi-…`) to enable the chat widget. Unset = demo/canned answers. |
| `RACK_AI_ENDPOINT` | NVIDIA NIM | Chat completions endpoint. |
| `RACK_AI_MODEL` | — | Model id (e.g. `nvidia/nvidia/nemotron-3-super-v3`). |

### Cluster-specific (optional)

| Variable | Default | Description |
|---|---|---|
| `NEMO_SQSH_SRC` | — | Path to the NeMo `.sqsh` container image on the head node. |
| `RACK_SLURM_SKIP_MODULE_LOAD` | `0` | `1` if `sinfo`/`squeue` are already on `PATH`. |
| `RACK_VALIDATION_CT_RANGE` | `01-18` | Tray range per rack (e.g. `01-08` for 8-node racks). |
| `VALIDATION_TEST` | — | Tests that must pass (e.g. `nccl_allreduce,llm`). |

---

## Volumes

| Container path | Purpose |
|---|---|
| `/app/var` | Persist caches, logs, workflow runs, secrets. Use a named volume (e.g. `-v rack-data:/app/var`). |
| `/run/secrets/ssh_key` | SSH private key for ssh mode (mount read-only: `:ro`). |
| `/app/var/logs` | Local rack validation log dir (only needed in `local` mode). |

## Ports

The container listens on **5000**. Map it to any host port: `-p 5050:5000`.

---

## Examples

```bash
# 1) Demo on port 5050
docker run -d --name rack-status -p 5050:5000 \
  -e RACK_STATUS_DEMO=1 -e RACK_WEB_AUTH_DISABLED=1 \
  -v rack-data:/app/var nvis-rack-status:latest

# 2) Production with a config file (edit rack-config.env first)
docker run -d --name rack-status -p 5000:5000 \
  --env-file rack-config.env \
  -e RACK_SSH_HOST=10.0.0.1 \
  -e RACK_WEB_AUTH_TOKEN=changeme \
  -v ~/.ssh/rack_status_key:/run/secrets/ssh_key:ro \
  --restart unless-stopped \
  nvis-rack-status:latest

# 3) Via SSH jump host (tunnel local 2222 → head node 22)
ssh -f -N -L 2222:<head-node-internal-ip>:22 user@jump-host
docker run -d --name rack-status -p 5000:5000 \
  --env-file rack-config.env \
  -e RACK_SSH_HOST=host.docker.internal \
  -e RACK_SSH_PORT=2222 \
  -e RACK_WEB_AUTH_TOKEN=changeme \
  -v ~/.ssh/key:/run/secrets/ssh_key:ro \
  --add-host=host.docker.internal:host-gateway \
  nvis-rack-status:latest
```

Generate a dedicated SSH key pair for the container:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/rack_status_key -N "" -C "nvis-rack-status"
ssh-copy-id -i ~/.ssh/rack_status_key.pub root@<head-node>
```

---

## Accessing & health

| URL | Description |
|---|---|
| `http://<host>:<port>/spa/` | React SPA (primary UI). |
| `http://<host>:<port>/` | Legacy HTML dashboard. |
| `http://<host>:<port>/api/status` | JSON status API (handy for readiness checks). |

```bash
# Tail logs / stop (helper)
./build-docker.sh logs
./build-docker.sh stop
```

---

## See also

- [`INSTALL.md`](../INSTALL.md) — Docker deployment, air-gapped bundle, SSH setup, troubleshooting.
- [`docs/ENVIRONMENT.md`](../docs/ENVIRONMENT.md) — exhaustive environment-variable reference.
- [`rack-config.env`](../rack-config.env) — editable production config template.
- [`build-docker.sh`](../build-docker.sh) — build / run / save / load helper.
