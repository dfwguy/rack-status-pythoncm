# deploy/ — proposed deployment configuration, under review

The OVA's live `docker-compose.override.yml` lives **only on that box**
(`/opt/observability/nvis-rack-status-v3/`) and is not tracked by git. It carries ~50 bind mounts and
is the most failure-prone piece of the deployment, with no history, no CI and no review — which is
why a missing mount crash-looped the container on 2026-08-14 with nothing in any diff to show it.

Files here are proposed versions of it, so a change can be reviewed before it is applied by hand.
They are **not** read by anything automatically.

## `nscale-tx.override.consolidated.yml`

The live override with its 37 individual `backend/*` mounts collapsed into one directory mount:

```yaml
- /opt/observability/nvis-rack-status-v3/backend:/app/backend:ro
```

Safe because the tree is a superset of the image's backend — the Dockerfile generates nothing under
it, and `.dockerignore` strips only `backend/tests` and `__pycache__` — and because all 37 mounts it
replaces were already `:ro`. 16 volumes remain, every one read-only.

What it buys: a new module or package is picked up automatically instead of being invisible until
someone remembers to add a line.

What it changes: the tree becomes the sole authority for backend code, so an image rebuild alone no
longer changes behaviour. That is already true of `app.py`, which is among the 37 — this makes it
consistent rather than partial.

To apply, on the OVA:

```bash
cd /opt/observability/nvis-rack-status-v3
cp docker-compose.override.yml "docker-compose.override.yml.bak.$(date -u +%Y%m%dT%H%M%S)"
# copy this file over docker-compose.override.yml, then:
docker compose config >/dev/null      # syntax gate
docker compose up -d                  # a mount change needs the container replaced
NVIS_CONTAINER=nvis-rack-status-v3 bash bin/verify-deploy.sh
```
