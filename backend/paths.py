# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Where the scripts and the logs live -- on whichever host actually owns them.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Every path here names a file on the *execution host*, which is usually not this machine. They are
``PurePosixPath``/str throughout and are never resolved against the local filesystem: existence is
asked of the connection (``host_exec.is_dir`` / ``is_file``), because only it knows which host the
path belongs to. ``PurePosixPath`` has no ``.resolve()``, so a call site that forgets this raises
loudly instead of quietly answering about the wrong machine.

Role roots come first (``host_exec.role_root``) so a deployment can put the script tree on a
different host from the logs; ``RACK_REMOTE_BASE_PATH`` is the single-host fallback.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath

import host_exec
import script_tree
from config_env import _eget
from core import rpath as _rpath

_logger = logging.getLogger(__name__)

#: Projected by ``catalog.context.high_overlay`` from the active cluster's ``connection.log_dir``.
#: The per-cluster, multi-tenant-safe source of the log directory — it wins over the deprecated
#: global ``RACK_STATUS_LOG_DIR`` so each project reads logs from its own cluster.
CLUSTER_LOG_DIR_ENV = "NVIS_LOG_DIR"

#: The single-tenant relic. One global value for every cluster at once, which is wrong the moment a
#: deployment serves more than one — a project would read logs from a directory belonging to a
#: cluster it does not operate. Kept as a deprecated fallback so existing deployments keep working
#: while they migrate to the per-cluster field; a direct value warns once.
LEGACY_LOG_DIR_ENV = "RACK_STATUS_LOG_DIR"

_warned_legacy_log_dir = False


def _warn_legacy_log_dir() -> None:
    """Warn once that the global ``RACK_STATUS_LOG_DIR`` is deprecated in favour of the per-cluster
    field. Fires only on a *directly set* global value, never on the per-cluster projection, so a
    cluster that configures its log directory the right way is silent."""
    global _warned_legacy_log_dir
    if _warned_legacy_log_dir:
        return
    _warned_legacy_log_dir = True
    _logger.warning(
        "%s is DEPRECATED and will be removed in a future release. It sets one log directory for "
        "every cluster at once, so a multi-tenant deployment reads a project's logs from a cluster "
        "it may not operate. Set the log directory per cluster instead (the cluster's "
        "connection.log_dir, 'Log directory' in the cluster editor); this global value is now only "
        "a fallback.",
        LEGACY_LOG_DIR_ENV,
    )


APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent

# One env var places the whole tree on the head node; role roots can override per role.
#
# `SCRIPTS_DIR` is now only the *legacy single-directory* answer, kept because it is computed at
# import and so can never know which project is active. Nothing chooses a script through it any
# more -- `script_tree` does, and falls back to this shape when a deployment has no layered tree.
# It survives as the fallback's definition and as `DEFAULT_SCRIPT`'s base.
_REMOTE_BASE = os.environ.get("RACK_REMOTE_BASE_PATH", "").strip()
if _REMOTE_BASE:
    SCRIPTS_DIR = Path(_REMOTE_BASE) / "bin" / "scripts"
    DEFAULT_LOG_DIR = Path(_REMOTE_BASE) / "var" / "logs"
else:
    SCRIPTS_DIR = REPO_ROOT / "bin" / "scripts"
    DEFAULT_LOG_DIR = REPO_ROOT / "var" / "logs"

DEFAULT_SCRIPT = SCRIPTS_DIR / "rack_status.sh"

#: The built Sphinx site, served at /docs/ by `api/docs_site.py`. A *local* path, unlike almost
#: everything else in this module: it is an artifact inside this container, not a file on the
#: execution host, so it is a `Path` and `is_dir()` on it is meaningful.
DOCS_DIR = REPO_ROOT / "static" / "docs"


def _scripts_dir() -> str:
    """Directory holding the scripts on the host that actually runs them.

    Delegates to :mod:`script_tree`, which layers the active project's own copy over its
    platform's over the shared default set -- and answers with today's single flat directory on
    any deployment where that tree has not been created. Prefers the ``scripts`` role's declared
    root, so a deployment can keep the script tree on a different machine from the logs.
    """
    return script_tree.scripts_dir()


def _script(basename: str, env_key: str = "") -> Path:
    """Resolve one script: the env override if set, else the layered tree.

    The override still wins, and still wins absolutely -- a site that points
    ``RACK_SYSINFO_SCRIPT`` at a path of its own gets that path, layering or not. This is the
    single place the nineteen per-tool resolvers below now share; each used to inline the same
    ``_eget(key, SCRIPTS_DIR / name)`` against the *module-level* ``SCRIPTS_DIR``, which is
    computed once at import and therefore cannot know which project is active.
    """
    if env_key:
        override = (_eget(env_key, "") or "").strip()
        if override:
            return _rpath(Path(override))
    return _rpath(Path(script_tree.resolve(basename)))


def _logs_dir_default() -> str:
    """Default log root on the host that owns the ``logs`` role."""
    return host_exec.role_root("logs") or str(DEFAULT_LOG_DIR)


def _script_exists(path: Path) -> bool:
    """Check whether a script exists on the execution host.

    Previously this returned True unconditionally for a remote target, because the
    local check could only ever be wrong there. The connection can answer for both.
    """
    return host_exec.is_file(path)


def _default_log_dir() -> PurePosixPath:
    """The log directory when a request did not name one.

    Resolution order, most specific first:

    1. the **active cluster's** ``connection.log_dir`` (projected to ``NVIS_LOG_DIR`` by the
       overlay) — the per-cluster, multi-tenant-safe source;
    2. the deprecated global ``RACK_STATUS_LOG_DIR`` — kept as a fallback for deployments still
       migrating, and it warns once when it is the thing actually used;
    3. ``remote_base_path/var/logs`` (or the repo's ``var/logs`` off a cluster).
    """
    cluster_log_dir = (os.environ.get(CLUSTER_LOG_DIR_ENV) or "").strip()
    if cluster_log_dir:
        return _rpath(cluster_log_dir)
    legacy = (os.environ.get(LEGACY_LOG_DIR_ENV) or "").strip()
    if legacy:
        _warn_legacy_log_dir()
        return _rpath(legacy)
    return _rpath(str(DEFAULT_LOG_DIR))


def _allowed_roots() -> list[PurePosixPath]:
    raw = os.environ.get("RACK_STATUS_ALLOWED_PREFIXES", "")
    if not raw.strip():
        return []
    return [_rpath(x.strip()) for x in raw.split(",") if x.strip()]


def _resolve_log_dir(raw: str | None) -> PurePosixPath:
    # No macOS firmlink fixup any more: that existed to undo a local .resolve()
    # applied to a head-node path, and the connection never resolves a remote path
    # against this filesystem in the first place.
    log_dir = _rpath(raw or _default_log_dir())
    roots = _allowed_roots()
    if roots:
        ok = any(str(log_dir).startswith(str(r) + os.sep) or log_dir == r for r in roots)
        if not ok:
            raise ValueError(f"log_dir must be under one of: {[str(r) for r in roots]}")
    # Only *verify* when there is a host to verify against. With no execution host configured
    # there is nothing to stat, and answering "not a directory" would blame the path for a
    # deployment problem — on the polled endpoints that call this, that reads as a broken
    # dashboard rather than an unconfigured one. `/api/status` already reports the real cause
    # through `host_exec.reachability_state()`, so let it.
    if host_exec.execution_host_configured() and not host_exec.is_dir(log_dir):
        raise ValueError(f"not a directory: {log_dir}")
    return log_dir


# --- Per-tool script paths on the execution host ------------------------------------------------
# Each is "the env override, else whichever layer of the script tree has this file" -- the active
# project's own copy, then its platform's, then the shared default set (see `script_tree`). They live
# here rather than beside the routes that run them because several tools are invoked from more than
# one place, and because a site that relocates one script should find every resolver in one file.
#
# They were nineteen near-identical bodies inlining `SCRIPTS_DIR`, which is computed once at import
# and so could never answer "which project is active". Sharing `_script` is what made the tree
# possible without editing nineteen functions again the next time the layout changes.
#
# _rpath, not Path.resolve(): these name files on the head node, so resolving locally would answer with
# this container's filesystem.


def _sysinfo_script_path() -> Path:
    return _script("sysinfo.sh", "RACK_SYSINFO_SCRIPT")


def _nvswitches_rack_audit_script_path() -> Path:
    return _script("nvswitches_rack_audit.sh", "RACK_NVSWITCH_AUDIT_SCRIPT")


def _nvswitch_sdn_reset_script_path() -> Path:
    return _script("nvswitch_sdn_reset.sh", "RACK_NVSWITCH_SDN_RESET_SCRIPT")


def _nvswitch_system_reset_script_path() -> Path:
    return _script("nvswitch_system_reset.sh", "RACK_NVSWITCH_SYSTEM_RESET_SCRIPT")


def _imex_reset_script_path() -> Path:
    return _script("imex_reset.sh", "RACK_IMEX_RESET_SCRIPT")


def _imex_check_script_path() -> Path:
    return _script("imex_check.sh", "RACK_IMEX_CHECK_SCRIPT")


def _hw_check_pdsh_script_path() -> Path:
    return _script("hw_check_pdsh.sh", "RACK_HW_CHECK_SCRIPT")


def _nvlmapper_reports_base() -> Path:
    # An execution-host directory: expanduser/resolve would answer with this
    # process's home and filesystem, which is not where the reports are written.
    # Defaults to the bug-report dir so one setting moves both -- the Reports page
    # lists NVLMapper outputs and bug reports under the same tree.
    raw = (_eget("RACK_NVLMAPPER_REPORTS_BASE") or "").strip() or _bug_report_remote_dir()
    return _rpath(raw)


def _parser_path() -> Path:
    return _script("parser.sh", "RACK_PARSER_SCRIPT")


def _rack_status_script_path() -> Path:
    return _script("rack_status.sh", "RACK_STATUS_SCRIPT")

# Kept as the old spelling for a moment? No: renamed on the move. `_script_path` said nothing about
# WHICH script, which is fine in a 20k-line module where there was only one, and useless in a shared
# one next to nine siblings that all spell it `_<tool>_script_path`.


# --- Bug-report directory -----------------------------------------------------------------------
# Shared by the four bug-report routes and by the log-zip export, which is why it is here rather than
# beside either of them.
#
# Still expanduser'd (unlike _nvlmapper_reports_base, which must not be, because `~` would resolve to
# this container's home rather than the execution host's). That is safe here because the value is an
# absolute path at every real deployment; a site that sets RACK_BUG_REPORT_REMOTE_DIR=~/reports gets
# the container's home, which is the one `~` the reports are never under -- so keep it absolute.


_DEFAULT_BUG_REPORT_REMOTE_DIR = "/home/cmsupport/reports"


def _bug_report_remote_dir() -> str:
    """Base dir on each node (pdsh) and on this host (inventory).

    Resolved through the config ladder (``_eget``: cluster overlay > dashboard Settings >
    process env > platform overlay > default), so a deployment points it at the project's own
    prefix -- e.g. ``/home/cmods/bringup-data/reports`` -- rather than inheriting the historical
    ``/home/cmsupport/reports``, which exists on exactly one appliance. It used to read
    ``os.environ`` directly, which meant neither a cluster overlay nor the Settings screen could
    move it off that dead default.
    """
    d = (_eget("RACK_BUG_REPORT_REMOTE_DIR") or "").strip()
    if not d:
        d = _DEFAULT_BUG_REPORT_REMOTE_DIR
    out = os.path.expanduser(d).strip()
    return out or _DEFAULT_BUG_REPORT_REMOTE_DIR


def _bug_report_local_base_path() -> Path:
    return _rpath(Path(_bug_report_remote_dir()))


# The validation-tool scripts. Same shape as the resolvers above and grouped with them so a site that
# relocates one script finds every resolver in one file. The settings page lists all of these, which is
# why they were shared with app.py's settings route rather than private to the launcher.


def _validation_script_path() -> Path:
    return _script("run_singlerack.sh", "RACK_VALIDATION_SCRIPT")


def _mnubergemm_stress_script_path() -> Path:
    return _script("run_mnubergemm_stress.sh", "RACK_MNUBERGEMM_STRESS_SCRIPT")


def _nvloom_singlerack_script_path() -> Path:
    return _script("run_nvloom_singlerack.sh", "RACK_NVLOOM_SINGLERACK_SCRIPT")


def _clusterkit_script_path() -> Path:
    return _script("run_clusterkit.sh", "RACK_CLUSTERKIT_SCRIPT")


def _run_llmb_nccl_script_path() -> Path:
    return _script("run_llmb_nccl.sh", "RACK_LLMB_NCCL_SCRIPT")


def _run_nccl_mpi_script_path() -> Path:
    """Direct-NCCL launcher: resolves racks → nodes → submits nccl_allreduce.sbatch
    (host nccl-tests + mpirun over IB; no llmb-run/container/UCX-verbs guesswork)."""
    return _script("run_nccl_mpi.sh", "RACK_NCCL_MPI_SCRIPT")


def _nemo_xrack_script_path() -> Path:
    return _script("run-nemo-xrack.sh", "RACK_NEMO_XRACK_SCRIPT")


def _run_llama_script_path() -> Path:
    return _script("run-llama.sh", "RACK_LLAMA_SCRIPT")


def _run_exemplar_llama_script_path() -> Path:
    return _script("run-exemplar-llama.sh", "RACK_EXEMPLAR_LLAMA_SCRIPT")


def _nccl_fullrack_script_path() -> Path:
    return _script("nccl_fullrack_rounds.sh", "RACK_NCCL_FULLRACK_SCRIPT")
