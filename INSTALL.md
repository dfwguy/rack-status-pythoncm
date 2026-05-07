# NVIS Rack Status — Docker Installation Guide

## Prerequisites

- Docker Engine 20.10+ (or Docker Desktop)
- An SSH key pair that can reach your head node (BCM or MAAS)
- The head node must be running `sshd` and have Slurm, cmsh/maas-cli, and pdsh available

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

### Option A: `docker run`

**BCM head node:**

```bash
docker run -d --name rack-status -p 5000:5000 \
  -e RACK_EXEC_MODE=ssh \
  -e RACK_PLATFORM=bcm \
  -e RACK_SSH_HOST=10.0.0.1 \
  -e RACK_SSH_USER=root \
  -e RACK_WEB_AUTH_MODE=token \
  -e RACK_WEB_AUTH_TOKEN=<your-password> \
  -v ~/.ssh/id_rsa:/run/secrets/ssh_key:ro \
  -v /path/to/logs:/app/var/logs \
  --restart unless-stopped \
  nvcr.io/your-org/nvis-rack-status:latest
```

**MAAS head node:**

```bash
docker run -d --name rack-status -p 5000:5000 \
  -e RACK_EXEC_MODE=ssh \
  -e RACK_PLATFORM=maas \
  -e RACK_SSH_HOST=10.0.0.1 \
  -e RACK_SSH_USER=root \
  -e RACK_WEB_AUTH_MODE=token \
  -e RACK_WEB_AUTH_TOKEN=<your-password> \
  -v ~/.ssh/id_rsa:/run/secrets/ssh_key:ro \
  -v /path/to/logs:/app/var/logs \
  --restart unless-stopped \
  nvcr.io/your-org/nvis-rack-status:latest
```

### Option B: Docker Compose

1. Download the compose file and environment template:

```bash
curl -O https://raw.githubusercontent.com/your-org/nvis-rack-status/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/your-org/nvis-rack-status/main/.env.example
```

2. Create your local configuration:

```bash
cp .env.example .env
```

3. Edit `.env` with your site-specific values:

```bash
RACK_EXEC_MODE=ssh
RACK_PLATFORM=bcm
RACK_SSH_HOST=10.0.0.1
RACK_SSH_USER=root
RACK_SSH_KEY_PATH=~/.ssh/id_rsa
RACK_WEB_AUTH_MODE=token
RACK_WEB_AUTH_TOKEN=<your-password>
RACK_LOG_DIR=/path/to/logs
```

4. Launch:

```bash
docker compose up -d
```

Open **http://localhost:5000/spa/** in your browser.

## Accessing the Dashboard

| URL | Description |
|---|---|
| `http://<host>:5000/spa/` | React SPA (primary UI) |
| `http://<host>:5000/` | Legacy HTML dashboard |
| `http://<host>:5000/api/status` | JSON API |

Log in with the password set in `RACK_WEB_AUTH_TOKEN`. If authentication is
disabled (`RACK_WEB_AUTH_DISABLED=1`), no login is required.

## Configuration Reference

### Connection

| Variable | Default | Description |
|---|---|---|
| `RACK_EXEC_MODE` | `local` | `local` = run commands directly, `ssh` = SSH to head node |
| `RACK_PLATFORM` | `bcm` | `bcm` (Base Command Manager) or `maas` (Canonical MAAS) |
| `RACK_SSH_HOST` | — | Head-node hostname or IP **(required in ssh mode)** |
| `RACK_SSH_USER` | `root` | SSH user on the head node |
| `RACK_SSH_PORT` | `22` | SSH port |

### Authentication

| Variable | Default | Description |
|---|---|---|
| `RACK_WEB_AUTH_MODE` | auto | `token` (shared password) or `pam` (system PAM). Auto-selects `token` in ssh mode, `pam` in local mode |
| `RACK_WEB_AUTH_TOKEN` | — | Login password when using token mode |
| `RACK_WEB_AUTH_DISABLED` | `0` | Set to `1` to disable login entirely |
| `RACK_WEB_SECRET_KEY` | random | Set a fixed value for stable sessions across container restarts |

### Volumes

| Container Path | Purpose |
|---|---|
| `/run/secrets/ssh_key` | SSH private key (mount read-only) |
| `/app/var/logs` | Rack validation log directory |
| `/app/var/cache` | Internal caches (use a named volume for persistence) |

### Other

| Variable | Default | Description |
|---|---|---|
| `RACK_STATUS_DEMO` | `0` | `1` = synthetic demo data, no cluster access needed |
| `PORT` | `5000` | Flask listen port inside the container |
| `RACK_WEB_PORT` | `5000` | Host-side port (compose only) |

## SSH Key Setup

The container connects to the head node over SSH to execute Slurm, cmsh/maas,
and pdsh commands. You need a key pair where:

1. The **private key** is mounted into the container at `/run/secrets/ssh_key`
2. The **public key** is in `~/.ssh/authorized_keys` on the head node for the
   target user (default `root`)

To generate a dedicated key pair:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/rack_status_key -N "" -C "nvis-rack-status"
ssh-copy-id -i ~/.ssh/rack_status_key.pub root@<head-node>
```

Then set `RACK_SSH_KEY_PATH=~/.ssh/rack_status_key` in your `.env` or use
`-v ~/.ssh/rack_status_key:/run/secrets/ssh_key:ro` in your `docker run`
command.

## Building the Image Locally

If you have the source code and want to build instead of pulling a pre-built
image:

```bash
git clone https://github.com/your-org/nvis-rack-status.git
cd nvis-rack-status
docker build -t nvis-rack-status:local .
```

Then replace `nvcr.io/your-org/nvis-rack-status:latest` with
`nvis-rack-status:local` in any of the commands above.

## Updating

```bash
docker compose pull
docker compose up -d
```

Or with `docker run`:

```bash
docker pull nvcr.io/your-org/nvis-rack-status:latest
docker stop rack-status && docker rm rack-status
# re-run the original docker run command
```

## Troubleshooting

**Container starts but dashboard shows no racks:**
- Verify `RACK_SSH_HOST` is reachable from inside the container:
  `docker exec rack-status ssh -i /run/secrets/ssh_key root@<host> hostname`
- Check that `cmsh` (BCM) or `maas` (MAAS) is available on the head node
- Check container logs: `docker logs rack-status`

**SSH connection refused:**
- Confirm `sshd` is running on the head node
- Confirm the mounted SSH key matches an authorized key on the head node
- Try a manual SSH: `docker exec -it rack-status ssh -i /run/secrets/ssh_key -o StrictHostKeyChecking=no root@<host> whoami`

**Login page appears but password is rejected:**
- Ensure `RACK_WEB_AUTH_TOKEN` is set and matches what you type at login
- Or set `RACK_WEB_AUTH_DISABLED=1` to bypass login

**Demo mode shows data but production mode is empty:**
- Confirm `RACK_STATUS_DEMO=0` (or unset) in production
- Confirm the SSH tunnel works (see above)
