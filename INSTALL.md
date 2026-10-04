# NVIS Rack Status — Docker Installation Guide

## Prerequisites

- Docker Engine 20.10+ (or Docker Desktop) with Compose v2
- Python 3.9+ on the Docker host for the one-line config generator (standard library only), or
  run it inside the image instead — see below
- An SSH key pair that can reach your head node (BCM or MAAS)
- On the head node: `sshd`, Slurm (unless the cluster uses PythonCM), `cmsh` or `maas`, and `pdsh`
- The validation scripts deployed on the head node — see [Deploying scripts](#deploying-scripts-to-the-head-node)

The dashboard is **HTTPS only**. There is no plain-HTTP mode and no way to run without signing in.

## Quick start (demo mode, no cluster)

```bash
docker run -d --name rack-status -p 127.0.0.1:5050:5000 \
  -e RACK_STATUS_DEMO=1 \
  -v rack-data:/app/var \
  nvis-rack-status:<version>
```

Open **https://localhost:5050/**. The first start generates a self-signed certificate, so the
browser warns once; accept it. Sign in as `admin` with the one-time password from the log:

```bash
docker logs rack-status 2>&1 | grep -A6 "ONE-TIME ADMINISTRATOR"
```

(Host port 5050 rather than 5000 because macOS answers `:5000` with the AirPlay Receiver.)

## Production deployment

### Where configuration lives

| File | Read by | Holds |
|---|---|---|
| `config/rackstatus.toml` | the application, at launch | every setting the app reads. Start from the minimal file below. |
| `config/tls/server.crt`, `server.key` | the application, at launch | *optional* — your own certificate. Without it, a self-signed one is generated. |
| `.env` | Docker Compose, for `${...}` | only the bind address, host port and SSH key path. Copy `.env.example`. |

`config/` is mounted read-only at `/app/config`. Only `config/rackstatus.toml.example` is tracked
in git; your `rackstatus.toml` and certificate stay out of shared history.

**Create the minimal configuration.** Every setting except the head node has a working default:

```bash
python3 backend/cli.py init-config --ssh-host <head-node>
```

No Python on the host? Run the same command inside the image:

```bash
docker run --rm -v "$PWD/config:/app/config" --entrypoint python3 nvis-rack-status:<version> \
  backend/cli.py init-config --ssh-host <head-node>
```

Either way it writes `config/rackstatus.toml` with one live line. To change anything else, copy its line
from `config/rackstatus.toml.example` (every setting, with its description) and uncomment it. The
full list of what the app reads, including the defaults, is the generated
[settings reference](docs/reference/configuration/settings.md). A real environment variable
overrides the file.

If a setting cannot be guessed, the app **refuses to start** and names every missing one at once —
today that is the SSH host (unless in demo mode).

> **After the first start, the connection belongs to the catalog.** The first boot creates a
> project and cluster from this file; from then on the cluster's host, user, key and base path come
> from **Projects & Clusters** in the UI and outrank the file. Change the target there, not here.

### Upgrading from `rack-config.env`

Deployments configured before this layout kept their settings in `rack-config.env`, which the
container no longer reads. Convert it once, before upgrading:

```bash
python3 backend/cli.py init-config --from-env-file rack-config.env
```

Each setting is carried into `config/rackstatus.toml` under its section. The command lists every
key it does not carry and why — secrets (set those in the environment), and the few that cannot
take effect from the file (`PORT`, and the TLS and Compose values). Nothing is dropped silently.

### TLS

Put your certificate and key at `config/tls/server.crt` and `config/tls/server.key` and restart.
Without them, the first start generates a self-signed pair under `/app/var/secrets/tls/` on the
data volume, so it — and the browser exception you accept for it — survives restarts and
recreates. The SAN must include `localhost`, because operators reach the dashboard through an
SSH tunnel; `bin/gen-tls-cert.py` issues such a certificate.

### Option A: Docker Compose (recommended)

```bash
cp .env.example .env                 # bind address, host port, SSH key path
python3 backend/cli.py init-config --ssh-host <head-node>
bin/write-build-info.sh              # stamps the build identity the dashboard shows
docker compose build
docker compose up -d
NVIS_CONTAINER=$(docker compose ps -q rack-status) bash bin/verify-deploy.sh
```

The port is published on `127.0.0.1` by default; reach it through an SSH tunnel. Set
`RACK_WEB_BIND_ADDR` in `.env` only to expose it deliberately.

### Option B: `build-docker.sh`

```bash
RACK_SSH_HOST=<head-node> ./build-docker.sh prod
```

Creates `config/rackstatus.toml` from `RACK_SSH_HOST` if it does not exist yet (it never overwrites
one), builds the image and runs it with `config/`, the SSH key and the `rack-data` volume mounted.

### Option C: plain `docker run`

```bash
docker run -d --name rack-status -p 127.0.0.1:5050:5000 \
  -v "$PWD/config:/app/config:ro" \
  -v ~/.ssh/rack_status_key:/run/secrets/ssh_key:ro \
  -v rack-data:/app/var \
  --restart unless-stopped \
  nvis-rack-status:<version>
```

**Always mount `/app/var` on a volume.** It holds the identity store (your administrator), the
signing keys, the generated certificate, the catalog and the run history; without it, recreating
the container loses all of them.

## Deploying scripts to the head node

The container runs validation scripts on the head node over SSH, from
`RACK_REMOTE_BASE_PATH`. Two ways to put them there — both copy only the scripts, never `var/`:

- **From the UI:** the first-run setup wizard offers to deploy the shipped scripts over the
  connection you just configured.
- **From a workstation:** `bin/deploy-scripts.sh [--dry-run] [user@host]` — a checksum-based
  `rsync` of `bin/scripts/`. It never deletes head-only files unless you pass `--delete`.

On the head node the scripts live under `{RACK_REMOTE_BASE_PATH}/scripts/` in layers (a project
layer, `custom/`, the platform, then `multi-platform/`), or under the legacy
`{RACK_REMOTE_BASE_PATH}/bin/scripts/` on a deployment that has not been migrated — both tools
detect which. Logs default to `{RACK_REMOTE_BASE_PATH}/var/logs/`.

## SSH

The private key is mounted at `/run/secrets/ssh_key`, where the app looks by default; the public
key goes in the head node's `~/.ssh/authorized_keys`:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/rack_status_key -N "" -C "nvis-rack-status"
ssh-copy-id -i ~/.ssh/rack_status_key.pub root@<head-node>
```

**Host keys are not verified by default.** To verify them on every connection the app makes —
the control connection, the web terminal, hops to a head or bastion, bug-report collection and
`bin/deploy-scripts.sh` — seed a known-hosts file from a machine you trust and point
`RACK_SSH_KNOWN_HOSTS` at it:

```bash
ssh-keyscan -H <head-node> >> config/known_hosts
# config/rackstatus.toml
# RACK_SSH_KNOWN_HOSTS = "/app/config/known_hosts"
```

**Jump hosts:** declare them in the cluster's connection topology (Projects & Clusters), or set
`RACK_SSH_OPTS` (for example `-J user@bastion`) in `config/rackstatus.toml`.

## Building the image

```bash
bin/write-build-info.sh
docker build -t nvis-rack-status:local .
```

Or `./build-docker.sh build`.

## Air-gapped deployment

```bash
./build-docker.sh save            # → dist/nvis-rack-status-<version>.tar.gz and .sha256
```

Each release also publishes this bundle to the project's Package Registry and the GitLab Release
page (`nvis-rack-status-<version>.tar.gz` + `.sha256`). On the offline host:

```bash
sha256sum -c nvis-rack-status-<version>.tar.gz.sha256
docker load -i nvis-rack-status-<version>.tar.gz     # or: ./build-docker.sh load <tarball>
```

Then run it as in [Option C](#option-c-plain-docker-run).

## Updating

A Compose deployment builds its image from the tree, so update the tree and rebuild:

```bash
git pull                              # or merge the release branch
bin/write-build-info.sh
docker compose build
docker compose up -d                  # not `docker restart` — that keeps the old image
NVIS_CONTAINER=$(docker compose ps -q rack-status) bash bin/verify-deploy.sh
```

With `docker run`, load or pull the new image, then `docker rm -f rack-status` and run the same
command again — the `rack-data` volume carries everything over.

## Running tests

```bash
pip install -r backend/requirements-dev.txt
python -m pytest backend/tests/ -q
cd frontend && npm test
```

`bin/check-ci-lint.sh` runs the same lint gates CI does.

## Troubleshooting

**The browser shows a certificate warning:** expected with the generated self-signed certificate.
`bin/verify-deploy.sh` prints its SHA-256 fingerprint — compare it before accepting. To remove the
warning, install a certificate your browsers trust in `config/tls/`.

**The container refuses to start:** read `docker logs rack-status`. The app lists every missing
setting at once with the fix; `python backend/cli.py check` reports the same without starting.

**The dashboard shows no racks:**
- Check the head node is reachable: `docker exec rack-status ssh -i /run/secrets/ssh_key root@<host> hostname`
- Check the cluster's connection and base path under **Projects & Clusters** — after the first
  start they outrank `config/rackstatus.toml`
- Check `cmsh` (BCM) or `maas` (MAAS) is available on the head node

**Scripts fail with "No such file or directory":** check the scripts are deployed (see above) and
that the cluster's base path points at them: `ssh root@<host> ls <base>/scripts <base>/bin/scripts`.

**The password is rejected:** the one-time `admin` password is printed only when the identity store
is empty. If every administrator credential is lost, the store must be redeployed — there is
deliberately no reset endpoint, recovery code or bypass flag.

**NeMo containers fail to start (pyxis/enroot errors):** check `enroot`, `pyxis` and
`nvidia-container-cli` on the compute nodes, ~20 GB of free space for extraction, and that
`NEMO_SQSH_SRC` points at a `.sqsh` readable from the compute nodes.

**Environment variables do not reach scripts:** variables with the prefixes in
`transport/factory.py` `FORWARD_ENV_PREFIXES` (`RACK_`, `NEMO_`, `VALIDATION_`, `SQUEUE_`,
`NVIS_`, `PDSH_`, `NGC_`, `LLMB_`) and the names in `FORWARD_ENV_NAMES` are forwarded to the
remote session.
