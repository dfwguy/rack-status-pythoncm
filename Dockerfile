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

RUN mkdir -p var/logs var/cache var/secrets var/workflows/runs
COPY var/workflows/*.yaml ./var/workflows/

COPY --from=frontend-build /build/static/spa/ ./static/spa/

EXPOSE 5000

ENV RACK_EXEC_MODE=local \
    RACK_PLATFORM=bcm \
    PORT=5000

ENTRYPOINT ["python3", "backend/app.py"]
