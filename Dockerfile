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

WORKDIR /app

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app.py native_status.py demo_data.py host_exec.py ai_chat.py workflow_engine.py ./
COPY platforms/ ./platforms/
COPY rack_web/ ./rack_web/
COPY bin/ ./bin/
COPY templates/ ./templates/
COPY static/ ./static/
COPY rack-config.env ./

RUN mkdir -p var/logs var/cache var/workflows/runs
COPY var/workflows/*.yaml ./var/workflows/

COPY --from=frontend-build /build/static/spa/ ./static/spa/

EXPOSE 5000

ENV RACK_EXEC_MODE=local \
    RACK_PLATFORM=bcm \
    PORT=5000

ENTRYPOINT ["python3", "app.py"]
