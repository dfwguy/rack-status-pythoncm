<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: LicenseRef-NvidiaProprietary
-->
# NVIS-P2P deploy assets

The Dragonfly control-plane and compute-peer assets that Rack Status uses to provision NVIS-P2P
(peer-to-peer container-image distribution) on a cluster. These are consumed by the P2P
provisioning workflow (`backend/api/p2p_*`, and the reconcile workflow) — they are **not** run by
hand in normal operation.

P2P is single-tenant: one Dragonfly instance per cluster, enabled per cluster from **Projects &
Clusters** (`Cluster.p2p_enabled`). See `docs/explanation/p2p-module-integration.md`.

## Layout

| Path | What it is |
|---|---|
| `deploy/` | Per-component Docker Compose for the control plane — `postgres/`, `redis/`, `control-plane/` (Manager + scheduler), `object-store/` (dfstore/MinIO), `seed-origin/` (seed nginx origin) — plus `systemd/` units for the compute-node peer and catalog-sync services. |
| `configs/` | Rendered-from templates: Manager, scheduler, seed and peer config. Secrets are **file references** (`*_FILE`, `/run/secrets/...`), never literal values. |
| `catalog/` | The validated container-image release manifest (`images.json` / `images.tsv`). A per-cluster manifest overrides this; this is the shipped default. |
| `scripts/` | The operational toolkit — catalog operations, per-node/per-fleet preflight, distribute, sync, verify, and the client/server build recipes. |
| `artifacts.lock` | **Pinned sha256 of every Dragonfly binary** the image is allowed to bake in. The reproducibility anchor. |
| `bin/stage-artifacts.sh` | Stages those binaries into `dist/` from a validated source set and verifies them against the lock. |
| `dist/` | The staged binaries (~300 MB). **Git-ignored** — never committed. |

## Reproducible, repeatable packaging

The binaries are ~300 MB and would bloat every clone, so they are not in git. Reproducibility comes
from the **sha256 pins in `artifacts.lock`**, not from committing the bytes or trusting a download
URL — which matters because the `client-arm64-64k` build (aarch64 GB300 compute, 64 KiB page ELF
alignment) is a custom build with no upstream release to fetch.

The build bakes them in, verified:

```bash
# 1. Stage + verify the pinned binaries from a validated set (once per builder):
P2P_ARTIFACT_SOURCE_DIR=/path/to/validated/dist deploy/p2p/bin/stage-artifacts.sh

# 2. Build the image. The Dockerfile COPYs deploy/p2p/ (including the staged dist/) and
#    re-verifies the checksums inside the image, failing the build on any mismatch.
build-docker.sh            # or: docker build -t nvis-rack-status:<tag> .
```

A build with no staged `dist/` produces a **text-only** image: the UI and gateway ship, but the
provisioning workflow has no binaries to install and will report the bundle as absent. That is a
deliberate, honest state, not a silent one.

The image is then self-contained: an air-gapped deploy target needs no upstream access, because the
provisioning workflow installs the binaries from the image rather than fetching them at run time.
