# NVIS Rack Status (`nvis-rack-status`)

Flask + React dashboard for **rack-level operations and NEMO validation visibility**: a sortable rack table with filters, parser charts (NCCL bandwidth, step-time), log export, Slurm controls, power and GPU flows, NVSwitch / NVLink tooling, optional Grafana embeds, and token or PAM login.

**Configuration:** every environment variable is documented in **[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)**.

---

## Quick Start (Standalone)

### Prerequisites

- Python 3.10+
- Node.js 22+ and npm (for the React SPA)
- Access to a BCM or MAAS head node (for Slurm/cmsh commands), or use demo mode

### 1. Backend

```bash
cd nvis-rack-status
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Frontend (React SPA)

```bash
cd frontend
npm install
npm run build     # builds to ../static/spa/
cd ..
```

### 3. Run

```bash
export PORT=5000
python3 app.py
```

Open **http://localhost:5000/spa/** for the React UI, or **http://localhost:5000/** for the legacy HTML dashboard.

### Demo mode (no cluster needed)

```bash
RACK_STATUS_DEMO=1 RACK_WEB_AUTH_DISABLED=1 python3 app.py
```

### Production with Grafana and custom validation path

```bash
export VALIDATION_TEST=nccl_allreduce,llm
export RACK_WEB_PROJECT_NAME="Project NAME"
export NEMO_SQSH_SRC=/home/cmsupport/nemo-25.11.01.sqsh
export RACK_STATUS_LOG_DIR=/home/cmsupport/aihpc_val-main/validation/nemo/
export PORT=5000
python3 app.py
```

---

## Docker

### Build and run (one command)

```bash
./build-docker.sh          # dev: demo data, no auth, port 5050
./build-docker.sh prod     # prod: SSH to head node, token auth
./build-docker.sh build    # build only
./build-docker.sh stop     # stop container
./build-docker.sh logs     # tail logs
./build-docker.sh help     # show all options
```

### Manual Docker commands

**Demo mode:**

```bash
docker build -t nvis-rack-status:local .
docker run -d --name rack-status -p 5050:5000 \
  -e RACK_STATUS_DEMO=1 \
  -e RACK_WEB_AUTH_DISABLED=1 \
  nvis-rack-status:local
```

**Production (SSH to BCM head node):**

```bash
docker run -d --name rack-status -p 5000:5000 \
  -e RACK_EXEC_MODE=ssh \
  -e RACK_PLATFORM=bcm \
  -e RACK_SSH_HOST=10.0.0.1 \
  -e RACK_SSH_USER=root \
  -e RACK_WEB_AUTH_MODE=token \
  -e RACK_WEB_AUTH_TOKEN=<password> \
  -v ~/.ssh/id_rsa:/run/secrets/ssh_key:ro \
  -v /path/to/logs:/app/var/logs \
  --restart unless-stopped \
  nvis-rack-status:local
```

**Production (MAAS head node):**

```bash
docker run -d --name rack-status -p 5000:5000 \
  -e RACK_EXEC_MODE=ssh \
  -e RACK_PLATFORM=maas \
  -e RACK_SSH_HOST=10.0.0.1 \
  -e RACK_SSH_USER=root \
  -e RACK_WEB_AUTH_MODE=token \
  -e RACK_WEB_AUTH_TOKEN=<password> \
  -v ~/.ssh/id_rsa:/run/secrets/ssh_key:ro \
  --restart unless-stopped \
  nvis-rack-status:local
```

### Docker Compose

```bash
cp .env.example .env
# Edit .env with your site values
docker compose up -d
```

### Architecture

The Docker image runs the Flask app inside the container and executes cluster commands (Slurm, cmsh, pdsh) on the head node via SSH. This is controlled by the `RACK_EXEC_MODE` and `RACK_PLATFORM` environment variables.

```
Container (Flask + React SPA)
  └── SSH ──► Head Node (BCM or MAAS)
                ├── sinfo / squeue / sacct / scontrol
                ├── cmsh (BCM) or maas-cli (MAAS)
                ├── pdsh
                └── rack_status.sh, sysinfo.sh, etc.
```

See **[INSTALL.md](INSTALL.md)** for full Docker deployment instructions, SSH key setup, configuration reference, and troubleshooting.

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python / Flask |
| Frontend (React SPA) | React 19, Vite 8, TypeScript 6 |
| UI Components | NVIDIA Kaizen UI (`@nvidia/foundations-react-core`) primary, MUI secondary |
| Charts | Chart.js + react-chartjs-2 |
| Icons | Lucide React |
| Theming | KUI ThemeProvider (dark/light) synced to MUI ThemeProvider |

---

## Project Structure

```
nvis-rack-status/
├── app.py                  # Flask backend (all API routes)
├── native_status.py        # Native Python rack-status builder
├── demo_data.py            # Synthetic demo data generator
├── host_exec.py            # SSH/local command execution layer
├── platforms/              # BCM and MAAS platform providers
│   ├── base.py             # Abstract PlatformProvider
│   ├── bcm.py              # BCM 11.x (cmsh, pdsh, module load slurm)
│   └── maas.py             # Canonical MAAS (maas-cli)
├── rack_web/               # PAM auth helpers
├── bin/scripts/            # Operational shell scripts
├── frontend/               # React SPA (Vite + TypeScript)
│   ├── src/components/     # Reusable components (RackTable, ParserResultsDialog, etc.)
│   ├── src/views/          # Page views (Dashboard, Multirack, Slurm, etc.)
│   └── src/context/        # Theme and auth context
├── templates/              # Legacy HTML dashboard
├── static/                 # Static assets (built SPA goes to static/spa/)
├── var/                    # Runtime logs and caches
├── Dockerfile              # Multi-stage build (Node + Ubuntu)
├── docker-compose.yml      # Compose with SSH key mount
├── build-docker.sh         # Build and run helper script
├── INSTALL.md              # Docker deployment guide
└── requirements.txt        # Python dependencies
```

---

## Status Backends: Native Python vs Shell

| Mode | Variable | What runs |
|------|-----------|-----------|
| **Native (default)** | `RACK_STATUS_USE_SHELL_TABLE` unset or false | `native_status.py` — cmsh rack list, async cmsh ds, sinfo, and log probes merge into in-memory state |
| **Shell (legacy)** | `RACK_STATUS_USE_SHELL_TABLE=1` | `rack_status.sh` — full shell pipeline each refresh |

---

## Key API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/status` | GET | Main rack status table |
| `/api/multirack-status` | GET | Multirack validation logs |
| `/api/parser-results` | GET | Parse log and return chart data |
| `/api/comments/<rack>` | PUT/DELETE | Set or clear rack comment |
| `/api/slurm-squeue` | GET | Slurm squeue output |
| `/api/slurm-sinfo-r` | GET | Slurm sinfo -R output |
| `/api/slurm-drain` | POST | Drain rack nodes |
| `/api/slurm-undrain` | POST | Undrain rack nodes |
| `/api/slurm-scancel` | POST | Cancel Slurm job |
| `/api/run-validation-async` | POST | Launch single-rack validation |
| `/api/run-nemo-xrack-async` | POST | Launch multirack validation |
| `/api/power-reset` | POST | Power-cycle nodes |
| `/api/auth-check` | GET | Check authentication status |
| `/api/login` | POST | Token/PAM login |

---

## Configuration

### Core settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | Flask listen port |
| `RACK_STATUS_DEMO` | `0` | `1` for synthetic demo data |
| `RACK_STATUS_LOG_DIR` | `var/logs/` | Root for NEMO validation logs |
| `RACK_EXEC_MODE` | `local` | `local` or `ssh` (Docker) |
| `RACK_PLATFORM` | `bcm` | `bcm` or `maas` |

### Docker SSH settings

| Variable | Default | Description |
|----------|---------|-------------|
| `RACK_SSH_HOST` | — | Head-node hostname/IP (required in ssh mode) |
| `RACK_SSH_USER` | `root` | SSH user |
| `RACK_SSH_KEY` | `/run/secrets/ssh_key` | Path to SSH private key |
| `RACK_SSH_PORT` | `22` | SSH port |

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `RACK_WEB_AUTH_MODE` | auto | `token` or `pam` (auto-selects token in SSH mode) |
| `RACK_WEB_AUTH_TOKEN` | — | Login password for token mode |
| `RACK_WEB_AUTH_DISABLED` | `0` | `1` to disable login entirely |
| `RACK_WEB_SECRET_KEY` | random | Stable session key across restarts |

Full variable reference: **[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)**

---

## Bundled Scripts

Operational scripts live under `bin/scripts/` (paths overridable with `RACK_*_SCRIPT` env vars). Prerequisites (Slurm, pdsh, cmsh, etc.) are summarized in `bin/scripts/README.txt`.

---

## Further Reading

| Doc | Content |
|-----|---------|
| **[INSTALL.md](INSTALL.md)** | Docker deployment, SSH setup, troubleshooting |
| **[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)** | All environment variables, timeouts, disable flags |
| **`app.py` module docstring** | PAM, Grafana alignment, cache files |
| **`docs/nvis-rack-status-slides.html`** | Reveal.js overview (open in browser) |

---

## Screenshots

Parser results with InfiniBand table and embedded Grafana (GPU thermal / power):

![Parser results — IB table and Grafana](docs/screenshots/ui-parser-results-ib-grafana.png)

Main table with NEMO log modal; footer shows `backend: native_status.py` when the Python table is active:

![Rack table and NEMO log modal](docs/screenshots/ui-nemo-log-table.png)

Parser results — NCCL summary and all_reduce bandwidth chart:

![Parser results — NCCL charts](docs/screenshots/ui-parser-results-nccl.png)
