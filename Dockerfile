# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# ──────────────────────────────────────────────────────────────────────────────
# Stage 1: Build the React SPA
# ──────────────────────────────────────────────────────────────────────────────
FROM node:24-slim AS frontend-build

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts

COPY frontend/ ./
RUN npm run build
# vite.config.ts outDir: "../static/spa" -> /build/static/spa

# ──────────────────────────────────────────────────────────────────────────────
# Stage 2: Runtime — Flask app on Ubuntu 22.04
# ──────────────────────────────────────────────────────────────────────────────
# ---- docs -------------------------------------------------------------------------------------
# Sphinx HTML, built into the image so the documentation a deployment serves at /docs/ is always
# the documentation for the code running there. Operators reach these clusters through an SSH
# tunnel and often cannot reach an internet docs site at all -- the same constraint that made the
# Kaizen CSS vendored.
#
# `-W`: a warning here is a broken cross-reference or a page missing from `_toc.yml`, and both are
# 404s for a reader. Failing the image build is the right response.
FROM python:3.12-slim AS docs-build
WORKDIR /build
COPY docs/requirements.txt docs/requirements.txt
RUN pip install --no-cache-dir -r docs/requirements.txt
# `version.py` because conf.py parses the version out of it. No `.git`: conf.py already tolerates
# git being absent and reports the version without a commit, which is worth more than shipping the
# repository into a build stage.
COPY version.py ./
COPY docs/ docs/
RUN sphinx-build -W --keep-going -b html docs static/docs


FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Container mirrors the repo layout: Python code under /app/backend, runtime
# assets (bin/, static/, var/, help_content.json) at /app so that backend modules
# resolve REPO_ROOT = parent(backend) == /app, identical to local dev.
WORKDIR /app

COPY backend/requirements.txt ./backend/
RUN pip3 install --no-cache-dir -r backend/requirements.txt

# Python backend package (modules, platforms/, rack_web/; tests excluded via .dockerignore).
COPY backend/ ./backend/
COPY bin/ ./bin/
COPY static/ ./static/
COPY help_content.json ./
# Release metadata. The app parses this (never imports it) as the version fallback, so it has to
# be in the image -- without it a container reports "unknown".
COPY version.py ./
# Owns TLS: picks the certificate (config/tls/ if the operator supplied one, else a self-signed pair
# generated on first boot under var/secrets/tls/) and sets the minimum protocol version. There is
# no plain-HTTP path.
COPY gunicorn.conf.py ./

# config/ is where the host's config directory is mounted read-only (rackstatus.toml, tls/). The
# example ships in the image so `docker run ... cat config/rackstatus.toml.example` works anywhere.
RUN mkdir -p var/logs var/cache var/secrets var/workflows/runs config
COPY config/rackstatus.toml.example ./config/
COPY var/workflows/*.yaml ./var/workflows/

# NVIS-P2P deploy assets: the Dragonfly compose files, config templates, scripts and catalog
# manifest that the P2P provisioning workflow uses to stand up a per-cluster instance. When a
# builder has staged the pinned binaries (deploy/p2p/bin/stage-artifacts.sh writes deploy/p2p/dist/,
# ~300 MB, git-ignored) they ride in here too. The image then RE-verifies any staged binaries
# against artifacts.lock, so a corrupted or wrong artifact fails the build instead of shipping. With
# no staged dist/, this is a text-only image (UI + gateway) and the workflow reports the bundle
# absent -- a deliberate, honest state. See deploy/p2p/README.md.
COPY deploy/p2p/ ./deploy/p2p/
RUN if [ -d deploy/p2p/dist ]; then \
        ( cd deploy/p2p/dist && grep -vE '^[[:space:]]*(#|$)' ../artifacts.lock | sha256sum -c - ) \
        && echo "p2p: staged Dragonfly artifacts verified against artifacts.lock"; \
    else \
        echo "note: deploy/p2p/dist absent - image is text-only (no P2P provisioning binaries)"; \
    fi

# What build is this? backend/build-info.json answers, and it arrives via the COPY above rather
# than being generated here -- see bin/write-build-info.sh for why. In short: deployments bind-mount
# the backend directory over the image's, so anything generated under it at build time is hidden at
# runtime, and /app/var is masked by a named volume. Run bin/write-build-info.sh before building.
# Absent, the app parses version.py and reports "<version>-dev", which is true of an unstamped tree.
RUN cat backend/build-info.json 2>/dev/null \
    || echo "note: backend/build-info.json absent - this image reports <version>-dev"

COPY --from=frontend-build /build/static/spa/ ./static/spa/
# The deployment tracker's static page, which /api/deployment-tracker/site.zip packages with data.
COPY --from=frontend-build /build/static/tracker/ ./static/tracker/
COPY --from=docs-build /build/static/docs/ ./static/docs/

EXPOSE 5000

ENV PORT=5000 \
    GUNICORN_THREADS=24 \
    GUNICORN_TIMEOUT=600

# Production WSGI server. ONE worker on purpose: the app keeps significant
# in-process state (rack-status cache, SSH reachability cache, async command
# contexts, active-user counts, terminal sessions). Multiple workers would each
# duplicate that state and the SSH work. Concurrency comes from gthread threads,
# which preserves the single-process model the app assumes while still serving
# the parallel UI polls + WebSocket terminals far better than the dev server.
# HTTPS only. gunicorn.conf.py settles the certificate when gunicorn loads it — before the listener
# exists — so no shell logic here decides TLS, and nothing can start it without. The certificate's
# SAN must include `localhost`: operators reach this through an ssh tunnel, so that is the name
# their browser validates (bin/gen-tls-cert.py adds it unconditionally).
ENTRYPOINT ["sh", "-c", "exec gunicorn -c /app/gunicorn.conf.py --workers 1 --threads ${GUNICORN_THREADS} --worker-class gthread --timeout ${GUNICORN_TIMEOUT} --graceful-timeout 30 --keep-alive 5 --access-logfile - --pythonpath /app/backend --bind 0.0.0.0:${PORT} app:app"]

# Healthy means "answering HTTPS" -- see bin/healthcheck.py for what counts and why the certificate
# is not verified there (bin/verify-deploy.sh verifies the chain).
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python3", "/app/bin/healthcheck.py"]
