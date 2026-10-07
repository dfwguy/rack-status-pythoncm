---
diataxis: how-to
description: Find every node — newly onboarded or reimaged — whose NVIS-P2P peer is not working, put back what the node is missing, deploy the peers, and confirm the fleet is healthy before launching campaigns.
---

# How to bring P2P peers up on new or reimaged nodes

The same steps onboard **new** nodes: a freshly provisioned node starts in the state a reimage
leaves behind, minus any surviving peer state.

A reimage keeps the node's data partition — the peer's binaries, its storage and any images survive
under the container directory — but wipes what lives on the root filesystem: a local account entry,
the systemd units, the container directory's ownership, and any edits to node-local healthcheck
config. Peers then stop answering, and campaigns defer the node with
`launch_limit_N:not_started`. These steps find the affected nodes and restore them in bulk.

The scripts live under `deploy/p2p/scripts` (installed at `<install root>/current/scripts`). Every
site-specific value is a parameter. Take the service account's values from the cluster's
**Dragonfly Settings** in Rack Status:

```bash
export NVIS_P2P_ROOT=/opt/nvis-p2p/current      # the installed tree, holding artifacts.lock
export NVIS_SERVICE_USER=<service user>          # Dragonfly Settings: service user
export NVIS_SERVICE_GROUP=<service group>        # ...service group
export NVIS_SERVICE_UID=<uid>                    # ...service uid
export NVIS_SERVICE_GID=<gid>                    # ...service gid
export NVIS_P2P_CATALOG_URL=http://<app server>:12130/.nvis-p2p/catalog/images.tsv
cd "$NVIS_P2P_ROOT/scripts"
```

`NVIS_P2P_CATALOG_URL` is what the deploy installs the 15-minute catalog refresh with. A reimaged
node keeps the URL from its previous install on its data partition, but a **new** node has none:
without the variable its deploy reports `catalog_refresh=not-installed`.

On a BCM-managed cluster whose healthcheck drains nodes running unknown processes, also point the
census and the preparation at that healthcheck's config:

```bash
export NVIS_P2P_HEALTHCHECK_ALLOWLIST=/cm/local/apps/cmd/scripts/healthchecks/configfiles/rogueprocess.py
export NVIS_P2P_BCM_ROGUEPROCESS_CONFIG=$NVIS_P2P_HEALTHCHECK_ALLOWLIST
```

## 1. Take a census

```bash
bash census-peer-fleet.sh roster.txt > census.tsv     # summary on stderr
```

Each node gets one state:

| State | Meaning | Fix |
|---|---|---|
| `healthy` | ready, peer running the pinned build, catalog refresh timer active | nothing |
| `needs-prep` | the node is not ready for a peer: wrong primary group, no passwordless sudo, container directory not writable, or not on the healthcheck allowlist | step 2, then step 3 |
| `needs-peer` | the node is ready but its peer is not running, not the pinned build, or has no refresh timer | step 3 |
| `unreachable` | ssh as the service account failed | hardware or network, outside P2P |

The pinned daemon is read from `artifacts.lock` for the peer build in `NVIS_P2P_PEER_CLIENT`
(default `client-arm64-64k-v1.4.9`).

## 2. Prepare the `needs-prep` nodes (as root)

`prepare-peer-node.sh` runs as root on each node and changes only what is wrong. It never creates
the account — that is the cluster directory's job — and it refuses, changing nothing, if the
account's uid differs, a local entry for it already exists, or `nsswitch` would not consult a local
entry first.

```bash
awk -F'\t' '$2 == "needs-prep" { print $1 }' census.tsv > prep.txt
```

Where root over ssh works:

```bash
xargs -P32 -I{} sh -c 'ssh {} sudo -n env NVIS_SERVICE_USER=$NVIS_SERVICE_USER \
  NVIS_SERVICE_UID=$NVIS_SERVICE_UID NVIS_SERVICE_GROUP=$NVIS_SERVICE_GROUP \
  NVIS_SERVICE_GID=$NVIS_SERVICE_GID bash -s < prepare-peer-node.sh' < prep.txt
```

A reimage can remove the very sudo that would allow that. On BCM, run it through `pexec` instead,
from a copy on storage the nodes share:

```bash
cmsh -c "device; pexec -n $(paste -sd, prep.txt) \"sudo -n env NVIS_SERVICE_USER=... \
  NVIS_SERVICE_UID=... NVIS_SERVICE_GROUP=... NVIS_SERVICE_GID=... \
  bash /shared/path/prepare-peer-node.sh\""
```

Each node prints one line, for example
`PREP node17 passwd=added containers=fixed(root:root:700) state=fixed allowlist=added uid=… gid=… sudo=yes`.
Optional settings: `NVIS_P2P_CONTAINER_OWNER` and `NVIS_P2P_CONTAINER_MODE` (default
`root:<service group>` and `775`), `NVIS_P2P_BASE`, `NVIS_P2P_CONTAINER_DIR`.

## 3. Re-deploy the peers

Every node that was `needs-prep` or `needs-peer`:

```bash
awk -F'\t' '$2 ~ /^needs-/ { print $1 }' census.tsv > redeploy.txt
bash deploy-catalog-peer-fleet.sh redeploy.txt reimage-recovery
```

The deploy re-installs the peer unit and the 15-minute catalog refresh timer, and reports
`catalog_refresh=` in each `DEPLOY_READY` line. If it reads `not-installed`, install the timer on
those nodes with the URL set:

```bash
xargs -P32 -I{} bash deploy-catalog-refresh-timer-node.sh {} < redeploy.txt
```

If a node's peer then crash-loops with `storage/metadata/LOCK: Permission denied` in
`<peer dir>/logs/dfdaemon-service.log`, its surviving state is still owned by an earlier identity:
run step 2 on it (it reclaims that state) and the peer recovers on its next restart.

## 4. Confirm, then distribute again

```bash
bash census-peer-fleet.sh roster.txt > census-after.tsv
```

When every reachable node is `healthy`, re-launch the campaigns. A campaign is idempotent: nodes that
already hold an image are skipped by their receipt, so only the recovered nodes do any work.
