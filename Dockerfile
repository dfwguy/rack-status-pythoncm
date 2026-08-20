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
COPY rack-config.env ./
# Release metadata. The app parses this (never imports it) as the version fallback, so it has to
# be in the image -- without it a container reports "unknown".
COPY version.py ./
# Holds the ssl_context hook, which is the only way to set a minimum TLS version (gunicorn
# exposes it as a config-file hook, not a flag). Harmless when TLS is off.
COPY gunicorn.conf.py ./

RUN mkdir -p var/logs var/cache var/secrets var/workflows/runs
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
# TLS is opt-in: set RACK_WEB_TLS_CERT and RACK_WEB_TLS_KEY (bin/gen-tls-cert.py issues them)
# and gunicorn serves HTTPS. Expanded here rather than in a config file so a deployment turns TLS
# on with two environment variables and no rebuild. The certificate's SAN must include `localhost`
# -- operators reach this through an ssh tunnel, so that is the name their browser validates.
ENTRYPOINT ["sh", "-c", "exec gunicorn -c /app/gunicorn.conf.py --workers 1 --threads ${GUNICORN_THREADS} --worker-class gthread --timeout ${GUNICORN_TIMEOUT} --graceful-timeout 30 --keep-alive 5 --access-logfile - --pythonpath /app/backend --bind 0.0.0.0:${PORT} ${RACK_WEB_TLS_CERT:+--certfile ${RACK_WEB_TLS_CERT} --keyfile ${RACK_WEB_TLS_KEY}} app:app"]
