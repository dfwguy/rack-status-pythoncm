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
FROM node:22-slim AS frontend-build

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts

COPY frontend/ ./
RUN npm run build
# vite.config.ts outDir: "../static/spa" -> /build/static/spa

# ──────────────────────────────────────────────────────────────────────────────
# Stage 2: Runtime — Flask app on Ubuntu 22.04
# ──────────────────────────────────────────────────────────────────────────────
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        libmunge2 \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Match the BCM host identity so the mounted Slurm configuration is accepted.
RUN groupadd --gid 450 slurm \
    && useradd --uid 450 --gid 450 --home-dir /cm/local/apps/slurm --shell /bin/sh slurm

# Slurm's root-only --uid/--gid submission path resolves the requested numeric
# identity through the client's local NSS database before contacting slurmctld.
# Keep the Nscale TX validation operator present in the image so allocations run
# as the same account that owns the shared evidence tree instead of as root.
RUN groupadd --gid 1003 cmods \
    && useradd --uid 1003 --gid 1003 --home-dir /home/cmods \
       --shell /bin/bash --no-create-home cmods \
    && useradd --uid 1024 --gid 1003 --home-dir /home/hlakshminara \
       --shell /bin/bash --no-create-home hlakshminara

# Container mirrors the repo layout: Python code under /app/backend, runtime
# assets (bin/, static/, var/, help_content.json) at /app so that backend modules
# resolve REPO_ROOT = parent(backend) == /app, identical to local dev.
WORKDIR /app

COPY backend/requirements.txt ./backend/
RUN python3 -m venv /opt/rack-venv \
    && /opt/rack-venv/bin/pip install --no-cache-dir -r backend/requirements.txt
ENV PATH="/opt/rack-venv/bin:${PATH}"

# Python backend package (modules, platforms/, rack_web/; tests excluded via .dockerignore).
COPY backend/ ./backend/
COPY bin/ ./bin/
COPY static/ ./static/
COPY help_content.json ./
COPY rack-config.env ./
# Release metadata. The app parses this (never imports it) as the version fallback, so it has to
# be in the image -- without it a container reports "unknown".
COPY version.py ./

RUN mkdir -p var/logs var/cache var/secrets var/workflows/runs
RUN chmod 0755 /app/bin/cmsh-readonly-16k
COPY var/workflows/*.yaml ./var/workflows/

# What build is this? backend/build-info.json answers, and it arrives via the COPY above rather
# than being generated here -- see bin/write-build-info.sh for why. In short: deployments bind-mount
# the backend directory over the image's, so anything generated under it at build time is hidden at
# runtime, and /app/var is masked by a named volume. Run bin/write-build-info.sh before building.
# Absent, the app parses version.py and reports "<version>-dev", which is true of an unstamped tree.
RUN cat backend/build-info.json 2>/dev/null \
    || echo "note: backend/build-info.json absent - this image reports <version>-dev"

COPY --from=frontend-build /build/static/spa/ ./static/spa/

EXPOSE 5000

ENV RACK_EXEC_MODE=local \
    RACK_PLATFORM=bcm \
    PORT=5000 \
    GUNICORN_THREADS=24 \
    GUNICORN_TIMEOUT=600

# Production WSGI server. ONE worker on purpose: the app keeps significant
# in-process state (rack-status cache, SSH reachability cache, async command
# contexts, active-user counts, terminal sessions). Multiple workers would each
# duplicate that state and the SSH work. Concurrency comes from gthread threads,
# which preserves the single-process model the app assumes while still serving
# the parallel UI polls + WebSocket terminals far better than the dev server.
ENTRYPOINT ["sh", "-c", "exec gunicorn --workers 1 --threads ${GUNICORN_THREADS} --worker-class gthread --timeout ${GUNICORN_TIMEOUT} --graceful-timeout 30 --keep-alive 5 --access-logfile - --pythonpath /app/backend --bind 0.0.0.0:${PORT} app:app"]
