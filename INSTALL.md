# NVIS Rack Status — Docker Installation Guide

## Prerequisites

- Docker Engine 20.10+ (or Docker Desktop)
- A dedicated non-root SSH identity that can reach your head node (BCM or MAAS)
- A pinned `known_hosts` file containing the approved head-node host key
- The head node must be running `sshd` and have Slurm, cmsh/maas-cli, and pdsh available
- The nvis-rack-status scripts must be deployed on the head node (see [Deploying Scripts](#deploying-scripts))

## Quick Start (Demo Mode)

Run the dashboard with synthetic data — no cluster access required:

```bash
docker run -d --name rack-status -p 5000:5000 \
  -e RACK_STATUS_DEMO=1 \
  -e RACK_WEB_AUTH_DISABLED=1 \
  nvcr.io/your-org/nvis-rack-status:latest
```

Open **http://localhost:5000/spa/** in your browser.

## Production Deployment

### Option A: Config file (recommended)

1. Edit `rack-config.env` with your site settings:

```bash
# Key settings to change:
RACK_REMOTE_BASE_PATH=/home/cmsupport/nvis-rack-status  # path on head node
RACK_EXEC_MODE=ssh
RACK_PLATFORM=bcm
RACK_SSH_USER=cmods                 # explicit non-root service identity
RACK_SLURM_SKIP_MODULE_LOAD=1      # if sinfo/squeue are on PATH
NEMO_SQSH_SRC=/path/to/nemo.sqsh   # NeMo container image on head node
```

2. Create a dashboard token file without putting the token in shell history or
   the container environment. The file must not be a symlink:

```bash
install -d -m 0700 deploy-secrets
umask 077
openssl rand -base64 32 > deploy-secrets/web-auth-token
chmod 0600 deploy-secrets/web-auth-token
```

3. Launch:

```bash
docker run -d --name rack-status -p 5000:5000 \
  --env-file rack-config.env \
  -e RACK_SSH_HOST=<head-node-ip> \
  --mount type=bind,src="$PWD/deploy-secrets/web-auth-token",dst=/run/secrets/rack-web-auth-token,readonly \
  -e RACK_WEB_AUTH_TOKEN_FILE=/run/secrets/rack-web-auth-token \
  -v ~/.ssh/id_rsa:/run/secrets/ssh_key:ro \
  -v ~/.ssh/known_hosts:/run/secrets/known_hosts:ro \
  --restart unless-stopped \
  nvis-rack-status:local
```

The `RACK_REMOTE_BASE_PATH` variable is the key — it tells the container where
the scripts and log directory live on the head node. All script paths resolve as
`{RACK_REMOTE_BASE_PATH}/bin/scripts/<script>.sh` and the default log directory
resolves as `{RACK_REMOTE_BASE_PATH}/var/logs/`.

### Option B: Docker Compose

```bash
# Edit rack-config.env, then provide only the host-side secret path to Compose.
export RACK_WEB_AUTH_TOKEN_PATH="$PWD/deploy-secrets/web-auth-token"
docker compose config >/tmp/rack-status-compose.rendered.yml
docker compose up -d
```

The secret value is mounted at `/run/secrets/rack-web-auth-token` and is not
present in `docker inspect` environment output. `RACK_WEB_AUTH_TOKEN` remains a
legacy rolling-upgrade fallback; remove it from the environment after verifying
file-backed login and rotate any token previously exposed through container
configuration.

### Option C: build-docker.sh helper

```bash
# Edit rack-config.env first
RACK_SSH_HOST=10.0.0.1 \
RACK_WEB_AUTH_TOKEN_FILE="$PWD/deploy-secrets/web-auth-token" \
./build-docker.sh prod
```

## Deploying Scripts to the Head Node

The container executes scripts on the head node via SSH. Copy the project to
a shared filesystem path accessible by compute nodes:

```bash
# On your workstation
tar czf nvis-deploy.tar.gz backend/ bin/ static/ help_content.json var/

# Pin the verified host key out of band, then use the checksum-based deployer.
export RACK_SSH_HOST=<head-node>
export RACK_SSH_USER=cmods
export RACK_SSH_KEY="$HOME/.ssh/rack_status_key"
export RACK_KNOWN_HOSTS_PATH="$HOME/.ssh/rack_status_known_hosts"
bin/deploy-scripts.sh --dry-run
bin/deploy-scripts.sh
```

Then set `RACK_REMOTE_BASE_PATH=/home/cmsupport/nvis-rack-status` in `rack-config.env`.

## Accessing the Dashboard

| URL | Description |
|---|---|
| `http://<host>:5000/spa/` | React SPA (primary UI) |
| `http://<host>:5000/` | Legacy HTML dashboard |
| `http://<host>:5000/api/status` | JSON API |

Log in with the password stored in `RACK_WEB_AUTH_TOKEN_FILE` (or the legacy
`RACK_WEB_AUTH_TOKEN`). If authentication is
disabled (`RACK_WEB_AUTH_DISABLED=1`), no login is required.

## Configuration Reference

### Remote Path

| Variable | Default | Description |
|---|---|---|
| `RACK_REMOTE_BASE_PATH` | — | Base directory on the head node where nvis-rack-status is deployed. All script paths and the default log directory derive from this single variable. |

### Connection

| Variable | Default | Description |
|---|---|---|
| `RACK_EXEC_MODE` | `local` | `local` = run commands directly, `ssh` = SSH to head node |
| `RACK_PLATFORM` | `bcm` | `bcm` (Base Command Manager) or `maas` (Canonical MAAS) |
| `RACK_SSH_HOST` | — | Head-node hostname or IP **(required in ssh mode)** |
| `RACK_SSH_USER` | — | Explicit non-root SSH user on the head node |
| `RACK_SSH_PORT` | `22` | SSH port |
| `RACK_KNOWN_HOSTS_PATH` | — | Host-side pinned `known_hosts` file used by deployment tooling |

### Authentication

| Variable | Default | Description |
|---|---|---|
| `RACK_WEB_AUTH_MODE` | auto | `token` (shared password) or `pam` (system PAM). Auto-selects `token` in ssh mode |
| `RACK_WEB_AUTH_TOKEN_FILE` | — | Preferred in-container path to a read-only token secret |
| `RACK_WEB_AUTH_TOKEN_PATH` | — | Host path supplied to Compose; mode 0400/0600, regular file, not a symlink |
| `RACK_WEB_AUTH_TOKEN` | — | Legacy environment fallback; visible to container inspectors |
| `RACK_WEB_AUTH_DISABLED` | `0` | Set to `1` to disable login entirely |
| `RACK_WEB_SECRET_KEY` | random | Set a fixed value for stable sessions across container restarts |

### Cluster-Specific

| Variable | Default | Description |
|---|---|---|
| `NEMO_SQSH_SRC` | — | Path to NeMo container image (`.sqsh`) on the head node |
| `RACK_SLURM_SKIP_MODULE_LOAD` | `0` | Set to `1` if Slurm binaries are already on PATH |
| `RACK_VALIDATION_CT_RANGE` | `01-18` | Tray range per rack (e.g. `01-08` for 8-node racks) |
| `VALIDATION_TEST` | — | Tests that must pass (e.g. `nccl_allreduce,llm`) |

### Volumes

| Container Path | Purpose |
|---|---|
| `/run/secrets/ssh_key` | SSH private key (mount read-only) |
| `/app/var/logs` | Local rack validation log directory (only needed in local mode) |
| `/app/var/cache` | Internal caches (use a named volume for persistence) |

### Other

| Variable | Default | Description |
|---|---|---|
| `RACK_STATUS_DEMO` | `0` | `1` = synthetic demo data, no cluster access needed |
| `PORT` | `5000` | Flask listen port inside the container |

## SSH Key Setup

The container connects to the head node over SSH. You need a key pair where:

1. The **private key** is mounted into the container at `/run/secrets/ssh_key`
2. The **public key** is in `~/.ssh/authorized_keys` on the head node

To generate a dedicated key pair:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/rack_status_key -N "" -C "nvis-rack-status"
ssh-copy-id -i ~/.ssh/rack_status_key.pub cmods@<head-node>
```

Then mount it: `-v ~/.ssh/rack_status_key:/run/secrets/ssh_key:ro`

### SSH via Jump Host

If the head node requires a jump host, set up an SSH tunnel first:

```bash
# Start tunnel (local port 2222 -> head node port 22 via jump host)
ssh -f -N -L 2222:<head-node-internal-ip>:22 user@jump-host

# Run container pointing to the tunnel
docker run -d --name rack-status -p 5000:5000 \
  --env-file rack-config.env \
  -e RACK_SSH_HOST=host.docker.internal \
  -e RACK_SSH_PORT=2222 \
  --mount type=bind,src="$PWD/deploy-secrets/web-auth-token",dst=/run/secrets/rack-web-auth-token,readonly \
  -e RACK_WEB_AUTH_TOKEN_FILE=/run/secrets/rack-web-auth-token \
  -v ~/.ssh/key:/run/secrets/ssh_key:ro \
  -v ~/.ssh/known_hosts:/run/secrets/known_hosts:ro \
  --add-host=host.docker.internal:host-gateway \
  nvis-rack-status:local
```

## Building the Image Locally

```bash
git clone <repo-url>
cd nvis-rack-status
docker build -t nvis-rack-status:local .
```

Or use the helper: `./build-docker.sh build`

## Air-Gapped Deployment (offline image bundle)

For hosts with **no internet/registry access**, package the fully-built image into a
single tarball, copy it over, then `docker load` + `docker run` it — no build or pull
needed on the target.

### 1. Produce the image bundle

**Locally** (uses `docker build` + `docker save`):

```bash
./build-docker.sh save
# → dist/nvis-rack-status-<version>.tar.gz
#   dist/nvis-rack-status-<version>.tar.gz.sha256
```

Useful overrides: `RACK_IMAGE_TAG=1.2.3`, `RACK_IMAGE_OUT_DIR=/tmp/out`,
`RACK_IMAGE_COMPRESS=0` (skip gzip).

**From CI/CD (published with each release)**: on the release commit, the `docker-save`
job builds the bundle tagged with the release **VERSION** (from `version.py`), and the
`release:publish-bundle` job uploads it to the project's **Package Registry** and links
it on the **GitLab Release** page. Download it by version from either:

- the Release page → **Assets** (`nvis-rack-status-<version>.tar.gz` + `.sha256`), or
- the Package Registry: `…/packages/generic/nvis-rack-status-image/<version>/nvis-rack-status-<version>.tar.gz`

Both CI jobs use Kaniko's `--tarPath` (the daemonless equivalent of `docker save`, since
the CI runners have no Docker daemon). `docker-save` can also be triggered **manually** on
any other pipeline to produce an ad-hoc bundle as a downloadable job artifact.

### 2. Transfer and run on the air-gapped host

```bash
# Copy the tarball (and checksum) to the offline host by your approved method
sha256sum -c nvis-rack-status-<version>.tar.gz.sha256   # verify integrity

docker load -i nvis-rack-status-<version>.tar.gz        # import (gz auto-detected)

# Demo (no cluster, no auth):
docker run -d --name rack-status -p 5000:5000 \
  -e RACK_STATUS_DEMO=1 -e RACK_WEB_AUTH_DISABLED=1 \
  -v rack-data:/app/var nvis-rack-status:<version>

# Or load with the helper (verifies the checksum if present):
./build-docker.sh load nvis-rack-status-<version>.tar.gz
```

For a real cluster, swap the demo flags for the SSH/auth env described in
[Configuration Reference](#configuration-reference) (e.g. `--env-file rack-config.env`,
`RACK_SSH_HOST`, `RACK_WEB_AUTH_TOKEN_FILE`, and the SSH/known-hosts mounts).

## Running Tests

```bash
# Backend (run from repo root)
pip install pytest
python -m pytest backend/tests/ -v

# Frontend
cd frontend && npm test
```

## Updating

```bash
docker compose pull && docker compose up -d
```

Or with `docker run`:

```bash
docker pull nvcr.io/your-org/nvis-rack-status:latest
docker stop rack-status && docker rm rack-status
# re-run the docker run command
```

## Troubleshooting

**Container starts but dashboard shows no racks:**
- Verify `RACK_SSH_HOST` is reachable with the configured non-root identity and pinned host key.
- Check `RACK_REMOTE_BASE_PATH` points to where scripts are deployed on the head node
- Check that `cmsh` (BCM) or `maas` (MAAS) is available on the head node
- Check container logs: `docker logs rack-status`

**Scripts fail with "No such file or directory":**
- Verify `RACK_REMOTE_BASE_PATH` is set correctly in `rack-config.env`
- Verify scripts exist through the approved non-root SSH profile at `<RACK_REMOTE_BASE_PATH>/bin/scripts/`.

**SSH connection refused:**
- Confirm `sshd` is running on the head node
- Confirm the mounted SSH key matches an authorized key on the head node
- For jump hosts, verify the SSH tunnel is active: `lsof -i :<tunnel-port>`

**Login page appears but password is rejected:**
- Ensure the mounted `RACK_WEB_AUTH_TOKEN_FILE` is non-empty and matches what you type at login
- Or set `RACK_WEB_AUTH_DISABLED=1` to bypass login

**NEMO container fails to start (pyxis/enroot errors):**
- Ensure `enroot` and `pyxis` are installed on compute nodes
- Ensure `libnvidia-container` (`nvidia-container-cli`) is installed
- Check disk space on compute nodes: container extraction needs ~20GB
- Verify `NEMO_SQSH_SRC` points to a valid `.sqsh` file accessible from compute nodes

**Log directory returns 400 error:**
- In SSH mode, `RACK_REMOTE_BASE_PATH` sets the default log dir automatically
- You can also change the log directory in the Settings page (persisted in browser)
- The path must exist on the head node, not inside the container

**Environment variables not reaching scripts:**
- Variables prefixed with `RACK_*`, `NEMO_*`, `VALIDATION_*`, and `SQUEUE_*` are automatically forwarded to the remote SSH session
- Add custom prefixes by modifying `_FORWARD_ENV_PREFIXES` in `backend/host_exec.py`
