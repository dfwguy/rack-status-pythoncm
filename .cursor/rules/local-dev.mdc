---
description: Local development startup and Docker build scripts
alwaysApply: true
---

# Local Development & Deployment

## Three prerequisites, or it will not serve

Since 2026-09-08 the app **refuses to start** without an administrator, an authorization policy and
TLS. On a brand-new deployment it creates all three itself — the administrator with a one-time
password, the `administrators` policy attached to them, and a self-signed certificate — so a first
run needs nothing. What it will not do is invent a policy for a store that *already* holds accounts,
because nothing tells it which of them should hold full access; that case is refused with the fix
named. Reasoning and the ordering traps: `identity-auth-traps.mdc` §11.

**Find out before you start anything:**

```bash
python backend/cli.py check          # what a serve would create; exits 78 on a fault
python backend/cli.py check --json   # same, for a deploy gate
```

`check` creates no administrator, credential or policy. It *may* seed the catalog, because the
required-setting preflight resolves the active cluster — pinned as a documented exception in
`test_cli.py`, not an oversight.

`serve` takes the same path overrides and hands off to gunicorn with `gunicorn.conf.py`, so a local
run gets the same TLS floor as production:

```bash
python backend/cli.py serve --port 5050
python backend/cli.py serve --identity-file … --policy-file … --catalog-file … --config …
python backend/cli.py serve --no-bootstrap   # refuse instead of creating anything
```

**It is argparse, and the flags only write `os.environ`.** Precedence has one owner — the
`config_registry` / `config_launch` / `config_env` ladder — and `click`'s `envvar=` treats an empty
variable as absent, the opposite of this product's rule that an empty string is a deliberate value
(`TestTC19EmptyStringWins`). Wiring it would fork the contract, so the CLI resolves nothing and the
ladder decides everything. `backend/cli.py` also imports no first-party module at module scope,
because `config_launch.load()` runs during `import app`.

## Local Dev

Use `./start.sh` to build and run locally. It handles:
- Python venv creation and deps install
- Node.js detection/download, frontend `npm install` + `npm run build`
- Auto-detects OS: macOS = demo mode, Linux = cluster mode
- Defaults to port 5050

```bash
./start.sh                              # auto-detect mode
RACK_STATUS_DEMO=1 ./start.sh           # force demo
BUILD_FRONTEND=0 ./start.sh             # skip frontend rebuild
```

Do NOT manually run `python3 backend/app.py` or `npm run build` separately — always use `start.sh`.

## SSH Mode (local Mac → remote cluster)

To run locally against a real cluster via SSH:

1. Deploy scripts to the head node.

**The repo's `bin/scripts/` is the SOURCE OF TRUTH; the head node (NFS) holds a deployed copy
under `RACK_REMOTE_BASE_PATH/bin/scripts`.** Always sync with `bin/deploy-scripts.sh` — a
checksum-based `rsync` wrapper — NOT per-file `scp` (per-file copies silently drift; e.g. an
edited `sysinfo.sh`/`*.sbatch` left stale on the head). It reads the same `RACK_SSH_*` /
`RACK_REMOTE_BASE_PATH` env the app uses, excludes Python build artifacts, and does not delete
head-only files.

```bash
bin/deploy-scripts.sh boston-slogin              # via an ~/.ssh/config alias
bin/deploy-scripts.sh --dry-run boston-slogin    # preview what would change (checksum diff)
RACK_SSH_HOST=<head-ip> RACK_SSH_USER=root RACK_SSH_KEY=~/.ssh/<key> \
  RACK_REMOTE_BASE_PATH=/home/cmsupport/nvis-rack-status bin/deploy-scripts.sh
```

**Where it lands depends on which layout the head node has, and it checks rather than guesses.**
A head with `<base>/scripts/multi-platform/` gets the layered tree (plus
`bin/scripts/platform/<id>/` → `<base>/scripts/<id>/`); one without it gets today's flat
`<base>/bin/scripts`. So an unmigrated cluster is untouched, and after the first
`bootstrap-project-scripts.sh` every later deploy lands in the right place with no flag to
remember. `--tree` and `--legacy` force it either way.

Re-running the deploy does **not** update a project's own copy of the scripts: that is the point
of the copy. Pull a fixed default forward with "Update scripts" on the Projects screen, which
reports -- and declines to overwrite -- any file the site has edited.

After editing anything under `bin/scripts/`, re-run `bin/deploy-scripts.sh` so local↔head stay
in sync; a `--dry-run` returning no lines means they match. (Scripts are executed on the head
whenever the target endpoint is remote — an unsynced edit means the app runs the old version.)

**Run `bin/prod-drift.sh` BEFORE any deploy.** On a live customer cluster other people fix
things in place, in two spots git never sees: the deployment tree's bind-mounted `backend/*.py`
and the head node's `bin/scripts` copy. A deploy overwrites both — on Nscale TX this nearly
destroyed six real fixes. `prod-drift` compares by content against the *deployment's own base
commit* (so "we are ahead" is not confused with "prod was edited"), exits 1 on drift, and
`--fetch DIR` lays the remote and git copies out for a `git merge-file` 3-way merge. Harvest and
commit what it finds first. Do not assume the cluster copy is newer: verify a drifted file is a
superset of ours, not a stale lineage that was patched locally.

2. Start the app with SSH env vars:
```bash
RACK_SSH_HOST=<head-node-ip> \
RACK_SSH_USER=root \
RACK_SSH_KEY=~/.ssh/<key> \
RACK_PLATFORM=bcm \
RACK_STATUS_DEMO=0 \
RACK_REMOTE_BASE_PATH=/home/cmsupport/nvis-rack-status \
BUILD_FRONTEND=0 \
./start.sh
```

`BUILD_FRONTEND=0` skips the frontend rebuild when only testing backend/cluster changes.

## How a deployment resolves files — two layers, and the mount always wins

Every "why is my change not live?" and "why is my change live when I didn't rebuild?" question in
this project comes back to one model:

| Layer | Applies to | Changing it |
|---|---|---|
| **Image** | any path *not* bind-mounted | needs a rebuild (`docker compose build`) |
| **Bind mount** | the mounted path, which shadows the image's copy | edit the host file; restart only if Python |
| **Named volume** (`rack-data:/app/var`) | `/app/var` | seeded from the image **only while empty**; after that the volume wins forever |

The mount layer is deliberate — it is what makes a live update possible without a rebuild, which on
a customer cluster is a one-minute fix instead of a ten-minute build. `bin/deploy-app.sh` exists to
drive exactly that path. **The corollary is that for any mounted path, the tree is the source of
truth and an image rebuild alone changes nothing.**

Three consequences, each of which has already cost real time:

- **A file generated during the build, under a mounted path, is invisible.** The mount covers it.
  So `bin/write-build-info.sh` stamps `backend/build-info.json` into the *tree* and lets
  `COPY backend/` carry it into the image; the Dockerfile does not generate it.
- **`/app/var` is stricter than a bind mount.** The first version of the build stamp was written to
  `var/build-info.json` and could never have been read on that OVA, because `rack-data` has existed
  for days and Docker only seeds an *empty* named volume.
- **A new module under a per-file mount list is invisible until listed.** This is what crash-looped
  the OVA: the new `app.py` arrived through its own mount while `app_version.py` had none, in an
  image that predated it. Mounting the *directory* removes the whole class.

## Deploying to a container deployment (OVA)

**A NEW backend module needs an image rebuild, not `deploy-app.sh`.** The OVA's
`docker-compose.override.yml` bind-mounts backend files **one file at a time** (~54 mounts), and its
own comment says it: a `.py` that is not listed is invisible to the container. `deploy-app.sh` also
rsyncs only `backend/*.py` as a flat glob, so it never copies a new *package* like `backend/api/`.
Deploying today's refactor that way put the new `app.py` in front of a container with no
`app_version.py`, and gunicorn crash-looped on `ModuleNotFoundError` with the dashboard down.

So, when the change adds or moves modules:

```bash
cd /opt/observability/nvis-rack-status-v3
git merge <branch>                     # tree first, so the build has the new files
bin/write-build-info.sh                # stamps backend/build-info.json; COPY carries it in
docker compose build
docker compose up -d                   # NOT docker restart -- that reuses the old image
NVIS_CONTAINER=nvis-rack-status-v3 bash bin/verify-deploy.sh
```

**The OVA's `docker-compose.override.yml` is not in git.** It holds ~50 bind mounts (37 of them
under `backend/`, all `:ro`) and is the single most failure-prone piece of the deployment with no
version history, no CI and no review — which is why the mount gap above was invisible until the
container crash-looped. There are a dozen `.bak.*` copies beside it from past hand edits. Collapsing
the 37 into one `- .../backend:/app/backend:ro` is safe (the tree is a superset of the image's
backend: the Dockerfile generates nothing there, and `.dockerignore` only strips tests and
`__pycache__`) and makes a new module visible automatically. It also makes the tree the sole
authority for backend code, so an image rebuild alone stops changing behaviour — which is already
true of `app.py` today.

`build-docker.sh` builds `nvis-rack-status:local`, which this container does not use — see
`nvis-rack-status.mdc`. And the host's `static/spa` is mounted over the image's, so an image built
from newer source still serves the older mounted bundle; `verify-deploy.sh` catches exactly that.
To realign without a Node toolchain on the box, copy the SPA out of the image:

```bash
id=$(docker create nvis-rack-status-v3-rack-status:latest)
docker cp "$id:/app/static/spa/." static/spa/ && docker rm "$id"
docker restart nvis-rack-status-v3     # index.html is a single-file mount; its inode changed
```

For a backend-only change to modules **already mounted**, `bin/deploy-app.sh` is still the fast
path — never a bare rsync:

```bash
bin/deploy-app.sh nscale-tx-ova            # sync, restart if backend changed, verify
bin/deploy-app.sh --dry-run nscale-tx-ova  # preview
```

It refuses to deploy an incoherent tree (missing `index.html`, or one referencing a bundle that
is not in `static/spa/assets`), restarts only when a backend `.py` actually changed, and ends by
running the gate below.

**It also refuses when the deployment changed since our last deploy.** After each deploy it writes
`.nvis-deploy-manifest` on the remote (bundle ref, commit, timestamp, who); before the next one it
compares the live bundle against that record and stops if they differ, so you find out *before*
overwriting somebody's change. `--force` proceeds anyway.

Why this rather than `prod-drift.sh`: **`static/spa/` is gitignored**, so a rebuilt frontend is
invisible to a tool that diffs tracked files against a git ref, and `prod-drift` also exits
non-zero on untracked extras (this deployment permanently has a Node toolchain and `.prebak` files
under it) so it cannot gate a deploy. On 2026-08-13 the OVA's own stale checkout was rebuilt in
place, reverting the frontend to a build predating a whole feature: the UI showed a deleted
placeholder screen and a route fell through to the dashboard. Every consistency check passed,
because host, container and served bundle all agreed — consistently wrong. `deploy-app.sh` still
reports tracked-file drift as a warning, since that part `prod-drift` does see.

**The underlying hazard remains:** the deployment tree holds buildable source at an old commit, and
we rsync built artifacts on top of it. Anything that runs a frontend build there (`start.sh`,
`build-docker.sh`, a bare `npm run build`) regenerates `static/spa` from that old source and reverts
the UI. The manifest detects it; advancing that checkout to the deployed commit would remove it.

**`index.html` and the backend `.py` files must be synced with `rsync --inplace`.** Compose
bind-mounts them as **single files**, which binds the container to that file's *inode*. A default
`rsync` writes a temp file and renames it over the target, replacing the inode — so the host
shows the new file while the container reads the old, now-unlinked one **indefinitely**, and only
a `docker restart` re-resolves the mount. `assets/` is a directory mount and is immune, which is
what makes the failure so confusing: the new bundles are all present and the page still asks for
the old one. This produced the black screen on 2026-08-12 and again on the API-reference deploy
the same afternoon, in both cases with `GET /login` returning 200. Note `--inplace` cannot repair
it after the fact — once the inode is replaced, only a restart will do. `deploy-app.sh` exists so
this is not something anyone has to remember.

## Verifying a deployment

`./build-docker.sh` runs **`bin/verify-deploy.sh`** after starting a container, and it exits
non-zero unless every check passes. Run it by hand against a remote deployment with
`NVIS_DEPLOY_SSH=<host> bin/verify-deploy.sh`.

It checks the things a smoke test misses: that the container is running rather than crash-looping,
that every backend module imports (an unmounted module that `app.py` imports is an instant crash
loop), that no module in the container is staler than the tree (a mount declared but never
activated by a `restart`), and — the one that matters most — that **every asset the served page
references actually resolves**. A hashed bundle that has moved on leaves the HTML perfectly
serveable and the product blank; that is exactly how the dashboard showed a black screen for an
hour on 2026-08-12 while `GET /login` returned 200 throughout.

## Docker Build & Run

Use `./build-docker.sh` for Docker workflows:

```bash
./build-docker.sh              # dev mode (demo, no auth, port 5050)
./build-docker.sh prod         # prod mode (SSH to head node, token auth)
./build-docker.sh build        # build image only
./build-docker.sh save         # build + docker save → dist/*.tar.gz (air-gapped bundle)
./build-docker.sh load <tar>   # docker load a saved image on an offline host
./build-docker.sh stop         # stop container
./build-docker.sh logs         # tail logs
```

Prod requires `RACK_SSH_HOST` and `RACK_WEB_AUTH_TOKEN` env vars.

## Air-Gapped Image Bundle

`./build-docker.sh save` builds the image and `docker save`s it to `dist/nvis-rack-status-<version>.tar.gz` (+ `.sha256`) for offline hosts. Tag/output overrides: `RACK_IMAGE_TAG`, `RACK_IMAGE_OUT_DIR`, `RACK_IMAGE_COMPRESS`. Version comes from `version.py` at the repo root. In CI the `docker-save` job (Kaniko `--tarPath`, daemonless) builds the bundle on the release commit tagged with the release VERSION, and `release:publish-bundle` uploads it to the generic Package Registry (`nvis-rack-status-image/<version>/`) and links it on the GitLab Release; `docker-save` is also available manually on any pipeline. On the target: `docker load -i <tar>` then `docker run`. See INSTALL.md "Air-Gapped Deployment".
