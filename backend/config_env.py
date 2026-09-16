# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Configuration resolution: where every knob in this product gets its value.

Author: Rafael M. Koike & Cursor Agent ("Ada")

``eget`` and friends are the readers, with ~120 call sites between them. The ladder, highest first:

    cluster "high" overlay  >  job_env_overrides.json  >  os.environ  >  platform "low" overlay  >  default

Pinned by ``backend/tests/test_config_precedence.py``, which was written against the previous
in-``app.py`` implementation and is re-run unchanged against this one. Read it before altering the
order: several asymmetries are deliberate (a blank overlay falls through, but ``FOO=`` in the
process environment is an explicit empty value and wins).

**The writer stays in ``app.py``.** ``_job_env_overrides_write`` has to invalidate the rack-status
cache, which is application state this module has no business knowing about. Splitting reader from
writer is what let this move happen at all: the reader needs nothing from ``app``.

Except one thing, which is why ``set_materialize_hook`` exists. Materialising overrides into
``os.environ`` strips Settings-managed keys, so the active catalog overlay has to be re-applied
afterwards or platform-derived values vanish. That re-apply lives in ``app.py``
(``_apply_catalog_active_to_environ``, which reaches the status cache). It used to be reached from
here by ``globals().get("_apply_catalog_active_to_environ")`` -- a late lookup that only worked
because both were in one file. It is now injected, which is the same behaviour said out loud and
testable.

Mind the re-entrancy guard in ``job_env_overrides_load``: the hook loads the overrides again, and
with no overrides file there is no mtime to cache, so nothing terminates the recursion and the
process runs out of file descriptors. See ``backend/tests/test_job_env_missing_file.py``.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from core import truthy_env as _truthy_env

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent

_logger = logging.getLogger(__name__)

# Optional, exactly as in app.py: without a catalog the ladder is simply shorter.
try:
    from catalog import context as catalog_context

    _CATALOG_AVAILABLE = True
except Exception as _catalog_exc:  # pragma: no cover - defensive
    _logger.warning("catalog unavailable in config_env: %s", _catalog_exc, exc_info=True)
    catalog_context = None  # type: ignore[assignment]
    _CATALOG_AVAILABLE = False

#: Re-applies the active catalog overlay after materialization. Injected by ``app.py``; see the
#: module docstring for why it cannot simply be imported.
_materialize_hook = None


def set_materialize_hook(fn) -> None:
    """Register the post-materialization catalog re-apply. Idempotent; last registration wins."""
    global _materialize_hook
    _materialize_hook = fn


JOB_ENV_UI_DEFINITIONS: list[dict[str, str]] = [
    {
        "section": "dashboard",
        "key": "RACK_STATUS_BACKGROUND_INTERVAL_SEC",
        "label": "Status background refresh (seconds)",
        "description": "How often the server refreshes cached native rack status (min 5). The background loop re-reads this after each sleep, so saves apply within roughly one interval (restart for an immediate first tick).",
    },
    {
        "section": "dashboard",
        "key": "RACK_VALIDATION_ACTIVITIES_REFRESH_INTERVAL_SEC",
        "label": "Validation activity refresh (seconds)",
        "description": "How often the server reconciles unseen open validation tasks against Slurm and their result logs (min 5).",
    },
    {
        "section": "dashboard",
        "key": "RACK_VALIDATION_ACTIVITIES_REFRESH_BUDGET_SEC",
        "label": "Validation refresh budget (seconds)",
        "description": "Maximum wall-clock budget per whole-store validation activity refresh cycle (default 3).",
    },
    {
        "section": "dashboard",
        "key": "RACK_VALIDATION_ACTIVITIES_REFRESH_DISABLED",
        "label": "Disable validation activity refresh",
        "description": "Set to 1 only for maintenance/testing; open activities otherwise reconcile in the background even when no browser views their rack.",
    },
    {
        "section": "dashboard",
        "key": "RACK_WEB_PROJECT_NAME",
        "label": "Page title suffix",
        "description": "Optional short label appended to HTML titles (shell PROJECT_NAME is still read if this is unset).",
    },
    {
        "section": "nvftp",
        "key": "RACK_PROJECT_TOKEN",
        "label": "NVFTP project folder name",
        "description": (
            "Top-level folder on the NVFTP remote path for this site (e.g. mistral-p2). "
            "var/cache and rack_comments.json mirror to /nvis-project-view/<token>/cache/ every RACK_NVFTP_SYNC_INTERVAL_SEC. "
            "Leave empty to disable uploads."
        ),
    },
    {
        "section": "nvftp",
        "key": "RACK_NVFTP_SFTP_USER",
        "label": "NVFTP SFTP username",
        "description": (
            "SFTP user from your NVFTP workspace. "
            "Password is the token saved via Overview → Set Project View."
        ),
    },
    {
        "section": "nvftp",
        "key": "RACK_NVFTP_REMOTE_BASE",
        "label": "NVFTP remote base path",
        "description": (
            "Optional POSIX prefix before /nvis-project-view/{RACK_PROJECT_TOKEN}/ on transfer.nvftp.nvidia.com. "
            "Leave empty for the default /nvis-project-view/… layout."
        ),
    },
    {
        "section": "nvftp",
        "key": "RACK_NVFTP_SYNC_INTERVAL_SEC",
        "label": "NVFTP cache sync interval (seconds)",
        "description": "How often var/cache is uploaded when sync is enabled (default 7200 = 2 hours, min 300).",
    },
    {
        "section": "nvftp",
        "key": "RACK_NVFTP_SFTP_HOST",
        "label": "NVFTP SFTP host",
        "description": "SFTP hostname (default transfer.nvftp.nvidia.com).",
    },
    {
        "section": "external",
        "key": "RACK_PROMETHEUS_LOG_TIMESTAMP_TZ",
        "label": "Log timestamp timezone (IANA)",
        "description": "Zone for naive timestamps in NEMO logs and Grafana axis defaults when RACK_WEB_GRAFANA_TIMEZONE is unset (e.g. UTC, America/Los_Angeles).",
    },
    {
        "section": "external",
        "key": "RACK_PROMETHEUS_BASIC_AUTH_USER",
        "label": "Prometheus basic auth username",
        "description": "Same variable as USER in ~/.prometheusrc (Bright Prometheus). Used by Utility → launch Grafana script when set together with the password; leave both empty to use PROMETHEUS_RC / default rc paths on the server.",
    },
    {
        "section": "external",
        "key": "RACK_PROMETHEUS_BASIC_AUTH_PASSWORD",
        "label": "Prometheus basic auth password",
        "description": "Same variable as PASSWORD in ~/.prometheusrc. Stored in server overrides like other settings; restrict file permissions on var/cache/job_env_overrides.json.",
    },
    {
        "section": "external",
        "key": "NGC_API_KEY",
        "label": "NGC API key",
        "description": "NGC personal API key (nvapi-…) used by Tools → Download NEMO container (enroot import). Saved in server overrides like other settings; restrict file permissions on var/cache/job_env_overrides.json. Forwarded to the cluster when running in SSH mode.",
    },
    {
        "section": "jobs_cluster",
        "key": "LLMB_INSTALL",
        "label": "LLMB workspace (LLMB_INSTALL)",
        "description": "dgxc-benchmarking / Exemplar WORKSPACE directory on the head node (e.g. /home/<user>/workspace/llmb) — the dir llmb-run reads/writes (.cache, workloads, experiments). Forwarded over SSH to run_llmb_nccl.sh (NCCL microbenchmark). Set this to override any stale LLMB_INSTALL inherited from the launching shell.",
    },
    {
        "section": "jobs_cluster",
        "key": "LLMB_REPO",
        "label": "LLMB repo (llmb-run binary)",
        "description": "Optional: dgxc-benchmarking repo dir holding the llmb-run binary (e.g. /home/<user>/workspace/dgxc-benchmarking). Auto-detected as a sibling of LLMB_INSTALL when empty; set only if the repo lives elsewhere.",
    },
    {
        "section": "jobs_cluster",
        "key": "LLMB_VENV",
        "label": "LLMB venv (optional)",
        "description": "Optional: virtualenv activated before llmb-run. Leave empty for uv-managed installs (the workspace's llmb-run symlink resolves the correct venv via its shebang).",
    },
    {
        "section": "jobs_cluster",
        "key": "LLMB_RUN",
        "label": "LLMB executable (optional)",
        "description": "Absolute reviewed llmb-run executable. When absent or unreadable, the containerized NCCL adapter uses the hash-pinned NVFTP five-collective fallback.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_SCRIPT_SUDO",
        "label": "Run privileged tools with sudo",
        "description": "Set to 1 to escalate root-requiring node tools (sysinfo, hardware checks, switch/IMEX/BMC) with sudo on the privileged 'script' route. Use when the SSH/login user is unprivileged but has sudo (e.g. cmods with passwordless sudo on the compute trays). Auto-enabled when a sudo password is set.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_SUDO_PASSWORD",
        "label": "Sudo password (optional)",
        "description": "Password for sudo -S when the sudoers policy is NOT passwordless. Leave empty for NOPASSWD sudo (uses sudo -n). Stored in server overrides like other settings; restrict file permissions on var/cache/job_env_overrides.json.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_SUDO_PRESERVE_ENV",
        "label": "Preserve env across sudo (sudo -E)",
        "description": "Default 1: keep the forwarded platform/site env across the sudo boundary (sudo -E). Set to 0 only if the sudoers policy rejects -E (env_reset without SETENV), in which case tools receive a clean root environment.",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_URI",
        "label": "Grafana base URL",
        "description": "Full origin without trailing slash (e.g. https://grafana.example). Empty uses same host as the app.",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_PORT",
        "label": "Grafana port",
        "description": "Port when Grafana is on the same host as the browser (default 3000).",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_DASH_UID",
        "label": "Grafana dashboard UID",
        "description": "UID path segment for embedded GPU / thermal panels (default bright-gpu-per-host).",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_DASH_SLUG",
        "label": "Grafana dashboard slug",
        "description": "Slug in Grafana URLs for the embedded dashboard (default bright-gpu-thermal-and-power).",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_TIMEZONE",
        "label": "Grafana timezone query",
        "description": "browser, utc, or an IANA zone. When empty, the server uses the same zone as RACK_PROMETHEUS_LOG_TIMESTAMP_TZ.",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_VAR_RACK",
        "label": "Grafana rack variable",
        "description": "Template variable name used to scope deep-linked dashboards by rack (default 'Rack' for BCM/NMC dashboards). Sent as var-<name>=<rack id>.",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_VAR_HOST",
        "label": "Grafana host variable",
        "description": "Template variable name used to scope deep-linked dashboards by node/host (default 'hostname' for BCM/NMC dashboards). Sent as var-<name>=<node>.",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS",
        "label": "Grafana deep-link dashboards (JSON)",
        "description": "JSON list of dashboards to expose as 'Open in Grafana' deep-links, e.g. [{\"id\":\"dcgm\",\"label\":\"GPU / DCGM\",\"uid\":\"...\",\"slug\":\"...\",\"scope\":\"host\"}]. scope = host|rack|cluster. Empty uses the built-in BCM/NMC GB200 catalog (DCGM, power, NVLink, health, Slurm, overview).",
    },
    {
        "section": "external",
        "key": "RACK_WEB_GRAFANA_DEEPLINK_DISABLED",
        "label": "Disable Grafana deep-links",
        "description": "Set to 1 to hide the 'Open in Grafana' deep-link menus in the rack table and parser results.",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_STATUS_SCRIPT",
        "label": "rack_status.sh path",
        "description": "Main rack status script (native table and probe-json).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_STATUS_EXTRA_ARGS",
        "label": "rack_status.sh extra args",
        "description": "Extra arguments appended to rack_status.sh (shell-split).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_PARSER_SCRIPT",
        "label": "parser.sh path",
        "description": "Log parser script for /api/parser-results.",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_PARSER_EXTRA_ARGS",
        "label": "parser.sh extra args",
        "description": "Extra arguments before --json (shell-split).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_PARSER_EXPORT_GLOB",
        "label": "Parser export glob",
        "description": "Basename glob under log dir for exports (default *.txt).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_PARSER_TIMEOUT",
        "label": "Parser timeout (seconds)",
        "description": "Timeout for parser.sh --json in the parser API.",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_PARSER_EXCEL_REPORT_DIR",
        "label": "Excel report directory",
        "description": "Output directory for Excel exports (optional).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_MULTIRACK_SITE_PREFIX",
        "label": "Multirack site prefix",
        "description": "Pod id for multirack log names without pN- (default p5).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_MULTIRACK_VALIDATION_TEST",
        "label": "Multirack probe VALIDATION_TEST",
        "description": "Default VALIDATION_TEST for multirack probe-json (e.g. nccl_all_reduce).",
    },
    {
        "section": "native_cmsh",
        "key": "RACK_SLURM_SKIP_MODULE_LOAD",
        "label": "Skip module load for Slurm",
        "description": (
            "Set to 1 if squeue/sinfo/scontrol are already on PATH (avoids ``module load slurm`` and "
            "bash-completion errors on the head node)."
        ),
    },
    {
        "section": "native_cmsh",
        "key": "RACK_SLURM_SUBMIT_UID",
        "label": "Slurm validation submit UID",
        "description": (
            "Numeric cluster user ID used for validation allocations submitted by a root "
            "control-plane container. Must be set together with RACK_SLURM_SUBMIT_GID."
        ),
    },
    {
        "section": "native_cmsh",
        "key": "RACK_SLURM_SUBMIT_GID",
        "label": "Slurm validation submit GID",
        "description": (
            "Numeric cluster group ID used for validation allocations submitted by a root "
            "control-plane container. Must be set together with RACK_SLURM_SUBMIT_UID."
        ),
    },
    {
        "section": "native_cmsh",
        "key": "RACK_SLURM_SUBMIT_HOME",
        "label": "Slurm validation submit home",
        "description": (
            "Absolute cluster home directory exported with validation allocations. This must "
            "match the submit UID so Pyxis/Enroot resolves the existing user configuration."
        ),
    },
    *[
        {
            "section": "native_cmsh",
            "key": f"RACK_SLURM_{name.upper()}",
            "label": f"{name} executable",
            "description": (
                f"Absolute {name} path used by every backend scheduler call. Set this when "
                "Slurm is not on the non-login shell PATH."
            ),
        }
        for name in ("sbatch", "srun", "sinfo", "squeue", "sacct", "scontrol", "scancel")
    ],
    {
        "section": "native_cmsh",
        "key": "RACK_MODULES_INIT",
        "label": "Environment modules init script",
        "description": (
            "Optional path to source before ``module load`` (e.g. "
            "/cm/local/apps/environment-modules/current/init/bash). Leave empty for auto-detect."
        ),
    },
    {
        "section": "native_cmsh",
        "key": "RACK_CMSH",
        "label": "cmsh executable",
        "description": "Bright cmsh command (power reset, probes, cluster overview).",
    },
    {
        "section": "native_cmsh",
        "key": "NVIS_RACK_ID",
        "label": "Rack id pattern",
        "description": (
            "How a rack row is named in the dashboard. Default: [\\d+] (digits as in cmsh). "
            "Bright default: p[\\d+]-r[\\d+] (e.g. p5-r03). Optional [01-99] for zero-padded two-digit fields. "
            "CBR default: CBR[\\d+]-DH[\\d+]-SVR-CN[01-99][01-99]."
        ),
    },
    {
        "section": "native_cmsh",
        "key": "NVIS_CT_ID",
        "label": "Compute tray hostname pattern",
        "description": (
            "Full compute node name from cmsh ds (must start with the rack pattern and add tray slot(s) at the end). "
            "Bright default: p[\\d+]-r[\\d+]-ct[\\d+] (e.g. p5-r03-ct12). "
            "CBR default: CBR[\\d+]-DH[\\d+]-SVR-CN[01-99][01-99][01-99]."
        ),
    },
    {
        "section": "native_cmsh",
        "key": "NVIS_NVSW_ID",
        "label": "NVSwitch hostname pattern (optional)",
        "description": (
            "Naming pattern for NVSwitch devices (reserved for future UI). "
            "Bright default: p[\\d+]-r[\\d+]-nvsw-[\\d+] (e.g. p03-r13-nvsw-9). "
            "CBR default: CBR[\\d+]-DH[\\d+]-NSW-NV[01-99][01-99][01-99]."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "VALIDATION_TEST",
        "label": "Validation test suite (parser + rack status)",
        "description": (
            "Comma-separated tokens for NCCL/LLM pass (see bin/scripts/validation_test_env.sh). "
            "Typical single-rack NEMO logs: nccl_all_reduce,llm. Full NCCL sweep: "
            "nccl_all_reduce,nccl_alltoall,nccl_allgather,llm. When unset, the server defaults to "
            "nccl_all_reduce,llm for singlerack runs (multirack probe-json uses RACK_MULTIRACK_VALIDATION_TEST only)."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_VALIDATION_CT_RANGE",
        "label": "Validation CT range",
        "description": "Tray indices inside pNN-rMM-ct[…] for singlerack validation (e.g. 01-18).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_VALIDATION_SCRIPT",
        "label": "Singlerack validation script",
        "description": "Path to run_singlerack.sh (or site equivalent).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_VALIDATION_TIMEOUT",
        "label": "Validation timeout (seconds)",
        "description": "Max wall time for synchronous / async singlerack validation.",
    },
    {
        "section": "jobs_cluster",
        "key": "NEMO_SQSH_SRC",
        "label": "NEMO container .sqsh path",
        "description": "Enroot image path or URI fragment; used for Setup defaults and skip checks.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_VALIDATION_CONTAINER_ROOT",
        "label": "Validation shared container root",
        "description": "Shared root for code-allowlisted validation image filenames, sizes, and SHA256 digests.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_VALIDATION_NODE_CONTAINER_DIR",
        "label": "Validation node container cache",
        "description": "Node-local destination for allowlisted validation images (production default /opt/containers).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_VALIDATION_PROVENANCE_ROOT",
        "label": "Validation script provenance root",
        "description": "Read-only archive root recording the reviewed NVFTP scripts used to qualify live wrappers.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_STATUS_LOG_DIR",
        "label": "Shared validation log root",
        "description": "Default shared log directory used by validation launchers and result collection.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_STATUS_ALLOWED_PREFIXES",
        "label": "Allowed validation log prefixes",
        "description": "Comma-separated absolute roots accepted by validation log path resolution.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NEMO_XRACK_SCRIPT",
        "label": "Multinode NEMO script",
        "description": "Path to run-nemo-xrack.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NEMO_XRACK_TIMEOUT",
        "label": "NEMO xrack timeout (seconds)",
        "description": "Background job timeout for multinode NEMO.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_MULTIRACK_CAMPAIGN_NODES_PER_RACK",
        "label": "Multirack nodes per rack",
        "description": (
            "Exact number of idle eligible nodes selected from each rack for multirack "
            "operations. Defaults to 16; single-rack validation still uses full rack geometry."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_LLAMA_SCRIPT",
        "label": "Llama script",
        "description": "Path to run-llama.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_LLAMA_TIMEOUT",
        "label": "Llama timeout (seconds)",
        "description": "Background job timeout for Llama runs.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_MNUBERGEMM_STRESS_SCRIPT",
        "label": "MNU Bergemm stress script",
        "description": "Path to run_mnubergemm_stress.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_MNUBERGEMM_CUDA_MAJOR",
        "label": "MNUBERGEMM stress CUDA major",
        "description": "12 or 13; selects HPC-X unpack under NVIS_WORKSPACE_DIR (default 13). Falls back to RACK_NVLOOM_CUDA_MAJOR.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_MNUBERGEMM_PARTITION",
        "label": "MNUBERGEMM stress Slurm partition",
        "description": "If set, run_mnubergemm_stress.sh passes sbatch -p … (omit for site default).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVLOOM_SINGLERACK_SCRIPT",
        "label": "nvloom singlerack driver script",
        "description": "Path to run_nvloom_singlerack.sh (Slurm sbatch --wait; logs under log_dir/nvloom/).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVLOOM_SINGLERACK_TIMEOUT",
        "label": "nvloom singlerack timeout (seconds)",
        "description": "Background job timeout for nvloom Slurm wrapper (sbatch --wait).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVLOOM_RACK_SAMPLES",
        "label": "nvloom gpuToRackSamples",
        "description": "Integer 1–256 for --gpuToRackSamples (default 5). Passed as RACK_SAMPLES.",
    },
    {
        "section": "jobs_cluster",
        "key": "NVIS_NVLOOM_UCX_TLS",
        "label": "nvloom UCX_TLS override",
        "description": (
            "Optional UCX_TLS string for the nvloom srun step (Spectrum-X / RoCE). "
            "When unset, nvloom_singlerack.sbatch uses rc,cuda_copy,cuda_ipc,cuda,self only if UCX_TLS is not already set."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_SCRIPT",
        "label": "ClusterKit singlerack driver script",
        "description": "Path to run_clusterkit.sh (Slurm; logs under log_dir/clusterkit/).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_TIMEOUT",
        "label": "ClusterKit singlerack timeout (seconds)",
        "description": "Background job timeout for ClusterKit Slurm wrapper (default 14400).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_PARTITION",
        "label": "ClusterKit Slurm partition",
        "description": "If set, run_clusterkit.sh passes sbatch -p … (omit for site default).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_CUDA_MAJOR",
        "label": "ClusterKit CUDA major (HPC-X bundle)",
        "description": "12 or 13; passed as CUDA_MAJ (default 13). Falls back to RACK_NVLOOM_CUDA_MAJOR.",
    },
    {
        "section": "jobs_cluster",
        "key": "NVIS_RDMA_LINK_TECH",
        "label": "RDMA link technology (global)",
        "description": (
            "auto | mlx | ib | roce | spectrumx | spx. Default auto: InfiniBand-style mlx5 sysfs when usable, else RoCE/Spectrum-X. "
            "Other validation scripts may source nvis-rdma-common.inc.sh; ClusterKit uses this when RACK_CLUSTERKIT_LINK_TECH is unset."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_LINK_TECH",
        "label": "ClusterKit interconnect mode",
        "description": (
            "Same values as NVIS_RDMA_LINK_TECH; overrides the global default for singlerack ClusterKit only. "
            "Use roce or spx on Spectrum-X; use mlx or ib for classic IB mlx5 local_cpulist mapping."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_HCA_PROFILE",
        "label": "ClusterKit HCA preset profile",
        "description": (
            "Preset rail table: mlx — gb300_mlx4 (default), b300_mlx8; roce — gb300_roce_spx_quad (default), "
            "gb300_roce_spx_rail, b300_roce_dual_8. Must match RACK_CLUSTERKIT_LINK_TECH / NVIS_RDMA_LINK_TECH family."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_CLUSTERKIT_UCX_TLS",
        "label": "ClusterKit UCX_TLS (RoCE branch)",
        "description": "Optional. When interconnect is RoCE/Spectrum-X, clusterkit.sbatch sets UCX_TLS to this value (default rc,cuda).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_LLMB_NCCL_SCRIPT",
        "label": "NCCL Microbenchmark (LLMB) driver script",
        "description": "Path to run_llmb_nccl.sh (resolves NVIS rack ids to Slurm nodes, then runs llmb-run microbenchmark_nccl).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_LLMB_NCCL_TIMEOUT",
        "label": "NCCL Microbenchmark (LLMB) timeout (seconds)",
        "description": "Background job timeout for the LLMB NCCL microbenchmark wrapper (default 14400).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_LLMB_NCCL_RESERVATION",
        "label": "NCCL Microbenchmark (LLMB) Slurm reservation",
        "description": "If set, run_llmb_nccl.sh passes --reservation … (omit for none).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NCCL_MPI_SIZE_MAX",
        "label": "Multi-rack NCCL max message size",
        "description": "Max all_reduce message size for the direct x-rack NCCL test (run_nccl_mpi.sh --size-max). Default 16G. Accepts nccl-tests sizes like 8, 512M, 16G.",
    },
    {
        "section": "jobs_cluster",
        "key": "NVIS_EXEMPLAR_REPO_BRANCH",
        "label": "Exemplar/llmb repo tag",
        "description": (
            "git tag/branch of NVIDIA/dgxc-benchmarking for Tools > Download/Setup Exemplar. Empty "
            "= the tool's default v25.10.01, whose nccl recipe supports gb200/b200/h100 only. "
            "v26.06.00 adds native gb300 and b300 blocks (verified upstream 2026-07-28), which is "
            "what a GB300 NVL72 needs. The tool's form has no field for this."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "NVIS_EXEMPLAR_WORKLOADS",
        "label": "Exemplar/llmb workloads to install",
        "description": (
            "Comma-separated llmb workloads for Tools > Download/Setup Exemplar. Empty = the stock "
            "default 'microbenchmark_nccl,pretrain_nemotron-h'. Set to 'microbenchmark_nccl' to "
            "install ONLY the NCCL microbenchmark and skip the NeMo container download, which is "
            "what makes the install action take hours. Add pretrain workloads later when needed."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "NVIS_NCCL_VERSION",
        "label": "NCCL version (build + run)",
        "description": (
            "Selects the NCCL used by BOTH the workspace build and the NCCL test run. "
            "'system' (or 2.28.9) builds/loads <workspace>/nccl/build/lib; any other X.Y.Z uses "
            "<workspace>/nccl-X.Y.Z/nccl/build/lib. The Tools > CUDA/NCCL installer checks out tag "
            "v<X.Y.Z>-1 and fails loudly listing real tags if it does not exist. Build the version "
            "you intend to run: selecting a version with no build on disk makes every NCCL job exit 2 "
            "with 'libnccl.so.2 not found'."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "NVIS_SLURM_HOSTLIST",
        "label": "Slurm sbatch -w override (comma-separated)",
        "description": "Optional. If set, run_clusterkit.sh / run_nvloom_singlerack.sh / run_mnubergemm_stress.sh use this exact NodeName list for sbatch -w instead of expanding the validation target. Use when Slurm NodeNames differ from NVIS hostnames (FQDN, prefix, or casing). Leave empty to auto-expand via scontrol.",
    },
    {
        "section": "jobs_cluster",
        "key": "NVIS_WORKSPACE_DIR",
        "label": "Workspace root (HPC-X / CUDA / nvloom)",
        "description": "Absolute path; default /home/cmsupport/workspace. Used by nvloom singlerack, MNUBERGEMM stress, and install scripts (TOPDIR).",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_WORKSPACE_DIR",
        "label": "Workspace root (alias)",
        "description": "Optional; if NVIS_WORKSPACE_DIR is unset, this sets the same workspace path.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVLOOM_CUDA_MAJOR",
        "label": "nvloom CUDA major (HPC-X bundle)",
        "description": '12 or 13; selects hpcx unpack name under the workspace (default 13). Passed as CUDA_MAJ.',
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_SYSINFO_SCRIPT",
        "label": "sysinfo / Precheck script",
        "description": "Path to sysinfo.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_SYSINFO_TIMEOUT",
        "label": "sysinfo timeout (seconds)",
        "description": "Timeout for Precheck / sysinfo streams.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_HW_CHECK_SCRIPT",
        "label": "Hardware check (pdsh) script",
        "description": "Path to hw_check_pdsh.sh when set; default uses bundled script.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_GPU_RESET_SCRIPT",
        "label": "GPU reset script",
        "description": "Site GPU reset script path.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVSWITCH_AUDIT_SCRIPT",
        "label": "NVSwitch audit script",
        "description": "Path to nvswitches_rack_audit.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVSWITCH_SDN_RESET_SCRIPT",
        "label": "NVSwitch SDN reset script",
        "description": "Path to nvswitch_sdn_reset.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVSWITCH_SYSTEM_RESET_SCRIPT",
        "label": "NVSwitch system reset script",
        "description": "Path to nvswitch_system_reset.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_IMEX_RESET_SCRIPT",
        "label": "IMEX reset script",
        "description": "Path to imex_reset.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_IMEX_CHECK_SCRIPT",
        "label": "IMEX check script",
        "description": "Path to imex_check.sh.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_NVLMAPPER_REPORTS_BASE",
        "label": "NVLMapper reports base",
        "description": "Directory under which NVLMapper stages per-rack work.",
    },
    {
        "section": "jobs_cluster",
        "key": "NVLMAPPER_RC",
        "label": "NVLMapper credentials file",
        "description": (
            "Path to key=value file (username= / user=, password=) merged into generated YAML by run_nvlmapper.sh. "
            "Default /root/.nvlmapperrc. Ignored when NVLMAPPER_USERNAME and NVLMAPPER_PASSWORD are both set."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "NVLMAPPER_USERNAME",
        "label": "NVLMapper username",
        "description": (
            "BMC/switch user for nvlmapper when not using a credentials file alone. When set together with "
            "NVLMAPPER_PASSWORD, overrides NVLMAPPER_RC file contents for the merge step."
        ),
    },
    {
        "section": "jobs_cluster",
        "key": "NVLMAPPER_PASSWORD",
        "label": "NVLMapper password",
        "description": "Password paired with NVLMAPPER_USERNAME. Stored in server overrides like Prometheus auth; restrict permissions on var/cache/job_env_overrides.json.",
    },
    {
        "section": "jobs_cluster",
        "key": "RACK_SLURM_DRAIN_REASON",
        "label": "Slurm drain reason",
        "description": "Reason string passed to scontrol drain (default admin).",
    },
]
_JOB_ENV_ALLOWED_KEYS: frozenset[str] = frozenset(d["key"] for d in JOB_ENV_UI_DEFINITIONS)
# Snapshot of process env for allowed keys at import (used when an override is cleared in Settings).
_JOB_ENV_SHELL_BASELINE: dict[str, str | None] = {k: os.environ.get(k) for k in _JOB_ENV_ALLOWED_KEYS}
# Changes to these keys affect the cached native / shell ``/api/status`` payload; others skip a full bust.
_STATUS_ROW_ENV_KEYS: frozenset[str] = frozenset(
    {
        "RACK_STATUS_SCRIPT",
        "RACK_STATUS_EXTRA_ARGS",
        "RACK_CMSH",
        "NVIS_RACK_ID",
        "NVIS_CT_ID",
        "NVIS_NVSW_ID",
        "RACK_SLURM_SKIP_MODULE_LOAD",
        "RACK_MODULES_INIT",
        "RACK_MULTIRACK_SITE_PREFIX",
        "RACK_MULTIRACK_VALIDATION_TEST",
        "VALIDATION_TEST",
        "RACK_PARSER_SCRIPT",
        "RACK_PARSER_EXTRA_ARGS",
        "RACK_PARSER_EXPORT_GLOB",
        "RACK_PARSER_TIMEOUT",
    }
)
_job_env_file_lock = threading.Lock()
_JOB_ENV_OVERRIDES_CACHE: dict[str, str] | None = None
_JOB_ENV_OVERRIDES_MTIME: float | None = None


def _materialize_job_env_overrides_into_os_environ(ov: dict[str, str]) -> None:
    """Copy dashboard JSON overrides into ``os.environ`` so ``native_status`` / ``host_exec`` inherit them."""
    for k in _JOB_ENV_ALLOWED_KEYS:
        if k in ov and ov[k].strip():
            os.environ[k] = ov[k].strip()[:8192]
        else:
            base = _JOB_ENV_SHELL_BASELINE.get(k)
            if base is not None:
                os.environ[k] = base
            else:
                os.environ.pop(k, None)
    # This strips Settings-managed keys not present in the overrides; re-apply the active
    # catalog overlay so platform/cluster-derived values (e.g. RACK_VALIDATION_CT_RANGE)
    # survive. Only runs on cache-miss materialization, so this is infrequent.
    if callable(_materialize_hook):
        try:
            _materialize_hook()
        except Exception:
            _logger.debug("_materialize_job_env_overrides_into_os_environ: ignoring error", exc_info=True)


def _job_env_status_payload_needs_rebuild(before: dict[str, str], after: dict[str, str]) -> bool:
    for k in _STATUS_ROW_ENV_KEYS:
        if (before.get(k) or "").strip() != (after.get(k) or "").strip():
            return True
    return False


def _job_env_overrides_path() -> Path:
    return (REPO_ROOT / "var" / "cache" / "job_env_overrides.json").resolve()


def _job_env_flock_path() -> Path:
    """Cross-process lock so Gunicorn workers / NFS do not read partially updated overrides."""
    return (_job_env_overrides_path().parent / "job_env_overrides.flock").resolve()


@contextmanager
def _job_env_flock(shared: bool = True):
    """``shared=True`` (LOCK_SH) for readers, ``False`` (LOCK_EX) for writers."""
    if _truthy_env("RACK_STATUS_DEMO"):
        yield
        return
    p = _job_env_flock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a+", encoding="utf-8")  # noqa: SIM115 - handle must outlive this scope: it holds the flock
    locked = False
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            locked = True
        except (OSError, AttributeError):
            pass
        yield
    finally:
        if locked:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            f.close()
        except OSError:
            pass
def _read_job_env_file_raw(p: Path) -> dict[str, str]:
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if k not in _JOB_ENV_ALLOWED_KEYS:
            continue
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:8192]
        elif v is not None and str(v).strip():
            out[k] = str(v).strip()[:8192]
    return out


def _job_env_overrides_load() -> dict[str, str]:
    """Effective overrides saved from Settings (merged on top of process env for jobs)."""
    global _JOB_ENV_OVERRIDES_CACHE, _JOB_ENV_OVERRIDES_MTIME
    p = _job_env_overrides_path()
    # Keep native_status on the same file as this process (covers forked workers / odd env).
    os.environ["NVIS_JOB_ENV_OVERRIDES_FILE"] = str(p)
    with _job_env_flock(shared=True):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            # No overrides file: a fresh install, or Settings never saved anything.
            #
            # Materialising re-enters this function -- it calls _apply_catalog_active_to_environ,
            # which loads the overrides again -- and with no file there is no mtime to cache, so
            # the early return below is never reached and the recursion does not terminate. Each
            # level holds the flock handle opened above, so it climbs until the process runs out
            # of file descriptors: "OSError: [Errno 24] Too many open files". Invisible wherever
            # the file happens to exist, which is every deployment, and immediate on a clean
            # checkout.
            #
            # So materialise only when this is news. Once the cache says "empty", a re-entrant
            # call has nothing to do and unwinds.
            with _job_env_file_lock:
                already_absent = _JOB_ENV_OVERRIDES_CACHE == {} and _JOB_ENV_OVERRIDES_MTIME is None
                _JOB_ENV_OVERRIDES_CACHE = {}
                _JOB_ENV_OVERRIDES_MTIME = None
            if not already_absent:
                _materialize_job_env_overrides_into_os_environ({})
            return {}
        with _job_env_file_lock:
            if _JOB_ENV_OVERRIDES_CACHE is not None and _JOB_ENV_OVERRIDES_MTIME == mtime:
                return dict(_JOB_ENV_OVERRIDES_CACHE)
        data = _read_job_env_file_raw(p)
        with _job_env_file_lock:
            _JOB_ENV_OVERRIDES_CACHE = data
            _JOB_ENV_OVERRIDES_MTIME = mtime
        _materialize_job_env_overrides_into_os_environ(data)
        return dict(data)


def _job_env_overrides_invalidate_cache() -> None:
    global _JOB_ENV_OVERRIDES_CACHE, _JOB_ENV_OVERRIDES_MTIME
    with _job_env_file_lock:
        _JOB_ENV_OVERRIDES_CACHE = None
        _JOB_ENV_OVERRIDES_MTIME = None
def _eget(name: str, default: str | None = None) -> str | None:
    """Environment getter with catalog + dashboard Settings overrides.

    Precedence (highest first): active cluster identity/naming/connection (catalog
    ``high`` overlay) > dashboard Settings overrides (``job_env_overrides.json``) >
    process env > platform geometry/tool defaults (catalog ``low`` overlay) > *default*.
    """
    high: dict[str, str] = {}
    low: dict[str, str] = {}
    if _CATALOG_AVAILABLE and catalog_context is not None:
        try:
            high, low = catalog_context.overlays_for_active()
        except Exception:
            _logger.debug("_eget: ignoring error", exc_info=True)
            high, low = {}, {}
    if name in high and high[name].strip():
        return high[name].strip()
    ov = _job_env_overrides_load()
    if name in ov and ov[name].strip():
        return ov[name].strip()
    v = os.environ.get(name)
    if v is not None:
        return v
    if name in low and low[name].strip():
        return low[name].strip()
    return default


def _eget_int(name: str, default: int) -> int:
    """Like ``_env_int`` but honors catalog overlays + Settings overrides via ``_eget``.

    Used for platform-geometry-derived knobs (CT range count, GPUs/tray, nodes/rack)
    so the active platform's geometry applies unless explicitly overridden.
    """
    raw = _eget(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip(), 10)
    except ValueError:
        return default


def _subprocess_env(
    updates: dict[str, str] | None = None,
    *,
    validation_test_if_unset: str = "nccl_all_reduce,llm",
) -> dict[str, str]:
    high: dict[str, str] = {}
    low: dict[str, str] = {}
    if _CATALOG_AVAILABLE and catalog_context is not None:
        try:
            high, low = catalog_context.overlays_for_active()
        except Exception:
            _logger.debug("_subprocess_env: ignoring error", exc_info=True)
            high, low = {}, {}
    # Precedence (later wins): low platform defaults < process env < Settings overrides
    # < active cluster identity < per-call updates.
    out: dict[str, str] = {}
    out.update(low)
    out.update(os.environ)
    out.update(_job_env_overrides_load())
    out.update(high)
    if updates:
        out.update(updates)
    # validation_test_env.sh treats unset as a very strict suite (all NCCL collectives + LLM), which
    # often marks racks pending when logs only have all-reduce + Nemotron. Callers may pass a
    # different *validation_test_if_unset* (e.g. multirack probe uses RACK_MULTIRACK_VALIDATION_TEST).
    if not (out.get("VALIDATION_TEST") or "").strip():
        d = (validation_test_if_unset or "nccl_all_reduce,llm").strip() or "nccl_all_reduce,llm"
        out["VALIDATION_TEST"] = d
    return out
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip(), 10)
    except ValueError:
        return default


def _job_env_int(name: str, default: int) -> int:
    """Like ``_env_int`` but prefers values saved under Settings → job environment."""
    ov = _job_env_overrides_load()
    if name in ov:
        raw = ov[name]
        if raw and str(raw).strip():
            try:
                return int(str(raw).strip(), 10)
            except ValueError:
                return default
    return _env_int(name, default)
