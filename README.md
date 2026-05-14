# NVIS Rack Status (`nvis-rack-status`)

[![Pipeline](https://gitlab-master.nvidia.com/nvis/nvis-rack-status/badges/feature/react-frontend-nvis-ux/pipeline.svg)](https://gitlab-master.nvidia.com/nvis/nvis-rack-status/-/pipelines)
[![Backend Coverage](https://gitlab-master.nvidia.com/nvis/nvis-rack-status/badges/feature/react-frontend-nvis-ux/coverage.svg?job=python-test&key_text=Backend+Coverage&key_width=130)](https://gitlab-master.nvidia.com/nvis/nvis-rack-status/-/jobs)
[![Frontend Coverage](https://gitlab-master.nvidia.com/nvis/nvis-rack-status/badges/feature/react-frontend-nvis-ux/coverage.svg?job=frontend-test&key_text=Frontend+Coverage&key_width=130)](https://gitlab-master.nvidia.com/nvis/nvis-rack-status/-/jobs)

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

---

## Docker

### Using the config file (recommended)

Edit `rack-config.env` with your site settings, then:

```bash
docker build -t nvis-rack-status:local .
docker run -d --name rack-status -p 5000:5000 \
  --env-file rack-config.env \
  -e RACK_SSH_HOST=<head-node-ip> \
  -e RACK_WEB_AUTH_TOKEN=<password> \
  -v ~/.ssh/id_rsa:/run/secrets/ssh_key:ro \
  nvis-rack-status:local
```

### Build and run helper script

```bash
./build-docker.sh          # dev: demo data, no auth, port 5050
./build-docker.sh prod     # prod: SSH to head node, uses rack-config.env
./build-docker.sh build    # build only
./build-docker.sh stop     # stop container
./build-docker.sh logs     # tail logs
```

### Docker Compose

```bash
cp rack-config.env rack-config.env.local   # edit with your settings
docker compose up -d
```

### Architecture

```
Container (Flask + React SPA)
  │
  │  RACK_REMOTE_BASE_PATH=/home/cmsupport/nvis-rack-status
  │  (resolves all script paths and log dir to the head node)
  │
  └── SSH ──► Head Node (BCM or MAAS)
                ├── sinfo / squeue / sacct / scontrol
                ├── cmsh (BCM) or maas-cli (MAAS)
                ├── pdsh
                └── bin/scripts/ (rack_status.sh, run-llama.sh, etc.)
```

Environment variables prefixed with `RACK_*`, `NEMO_*`, `VALIDATION_*`, and `SQUEUE_*` are automatically forwarded to the remote SSH session.

See **[INSTALL.md](INSTALL.md)** for full deployment instructions, SSH key setup, and troubleshooting.

---

## Testing

### Backend (pytest)

```bash
source .venv/bin/activate
pip install pytest pytest-cov
python -m pytest tests/ -v                     # run tests
python -m pytest tests/ --cov=. --cov-report=term  # with coverage
```

### Frontend (vitest)

```bash
cd frontend
npm test                    # run tests
npx vitest run --coverage   # with coverage
```

### CI/CD

The `.gitlab-ci.yml` pipeline runs on every push:

| Stage | Jobs |
|-------|------|
| **lint** | Python (ruff), TypeScript (tsc) |
| **test** | pytest (46 tests), vitest (13 tests) with JUnit reports |
| **build** | Docker image build (main branch + merge requests) |

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
| Backend Tests | pytest + pytest-cov |
| Frontend Tests | Vitest + Testing Library |
| CI/CD | GitLab CI (lint, test, build, push to NVCR) |

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
├── tests/                  # Python unit tests (pytest)
├── frontend/               # React SPA (Vite + TypeScript)
│   ├── src/components/     # Reusable components
│   ├── src/views/          # Page views (Dashboard, Multirack, Slurm, Analytics, etc.)
│   ├── src/context/        # Theme, auth, and command tracking contexts
│   ├── src/hooks/          # Custom React hooks
│   └── src/lib/            # Utility libraries (parsers, helpers)
├── templates/              # Legacy HTML dashboard
├── static/                 # Static assets (built SPA goes to static/spa/)
├── var/                    # Runtime logs and caches
├── rack-config.env         # Docker deployment config (edit per site)
├── Dockerfile              # Multi-stage build (Node + Ubuntu)
├── docker-compose.yml      # Compose with env_file support
├── build-docker.sh         # Build and run helper script
├── .gitlab-ci.yml          # CI/CD pipeline (lint, test, build)
├── INSTALL.md              # Docker deployment guide
└── requirements.txt        # Python dependencies
```

---

## Configuration

### Remote path (Docker SSH mode)

| Variable | Default | Description |
|----------|---------|-------------|
| `RACK_REMOTE_BASE_PATH` | — | Base directory on the head node. When set, all script paths and default log dir resolve from this path instead of the container's `/app/`. |
| `RACK_EXEC_MODE` | `local` | `local` or `ssh` |
| `RACK_PLATFORM` | `bcm` | `bcm` or `maas` |

### SSH connection

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

### Other

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | Flask listen port |
| `RACK_STATUS_DEMO` | `0` | `1` for synthetic demo data |
| `NEMO_SQSH_SRC` | — | Path to NeMo container image on head node |
| `RACK_SLURM_SKIP_MODULE_LOAD` | `0` | `1` if sinfo/squeue are already on PATH |
| `RACK_VALIDATION_CT_RANGE` | `01-18` | Tray range per rack (e.g. `01-02` for 2-node racks) |

Full variable reference: **[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)**

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
| `/api/run-llama-async` | POST | Launch Llama benchmark |
| `/api/run-nemo-xrack-async` | POST | Launch multirack validation |
| `/api/run-mnubergemm-stress-async` | POST | Launch stress test |
| `/api/power-reset` | POST | Power-cycle nodes |
| `/api/login` | POST | Token/PAM login |
| `/api/tail-log-stream` | POST | Stream log file (tail -f) |

---

## Further Reading

| Doc | Content |
|-----|---------|
| **[INSTALL.md](INSTALL.md)** | Docker deployment, SSH setup, troubleshooting |
| **[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md)** | All environment variables, timeouts, disable flags |
| **`.cursor/rules/`** | Design rules for KUI/MUI components, host_exec patterns, backend conventions |
| **`app.py` module docstring** | PAM, Grafana alignment, cache files |

---

## Screenshots

Parser results with InfiniBand table and embedded Grafana (GPU thermal / power):

![Parser results — IB table and Grafana](docs/screenshots/ui-parser-results-ib-grafana.png)

Main table with NEMO log modal; footer shows `backend: native_status.py` when the Python table is active:

![Rack table and NEMO log modal](docs/screenshots/ui-nemo-log-table.png)

Parser results — NCCL summary and all_reduce bandwidth chart:

![Parser results — NCCL charts](docs/screenshots/ui-parser-results-nccl.png)
