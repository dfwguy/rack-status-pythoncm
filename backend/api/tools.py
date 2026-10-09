# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""The Tools page: workspace installers, container downloads, Grafana launch.

Seventeen endpoints in one module because they are one workflow with one shape: a POST that starts a
long host-side script, a matching ``-stream`` variant that streams its output, and a status probe. The
paired POST/stream endpoints exist because the SPA needs both a fire-and-forget launch and a live view
of the same run.

The exclusive-lock helpers are the reason this is not a thin wrapper. Two operators installing the
same workspace concurrently corrupts it, so each tool takes an advisory ``flock`` on a per-tool file;
the stream variant holds it for the life of the stream and releases it in a finally, because a
half-installed HPC-X is worse than a refused install.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import host_exec
import script_tree
from audit.actions import action
from config_env import (
    JOB_ENV_UI_DEFINITIONS,
    _eget,
    _env_int,
    _nvis_workspace_dir_effective,
    _subprocess_env,
)
from core import rpath as _rpath
from core import truthy_env as _truthy_env
from flask import Blueprint, Response, jsonify, request, stream_with_context
from paths import REPO_ROOT, _allowed_roots, _script_exists
from stream_exec import _STREAM_NO_CACHE_HEADERS, _merged_stdout_stream_generator

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("tools", __name__, url_prefix="")


class _ToolsExclusiveLockBusyError(Exception):
    """Non-blocking flock(2) LOCK_EX failed — another process/session holds this Utility lock."""


_TOOLS_NEMO_CONTAINER_SQSH = "nemo-25.11.01.sqsh"


_TOOL_EXCLUSIVE_LOCK_FILES: dict[str, str] = {
    "download_containers": "tools_download_containers.flock",
    "install_hpcx_workspace": "tools_install_hpcx_workspace.flock",
    "install_cuda_nccl_workspace": "tools_install_cuda_nccl_workspace.flock",
    "install_nvloom_workspace": "tools_install_nvloom_workspace.flock",
    "install_dgxc_benchmarking_workspace": "tools_install_dgxc_benchmarking_workspace.flock",
    "install_dcgm_workspace": "tools_install_dcgm_workspace.flock",
    "launch_grafana_bright_prometheus": "tools_launch_grafana_bright_prometheus.flock",
}


_TOOL_EXCLUSIVE_LOCK_BUSY_MSG: dict[str, str] = {
    "download_containers": (
        "Another session is already running the NEMO container download. Wait for it to finish."
    ),
    "install_hpcx_workspace": (
        "Another session is already running the Mellanox HPC-X workspace install. Wait for it to finish."
    ),
    "install_cuda_nccl_workspace": (
        "Another session is already running the CUDA toolkit / NCCL workspace install. Wait for it to finish."
    ),
    "install_nvloom_workspace": (
        "Another session is already running the nvloom workspace install. Wait for it to finish."
    ),
    "install_dgxc_benchmarking_workspace": (
        "Another session is already running the Exemplar (dgxc-benchmarking) setup. Wait for it to finish."
    ),
    "install_dcgm_workspace": (
        "Another session is already running the DCGM / DCGMI workspace install. Wait for it to finish."
    ),
    "launch_grafana_bright_prometheus": (
        "Another session is already running the Grafana + Prometheus launch script. Wait for it to finish."
    ),
}


def _tools_exclusive_lock_path(lock_name: str) -> Path:
    fname = _TOOL_EXCLUSIVE_LOCK_FILES.get(lock_name)
    if not fname:
        raise ValueError(f"unknown tools lock: {lock_name!r}")
    p = (REPO_ROOT / "var" / "cache" / fname).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def _tools_exclusive_lock(lock_name: str):
    """Hold an advisory exclusive flock for one Utility script (cross-process / multi-worker)."""
    path = _tools_exclusive_lock_path(lock_name)
    fh = open(path, "a+", encoding="utf-8")  # noqa: SIM115 - handle must outlive this scope: it holds the flock
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise _ToolsExclusiveLockBusyError(_TOOL_EXCLUSIVE_LOCK_BUSY_MSG.get(lock_name, "This tool is already running."))
    try:
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass


def _tools_try_acquire_exclusive_flock_for_stream(lock_name: str):
    """Return ``(fh, None)`` on success, or ``(None, busy_message)`` if another holder has LOCK_EX."""
    path = _tools_exclusive_lock_path(lock_name)
    fh = open(path, "a+", encoding="utf-8")  # noqa: SIM115 - handle must outlive this scope: it holds the flock
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            fh.close()
        except OSError:
            pass
        return None, _TOOL_EXCLUSIVE_LOCK_BUSY_MSG.get(lock_name, "This tool is already running.")
    return fh, None


def _tools_release_exclusive_flock_fh(fh) -> None:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass


def _tools_check_destination_allowed(p: Path) -> None:
    """Raise ``ValueError`` if *p* is outside allowed prefixes when enforcement is on."""
    if _truthy_env("RACK_TOOLS_ENFORCE_ALLOWED_PREFIXES"):
        roots = _allowed_roots()
        if roots:
            ok = any(str(p).startswith(str(r) + os.sep) or p == r for r in roots)
            if not ok:
                raise ValueError(
                    "destination must be under one of RACK_STATUS_ALLOWED_PREFIXES "
                    f"(enforce: RACK_TOOLS_ENFORCE_ALLOWED_PREFIXES): {[str(r) for r in roots]}"
                )


#: What an operator sees when a workspace tool runs on a cluster that has no workspace configured.
_NO_WORKSPACE = (
    "no workspace is configured for this cluster: set it under Projects -> cluster -> Workspace "
    "(or pass one for this run)"
)


def _resolve_workspace(body: dict) -> Path:
    """The workspace a tool installs into: the request's, else the cluster's (``workspace_dir``).

    There is no fallback path. The old one, /home/cmsupport/workspace, existed on no current
    cluster, so an install landed where the launchers never looked.
    """
    raw = (body.get("workspace") or "").strip() or _nvis_workspace_dir_effective()
    if not raw:
        raise ValueError(_NO_WORKSPACE)
    return _resolve_tools_destination(raw)


def _resolve_tools_destination(raw: str) -> Path:
    """Validate an absolute destination **directory** for Tools (e.g. enroot import output).

    May be an existing directory or a non-existent path that ``mkdir -p`` will create. Must not
    be a file path or a path whose last component looks like a ``.sqsh`` file.
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("destination is required")
    if not PurePosixPath(s).is_absolute():
        raise ValueError("destination must be an absolute path")
    # The destination lives on the execution host, so existence is asked of the
    # connection rather than stat-ed locally.
    p = _rpath(s)
    # host_exec.is_file() is really "exists": transport/base.py implements it as
    # `stat(path) is not None`, so it returns True for a DIRECTORY too (is_dir
    # correctly uses `test -d`). Testing it alone rejected every existing
    # workspace with "destination must be a directory, not a file" and made the
    # Exemplar/llmb install unreachable. Exclude directories explicitly here
    # rather than change is_file(), which has ~46 call sites.
    if host_exec.is_file(p) and not host_exec.is_dir(p):
        raise ValueError("destination must be a directory, not a file")
    if not host_exec.is_dir(p) and p.suffix.lower() == ".sqsh":
        raise ValueError("destination must be a directory path, not a .sqsh file path")
    _tools_check_destination_allowed(p)
    return p


def _tools_destination_dir_for_status_probe(raw: str) -> Path:
    """Directory to check for an existing NEMO ``.sqsh`` (UI may paste a file path; POST remains directory-only)."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("destination is required")
    if not PurePosixPath(s).is_absolute():
        raise ValueError("destination must be an absolute path")
    # The container is downloaded to the cluster, so the destination is an
    # execution-host directory: it is normalised textually and inspected over the
    # connection. This is the path that expanduser/.resolve() used to rewrite
    # through the local filesystem (on macOS turning /home into
    # /System/Volumes/Data/home) -- see docs/EXECUTION_ARCHITECTURE.md 4.
    p = _rpath(s)
    is_dir = host_exec.is_dir(p)
    is_file = host_exec.is_file(p)
    # ``host_exec.is_file`` is the historical existence predicate, so a symlink
    # to a directory makes both it and ``is_dir`` true. Treat that as the
    # directory it resolves to instead of rejecting a perfectly valid shared-NFS
    # container path.
    if is_file and not is_dir and p.suffix.lower() != ".sqsh":
        raise ValueError("destination must be a directory or a NEMO .sqsh file path")
    if (is_file and not is_dir and p.suffix.lower() == ".sqsh") or (
        not is_dir and p.suffix.lower() == ".sqsh"
    ):
        dest_dir = p.parent
    else:
        dest_dir = p
    _tools_check_destination_allowed(dest_dir)
    return dest_dir


def _tools_sqsh_nonempty(path: Path) -> bool:
    """Return ``test -s`` from the execution host, following shared-NFS symlinks."""
    try:
        return host_exec.run_on_host(["test", "-s", str(path)], timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _tools_find_existing_sqsh_for_skip(dest_dir: Path) -> tuple[Path | None, list[str]]:
    """Return (path, checked_str_paths) if a non-empty .sqsh is already present under *dest_dir*."""
    checked: list[str] = []

    def try_path(p: Path) -> Path | None:
        checked.append(str(p))
        return p if _tools_sqsh_nonempty(p) else None

    hit = try_path(dest_dir / _TOOLS_NEMO_CONTAINER_SQSH)
    if hit:
        return hit, checked

    raw = (_eget("NEMO_SQSH_SRC") or "").strip()
    if raw:
        bn = Path(raw).expanduser().name
        if bn.lower().endswith(".sqsh"):
            hit = try_path(dest_dir / bn)
            if hit:
                return hit, checked
        try:
            nemo = _rpath(Path(raw))
        except (OSError, ValueError, RuntimeError):
            nemo = None
        if nemo is not None and nemo.suffix.lower() == ".sqsh":
            try:
                if _rpath(nemo.parent) == _rpath(dest_dir):
                    hit = try_path(nemo)
                    if hit:
                        return hit, checked
            except OSError:
                pass

    return None, checked


@bp.route("/api/tools/download-containers", methods=["POST"])
@action("tools:download_containers")
def api_tools_download_containers():
    """Run bin/scripts/05-download-containers.sh with a chosen destination (and optional arch).

    Requires ``NGC_API_KEY`` (nvapi-…) saved in Settings → External connections or the
    **server** environment — never in the HTTP body.
    If the expected ``.sqsh`` already exists and is non-empty, returns success with ``skipped`` (no import, no key needed).
    """
    if os.environ.get("RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    dest_raw = body.get("destination")
    arch_raw = body.get("arch")
    try:
        dest_dir = _resolve_tools_destination(str(dest_raw).strip() if dest_raw else "")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    found, checked_paths = _tools_find_existing_sqsh_for_skip(dest_dir)
    if found is not None:
        msg = f"File already exists, skipping download: {found}"
        return jsonify(
            {
                "ok": True,
                "skipped": True,
                "destination": str(dest_dir),
                "existing_file": str(found),
                "checked_paths": checked_paths,
                "message": msg,
                "stdout": msg,
                "stderr": "",
                "command_display": "(no command — output file already present)",
            }
        )
    if not (_eget("NGC_API_KEY") or "").strip():
        return jsonify(
            {
                "ok": False,
                "error": "NGC_API_KEY is not set — add it in Settings → External connections (or export before starting the app)",
            }
        ), 400
    arch = str(arch_raw).strip() if arch_raw else ""
    if arch and arch not in ("aarch64", "x86_64", "arm64"):
        return jsonify({"ok": False, "error": "arch must be aarch64, x86_64, or arm64"}), 400
    if arch == "arm64":
        arch = "aarch64"
    script = script_tree.resolve_path("05-download-containers.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(60, _env_int("RACK_TOOLS_DOWNLOAD_CONTAINERS_TIMEOUT", 14400))
    cmd: list[str] = [str(script), str(dest_dir)]
    if arch:
        cmd.append(arch)
    display_cmd = f"{script.name} {shlex.quote(str(dest_dir))}" + (f" {shlex.quote(arch)}" if arch else "")
    try:
        with _tools_exclusive_lock("download_containers"):
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except _ToolsExclusiveLockBusyError as e:
        return jsonify({"ok": False, "error": str(e), "destination": str(dest_dir)}), 409
    except subprocess.TimeoutExpired:
        return jsonify(
            {"ok": False, "error": f"download script timed out after {timeout}s", "destination": str(dest_dir)}
        ), 504
    except Exception as e:
        _logger.debug("api_tools_download_containers: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e), "destination": str(dest_dir)}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "destination": str(dest_dir),
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    ), (200 if ok else 502)


@bp.route("/api/tools/download-containers-stream", methods=["POST"])
@action("tools:download_containers", variant="stream")
def api_tools_download_containers_stream():
    """Stream ``05-download-containers.sh`` merged stdout/stderr (same POST JSON as ``/api/tools/download-containers``)."""
    if os.environ.get("RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    dest_raw = body.get("destination")
    arch_raw = body.get("arch")
    try:
        dest_dir = _resolve_tools_destination(str(dest_raw).strip() if dest_raw else "")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    found, _checked = _tools_find_existing_sqsh_for_skip(dest_dir)
    if found is not None:
        msg = f"File already exists, skipping download: {found}"

        def _skip_gen() -> Iterator[bytes]:
            yield f"{msg}\n\n--- exit 0 ---\n".encode()

        return Response(
            stream_with_context(_skip_gen()),
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )
    if not (_eget("NGC_API_KEY") or "").strip():

        def _ngc_gen() -> Iterator[bytes]:
            yield (
                "ERROR: NGC_API_KEY is not set — add it in Settings \u2192 External connections "
                "(or export before starting the app)\n"
            ).encode("utf-8")

        return Response(
            stream_with_context(_ngc_gen()),
            status=400,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )
    arch = str(arch_raw).strip() if arch_raw else ""
    if arch and arch not in ("aarch64", "x86_64", "arm64"):
        return jsonify({"ok": False, "error": "arch must be aarch64, x86_64, or arm64"}), 400
    if arch == "arm64":
        arch = "aarch64"
    script = script_tree.resolve_path("05-download-containers.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(60, _env_int("RACK_TOOLS_DOWNLOAD_CONTAINERS_TIMEOUT", 14400))
    cmd: list[str] = [str(script), str(dest_dir)]
    if arch:
        cmd.append(arch)
    display_cmd = f"{script.name} {shlex.quote(str(dest_dir))}" + (f" {shlex.quote(arch)}" if arch else "")
    lock_fh, lock_busy = _tools_try_acquire_exclusive_flock_for_stream("download_containers")
    if lock_busy is not None:

        def _lock_busy_gen() -> Iterator[bytes]:
            yield f"ERROR: {lock_busy}\n".encode()

        return Response(
            stream_with_context(_lock_busy_gen()),
            status=409,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )

    def _download_stream_gen() -> Iterator[bytes]:
        try:
            yield from _merged_stdout_stream_generator(
                cmd,
                str(REPO_ROOT),
                display_cmd,
                timeout,
                linebuffer_env_var="RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER",
            )
        finally:
            _tools_release_exclusive_flock_fh(lock_fh)

    return Response(
        stream_with_context(_download_stream_gen()),
        mimetype="text/plain; charset=utf-8",
        headers=dict(_STREAM_NO_CACHE_HEADERS),
    )


@bp.route("/api/tools/download-containers-status", methods=["GET"])
@action(get="job:list")
def api_tools_download_containers_status():
    """Return whether a non-empty NEMO ``.sqsh`` is already present for *destination* (same rules as download skip)."""
    if os.environ.get("RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED)"}), 403
    dest_q = (request.args.get("destination") or "").strip()
    if not dest_q:
        return jsonify({"ok": True, "exists": False})
    try:
        dest_dir = _tools_destination_dir_for_status_probe(dest_q)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    found, _checked = _tools_find_existing_sqsh_for_skip(dest_dir)
    if found is not None:
        return jsonify({"ok": True, "exists": True, "existing_file": str(found), "destination_dir": str(dest_dir)})
    return jsonify({"ok": True, "exists": False, "destination_dir": str(dest_dir)})


def _workspace_toolchain_status_payload(ws: Path, cuda_major: str) -> dict[str, object]:
    """Probe CUDA, HPC-X, NCCL, and NVLoom files on the host that owns *ws*.

    ``_resolve_tools_destination`` returns a textual ``PurePosixPath`` in SSH mode.
    Never call ``Path.is_file`` on it: that either raises (``PurePosixPath``) or
    inspects the container instead of the execution host. One bounded
    ``host_exec.stat_dir`` call keeps this endpoint transport-correct and avoids a
    separate SSH round trip for every candidate file.
    """
    maj = cuda_major.strip() if cuda_major else "13"
    if maj == "12":
        hpcx_unpack = "hpcx-v2.22.1-gcc-doca_ofed-ubuntu24.04-cuda12-aarch64"
    else:
        hpcx_unpack = "hpcx-v2.25.1-gcc-doca_ofed-ubuntu24.04-cuda13-aarch64"

    candidates = {
        "cuda_nvcc": (ws / "cuda" / "bin" / "nvcc", ws / f"cuda{maj}" / "bin" / "nvcc"),
        "hpcx_init": (
            ws / hpcx_unpack / "hpcx-init.sh",
            ws / hpcx_unpack / "hpcx-mt-init-ompi.sh",
        ),
        "nccl_all_reduce_perf": (ws / "nccl" / "bins" / "all_reduce_perf",),
        "nvloom_cli": (ws / "nvloom" / "nvloom_cli",),
        "cmake_nvloom_bundle": (ws / "cmake-3.28.1-linux-aarch64" / "bin" / "cmake",),
    }
    basenames = tuple(sorted({path.name for paths in candidates.values() for path in paths}))
    found = {
        os.path.normpath(path)
        for path in host_exec.stat_dir(ws, patterns=basenames, maxdepth=5)
    }

    def present(key: str) -> bool:
        return any(os.path.normpath(str(path)) in found for path in candidates[key])

    cuda_nvcc = present("cuda_nvcc")
    hpcx_init = present("hpcx_init")
    nccl_perf = present("nccl_all_reduce_perf")
    all_ready = bool(cuda_nvcc and hpcx_init and nccl_perf)
    nvloom_cli = present("nvloom_cli")
    cmake_nvloom_bundle = present("cmake_nvloom_bundle")
    return {
        "cuda_nvcc": cuda_nvcc,
        "hpcx_init": hpcx_init,
        "nccl_all_reduce_perf": nccl_perf,
        "all_ready": all_ready,
        "hpcx_unpack": hpcx_unpack,
        "workspace_dir": str(ws),
        "nvloom_cli": nvloom_cli,
        "cmake_nvloom_bundle": cmake_nvloom_bundle,
    }


@bp.route("/api/tools/workspace-toolchain-status", methods=["GET"])
@action(get="job:list")
def api_tools_workspace_toolchain_status():
    """Return whether CUDA toolkit, HPC-X, NCCL test binary, and nvloom_cli are present under *workspace*."""
    ws_q = (request.args.get("workspace") or "").strip()
    cuda_m = str(request.args.get("cuda_major") or "13").strip()
    if cuda_m not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    if not ws_q:
        return jsonify(
            {
                "ok": True,
                "cuda_nvcc": False,
                "hpcx_init": False,
                "nccl_all_reduce_perf": False,
                "all_ready": False,
                "workspace_dir": "",
                "nvloom_cli": False,
                "cmake_nvloom_bundle": False,
            }
        )
    try:
        ws = _resolve_tools_destination(ws_q)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    payload = _workspace_toolchain_status_payload(ws, cuda_m)
    return jsonify({"ok": True, **payload})


def _write_prometheus_rc_for_launch(user: str, password: str) -> Path:
    """Create a mode-0600 file with ``USER`` / ``PASSWORD`` assignments like ``~/.prometheusrc`` for ``source``."""
    fd, path_s = tempfile.mkstemp(prefix="nvis-prometheusrc-", suffix=".env", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"USER={shlex.quote(user)}\n")
            f.write(f"PASSWORD={shlex.quote(password)}\n")
        p = Path(path_s)
        p.chmod(0o600)
        return p
    except Exception:
        try:
            os.unlink(path_s)
        except OSError:
            pass
        raise


@bp.route("/api/tools/launch-grafana-bright-prometheus", methods=["POST"])
@action("tools:launch_grafana")
def api_tools_launch_grafana_bright_prometheus():
    """Run ``bin/scripts/launch-grafana-bright-prometheus.sh`` on the dashboard host (Docker Grafana + Prometheus DS).

    When **both** ``RACK_PROMETHEUS_BASIC_AUTH_USER`` and ``RACK_PROMETHEUS_BASIC_AUTH_PASSWORD`` are set
    (Settings → External connections), the server writes a temporary ``PROMETHEUS_RC`` file in the same shape
    as ``~/.prometheusrc`` and passes it to the script. If either is empty, ``PROMETHEUS_RC`` is unset so the
    script resolves ``/cm/cmsupport/.prometheusrc`` or ``~/.prometheusrc`` as documented in the script.
    """
    if os.environ.get("RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_DISABLED)"}), 403
    script = script_tree.resolve_path("launch-grafana-bright-prometheus.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    user = (_eget("RACK_PROMETHEUS_BASIC_AUTH_USER") or "").strip()
    password = (_eget("RACK_PROMETHEUS_BASIC_AUTH_PASSWORD") or "").strip()
    tmp_rc: Path | None = None
    extra: dict[str, str] = {}
    if user or password:
        if not user or not password:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "Set both Prometheus username and password in Settings → External connections, "
                        "or clear both to use ~/.prometheusrc (or other default paths) on the server."
                    ),
                }
            ), 400
        tmp_rc = _write_prometheus_rc_for_launch(user, password)
        extra["PROMETHEUS_RC"] = str(tmp_rc)
    timeout = max(120, _env_int("RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_TIMEOUT", 7200))
    cmd = ["bash", str(script)]
    display_cmd = "bash bin/scripts/launch-grafana-bright-prometheus.sh"
    if extra.get("PROMETHEUS_RC"):
        display_cmd += "  (PROMETHEUS_RC from Settings credentials)"
    proc: subprocess.CompletedProcess[str] | None = None
    err_resp: tuple[object, int] | None = None
    try:
        with _tools_exclusive_lock("launch_grafana_bright_prometheus"):
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(extra),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except _ToolsExclusiveLockBusyError as e:
        err_resp = (jsonify({"ok": False, "error": str(e)}), 409)
    except subprocess.TimeoutExpired:
        err_resp = (jsonify({"ok": False, "error": f"script timed out after {timeout}s"}), 504)
    except Exception as e:
        _logger.debug("api_tools_launch_grafana_bright_prometheus: ignoring error", exc_info=True)
        err_resp = (jsonify({"ok": False, "error": str(e)}), 500)
    finally:
        if tmp_rc is not None:
            try:
                tmp_rc.unlink(missing_ok=True)
            except OSError:
                pass
    if err_resp is not None:
        return err_resp
    assert proc is not None
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    ), (200 if ok else 502)


@bp.route("/api/tools/launch-grafana-bright-prometheus-stream", methods=["POST"])
@action("tools:launch_grafana", variant="stream")
def api_tools_launch_grafana_bright_prometheus_stream():
    """Stream ``launch-grafana-bright-prometheus.sh`` (same credentials rules as the JSON POST)."""
    if os.environ.get("RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_DISABLED)"}), 403
    script = script_tree.resolve_path("launch-grafana-bright-prometheus.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    user = (_eget("RACK_PROMETHEUS_BASIC_AUTH_USER") or "").strip()
    password = (_eget("RACK_PROMETHEUS_BASIC_AUTH_PASSWORD") or "").strip()
    tmp_rc: Path | None = None
    extra: dict[str, str] = {}
    if user or password:
        if not user or not password:

            def _cred_err() -> Iterator[bytes]:
                yield (
                    "ERROR: Set both Prometheus username and password in Settings → External connections, "
                    "or clear both to use ~/.prometheusrc on the server.\n"
                ).encode()

            return Response(
                stream_with_context(_cred_err()),
                status=400,
                mimetype="text/plain; charset=utf-8",
                headers=dict(_STREAM_NO_CACHE_HEADERS),
            )
        tmp_rc = _write_prometheus_rc_for_launch(user, password)
        extra["PROMETHEUS_RC"] = str(tmp_rc)
    timeout = max(120, _env_int("RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_TIMEOUT", 7200))
    cmd = ["bash", str(script)]
    display_cmd = "bash bin/scripts/launch-grafana-bright-prometheus.sh"
    if extra.get("PROMETHEUS_RC"):
        display_cmd += "  (PROMETHEUS_RC from Settings credentials)"

    lock_fh, lock_busy = _tools_try_acquire_exclusive_flock_for_stream("launch_grafana_bright_prometheus")
    if lock_busy is not None:
        if tmp_rc is not None:
            try:
                tmp_rc.unlink(missing_ok=True)
            except OSError:
                pass

        def _lock_busy_gen() -> Iterator[bytes]:
            yield f"ERROR: {lock_busy}\n".encode()

        return Response(
            stream_with_context(_lock_busy_gen()),
            status=409,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )

    def _grafana_stream_gen() -> Iterator[bytes]:
        try:
            yield from _merged_stdout_stream_generator(
                cmd,
                str(REPO_ROOT),
                display_cmd,
                timeout,
                linebuffer_env_var="RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER",
                env_extra=extra if extra else None,
            )
        finally:
            if tmp_rc is not None:
                try:
                    tmp_rc.unlink(missing_ok=True)
                except OSError:
                    pass
            _tools_release_exclusive_flock_fh(lock_fh)

    return Response(
        stream_with_context(_grafana_stream_gen()),
        mimetype="text/plain; charset=utf-8",
        headers=dict(_STREAM_NO_CACHE_HEADERS),
    )


def _json_body_bool(body: dict, key: str, *, default: bool) -> bool:
    """Parse a boolean from a JSON object (dashboard Tools POST bodies)."""
    if key not in body:
        return default
    v = body[key]
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("1", "true", "yes", "on"):
            return True
        if t in ("0", "false", "no", "off", ""):
            return False
    return default


@bp.route("/api/tools/install-hpcx-workspace", methods=["POST"])
@action("tools:install_hpcx")
def api_tools_install_hpcx_workspace():
    """Run ``install-hpcx-workspace.sh``: Mellanox HPC-X tarball only (CUDA/NCCL: ``install-cuda-nccl-workspace.sh``)."""
    if os.environ.get("RACK_TOOLS_INSTALL_HPCX_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_HPCX_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    arch = str(body.get("arch") or "aarch64").strip()
    if arch != "aarch64":
        return jsonify(
            {
                "ok": False,
                "error": 'arch must be "aarch64" (matches aihpc_val installation/03-build-nccl.sh).',
            }
        ), 400
    try:
        ws = _resolve_workspace(body)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = script_tree.resolve_path("install-hpcx-workspace.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_HPCX_WORKSPACE_TIMEOUT", 28800))
    cmd = ["bash", str(script), str(ws), cuda_major, arch]
    display_cmd = (
        "bash bin/scripts/install-hpcx-workspace.sh "
        f"{shlex.quote(str(ws))} {shlex.quote(cuda_major)} {shlex.quote(arch)}"
    )
    try:
        with _tools_exclusive_lock("install_hpcx_workspace"):
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except _ToolsExclusiveLockBusyError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": f"install-hpcx-workspace timed out after {timeout}s"}), 504
    except Exception as e:
        _logger.debug("api_tools_install_hpcx_workspace: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    ), (200 if ok else 502)


@bp.route("/api/tools/install-hpcx-workspace-stream", methods=["POST"])
@action("tools:install_hpcx", variant="stream")
def api_tools_install_hpcx_workspace_stream():
    """Stream ``install-hpcx-workspace.sh`` (POST JSON: ``workspace``, ``cuda_major``, ``arch``)."""
    if os.environ.get("RACK_TOOLS_INSTALL_HPCX_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_HPCX_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    arch = str(body.get("arch") or "aarch64").strip()
    if arch != "aarch64":
        return jsonify(
            {
                "ok": False,
                "error": 'arch must be "aarch64" (matches aihpc_val installation/03-build-nccl.sh).',
            }
        ), 400
    try:
        ws = _resolve_workspace(body)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = script_tree.resolve_path("install-hpcx-workspace.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_HPCX_WORKSPACE_TIMEOUT", 28800))
    cmd = ["bash", str(script), str(ws), cuda_major, arch]
    display_cmd = (
        "bash bin/scripts/install-hpcx-workspace.sh "
        f"{shlex.quote(str(ws))} {shlex.quote(cuda_major)} {shlex.quote(arch)}"
    )
    lock_fh, lock_busy = _tools_try_acquire_exclusive_flock_for_stream("install_hpcx_workspace")
    if lock_busy is not None:

        def _lock_busy_gen() -> Iterator[bytes]:
            yield f"ERROR: {lock_busy}\n".encode()

        return Response(
            stream_with_context(_lock_busy_gen()),
            status=409,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )

    def _hpcx_stream_gen() -> Iterator[bytes]:
        try:
            yield from _merged_stdout_stream_generator(
                cmd,
                str(REPO_ROOT),
                display_cmd,
                timeout,
                linebuffer_env_var="RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER",
            )
        finally:
            _tools_release_exclusive_flock_fh(lock_fh)

    return Response(
        stream_with_context(_hpcx_stream_gen()),
        mimetype="text/plain; charset=utf-8",
        headers=dict(_STREAM_NO_CACHE_HEADERS),
    )


_DCGM_VALID_ARCH = ("aarch64", "x86_64")


_DCGM_VALID_DISTRO = (
    "dgx",
    "ubuntu2404",
    "ubuntu2204",
    "ubuntu2004",
    "debian12",
    "rhel9",
    "rhel8",
    "sles15",
)


_DCGM_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.:_+~-]{0,63}$")


def _parse_dcgm_body(body: dict) -> tuple[dict | None, str | None]:
    """Validate the DCGM workspace-install JSON. Returns ``(parsed, None)`` or ``(None, error)``."""
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return None, 'cuda_major must be "12" or "13"'
    arch = str(body.get("arch") or "aarch64").strip()
    if arch not in _DCGM_VALID_ARCH:
        return None, 'arch must be "aarch64" (GBx00 / ARM) or "x86_64" (Bx00)'
    distro = str(body.get("distro") or "dgx").strip()
    if distro not in _DCGM_VALID_DISTRO:
        return None, f"distro must be one of: {', '.join(_DCGM_VALID_DISTRO)}"
    version = str(body.get("version") or "").strip()
    if version and not _DCGM_VERSION_RE.match(version):
        return None, "version pin has invalid characters"
    try:
        ws = _resolve_workspace(body)
    except ValueError as e:
        return None, str(e)
    return {"cuda_major": cuda_major, "arch": arch, "distro": distro, "version": version, "ws": ws}, None


def _dcgm_extra_env(parsed: dict) -> dict[str, str]:
    env = {"NVIS_DCGM_DISTRO": parsed["distro"]}
    if parsed["version"]:
        env["NVIS_DCGM_VERSION"] = parsed["version"]
    return env


def _dcgm_display_cmd(parsed: dict) -> str:
    extra = _dcgm_extra_env(parsed)
    prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in extra.items())
    if prefix:
        prefix += " "
    return (
        f"{prefix}bash bin/scripts/install-dcgm-workspace.sh "
        f"{shlex.quote(str(parsed['ws']))} {shlex.quote(parsed['cuda_major'])} {shlex.quote(parsed['arch'])}"
    )


@bp.route("/api/tools/install-dcgm-workspace", methods=["POST"])
@action("tools:install_dcgm")
def api_tools_install_dcgm_workspace():
    """Run ``install-dcgm-workspace.sh``: download + extract DCGM 4.x into the shared workspace (head node)."""
    if os.environ.get("RACK_TOOLS_INSTALL_DCGM_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_DCGM_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    parsed, err = _parse_dcgm_body(body)
    if err is not None:
        return jsonify({"ok": False, "error": err}), 400
    assert parsed is not None
    script = script_tree.resolve_path("install-dcgm-workspace.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_DCGM_WORKSPACE_TIMEOUT", 7200))
    cmd = ["bash", str(script), str(parsed["ws"]), parsed["cuda_major"], parsed["arch"]]
    try:
        with _tools_exclusive_lock("install_dcgm_workspace"):
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(_dcgm_extra_env(parsed)),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except _ToolsExclusiveLockBusyError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": f"install-dcgm-workspace timed out after {timeout}s"}), 504
    except Exception as e:
        _logger.debug("api_tools_install_dcgm_workspace: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "command_display": _dcgm_display_cmd(parsed),
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "arch": parsed["arch"],
            "distro": parsed["distro"],
            "cuda_major": parsed["cuda_major"],
        }
    ), (200 if ok else 502)


@bp.route("/api/tools/install-dcgm-workspace-stream", methods=["POST"])
@action("tools:install_dcgm", variant="stream")
def api_tools_install_dcgm_workspace_stream():
    """Stream ``install-dcgm-workspace.sh`` (POST JSON: ``workspace``, ``cuda_major``, ``arch``, ``distro``, ``version``)."""
    if os.environ.get("RACK_TOOLS_INSTALL_DCGM_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_DCGM_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    parsed, err = _parse_dcgm_body(body)
    if err is not None:
        return jsonify({"ok": False, "error": err}), 400
    assert parsed is not None
    script = script_tree.resolve_path("install-dcgm-workspace.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_DCGM_WORKSPACE_TIMEOUT", 7200))
    cmd = ["bash", str(script), str(parsed["ws"]), parsed["cuda_major"], parsed["arch"]]
    extra_env = _dcgm_extra_env(parsed)
    display_cmd = _dcgm_display_cmd(parsed)
    lock_fh, lock_busy = _tools_try_acquire_exclusive_flock_for_stream("install_dcgm_workspace")
    if lock_busy is not None:

        def _lock_busy_gen() -> Iterator[bytes]:
            yield f"ERROR: {lock_busy}\n".encode()

        return Response(
            stream_with_context(_lock_busy_gen()),
            status=409,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )

    def _dcgm_stream_gen() -> Iterator[bytes]:
        try:
            yield from _merged_stdout_stream_generator(
                cmd,
                str(REPO_ROOT),
                display_cmd,
                timeout,
                linebuffer_env_var="RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER",
                env_extra=extra_env,
            )
        finally:
            _tools_release_exclusive_flock_fh(lock_fh)

    return Response(
        stream_with_context(_dcgm_stream_gen()),
        mimetype="text/plain; charset=utf-8",
        headers=dict(_STREAM_NO_CACHE_HEADERS),
    )


_CUDA_NCCL_BUILD_HOST_RE = re.compile(r"^[a-zA-Z0-9](?:[-a-zA-Z0-9.]{0,251})$")


def _validate_cuda_nccl_build_host(raw: object) -> tuple[str | None, str | None]:
    """Return ``(hostname, None)`` or ``(None, error_message)`` for ``build_host`` JSON field."""
    t = str(raw or "").strip()
    if not t:
        return None, "build_host is required (one Slurm compute node; workspace path must exist there on shared home)."
    if len(t) > 253:
        return None, "build_host is too long."
    if not _CUDA_NCCL_BUILD_HOST_RE.match(t):
        return None, "build_host must be a single hostname (letters, digits, dots, hyphens)."
    return t, None


@bp.route("/api/tools/install-cuda-nccl-workspace", methods=["POST"])
@action("tools:install_cuda_nccl")
def api_tools_install_cuda_nccl_workspace():
    """Run ``install-cuda-nccl-workspace.sh``: download on localhost, install on ``build_host`` via pdsh."""
    if os.environ.get("RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    arch = str(body.get("arch") or "aarch64").strip()
    if arch != "aarch64":
        return jsonify(
            {
                "ok": False,
                "error": 'arch must be "aarch64" (matches aihpc_val installation/03-build-nccl.sh).',
            }
        ), 400
    install_cuda = _json_body_bool(body, "install_cuda_toolkit", default=False)
    build_nccl = _json_body_bool(body, "build_nccl", default=False)
    if not install_cuda and not build_nccl:
        return jsonify(
            {"ok": False, "error": "Enable install_cuda_toolkit and/or build_nccl (at least one step)."},
        ), 400
    build_host, bh_err = _validate_cuda_nccl_build_host(body.get("build_host"))
    if bh_err is not None:
        return jsonify({"ok": False, "error": bh_err}), 400
    assert build_host is not None
    try:
        ws = _resolve_workspace(body)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = script_tree.resolve_path("install-cuda-nccl-workspace.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_TIMEOUT", 28800))
    extra_env = {
        "NVIS_INSTALL_CUDA_TOOLKIT": "1" if install_cuda else "0",
        "NVIS_BUILD_NCCL": "1" if build_nccl else "0",
        "NVIS_CUDA_NCCL_BUILD_HOST": build_host,
    }
    cmd = ["bash", str(script), str(ws), cuda_major, arch]
    display_cmd = (
        f"NVIS_INSTALL_CUDA_TOOLKIT={extra_env['NVIS_INSTALL_CUDA_TOOLKIT']} "
        f"NVIS_BUILD_NCCL={extra_env['NVIS_BUILD_NCCL']} "
        f"NVIS_CUDA_NCCL_BUILD_HOST={shlex.quote(build_host)} "
        "bash bin/scripts/install-cuda-nccl-workspace.sh "
        f"{shlex.quote(str(ws))} {shlex.quote(cuda_major)} {shlex.quote(arch)}"
    )
    try:
        with _tools_exclusive_lock("install_cuda_nccl_workspace"):
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(extra_env),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except _ToolsExclusiveLockBusyError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": f"install-cuda-nccl-workspace timed out after {timeout}s"}), 504
    except Exception as e:
        _logger.debug("api_tools_install_cuda_nccl_workspace: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "install_cuda_toolkit": install_cuda,
            "build_nccl": build_nccl,
            "build_host": build_host,
        }
    ), (200 if ok else 502)


@bp.route("/api/tools/install-cuda-nccl-workspace-stream", methods=["POST"])
@action("tools:install_cuda_nccl", variant="stream")
def api_tools_install_cuda_nccl_workspace_stream():
    """Stream ``install-cuda-nccl-workspace.sh`` (same POST JSON as ``/api/tools/install-cuda-nccl-workspace``)."""
    if os.environ.get("RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    arch = str(body.get("arch") or "aarch64").strip()
    if arch != "aarch64":
        return jsonify(
            {
                "ok": False,
                "error": 'arch must be "aarch64" (matches aihpc_val installation/03-build-nccl.sh).',
            }
        ), 400
    install_cuda = _json_body_bool(body, "install_cuda_toolkit", default=False)
    build_nccl = _json_body_bool(body, "build_nccl", default=False)
    if not install_cuda and not build_nccl:
        return jsonify(
            {"ok": False, "error": "Enable install_cuda_toolkit and/or build_nccl (at least one step)."},
        ), 400
    build_host, bh_err = _validate_cuda_nccl_build_host(body.get("build_host"))
    if bh_err is not None:
        return jsonify({"ok": False, "error": bh_err}), 400
    assert build_host is not None
    try:
        ws = _resolve_workspace(body)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = script_tree.resolve_path("install-cuda-nccl-workspace.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_TIMEOUT", 28800))
    extra_env = {
        "NVIS_INSTALL_CUDA_TOOLKIT": "1" if install_cuda else "0",
        "NVIS_BUILD_NCCL": "1" if build_nccl else "0",
        "NVIS_CUDA_NCCL_BUILD_HOST": build_host,
    }
    cmd = ["bash", str(script), str(ws), cuda_major, arch]
    display_cmd = (
        f"NVIS_INSTALL_CUDA_TOOLKIT={extra_env['NVIS_INSTALL_CUDA_TOOLKIT']} "
        f"NVIS_BUILD_NCCL={extra_env['NVIS_BUILD_NCCL']} "
        f"NVIS_CUDA_NCCL_BUILD_HOST={shlex.quote(build_host)} "
        "bash bin/scripts/install-cuda-nccl-workspace.sh "
        f"{shlex.quote(str(ws))} {shlex.quote(cuda_major)} {shlex.quote(arch)}"
    )
    lock_fh, lock_busy = _tools_try_acquire_exclusive_flock_for_stream("install_cuda_nccl_workspace")
    if lock_busy is not None:

        def _lock_busy_gen() -> Iterator[bytes]:
            yield f"ERROR: {lock_busy}\n".encode()

        return Response(
            stream_with_context(_lock_busy_gen()),
            status=409,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )

    def _cuda_nccl_stream_gen() -> Iterator[bytes]:
        try:
            yield from _merged_stdout_stream_generator(
                cmd,
                str(REPO_ROOT),
                display_cmd,
                timeout,
                linebuffer_env_var="RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER",
                env_extra=extra_env,
                privileged=True,
            )
        finally:
            _tools_release_exclusive_flock_fh(lock_fh)

    return Response(
        stream_with_context(_cuda_nccl_stream_gen()),
        mimetype="text/plain; charset=utf-8",
        headers=dict(_STREAM_NO_CACHE_HEADERS),
    )


@bp.route("/api/tools/install-nvloom-workspace", methods=["POST"])
@action("tools:install_nvloom")
def api_tools_install_nvloom_workspace():
    """Run ``install-nvloom-workspace.sh``: git clone on localhost, build on ``build_host`` via pdsh."""
    if os.environ.get("RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    build_host, bh_err = _validate_cuda_nccl_build_host(body.get("build_host"))
    if bh_err is not None:
        return jsonify({"ok": False, "error": bh_err}), 400
    assert build_host is not None
    try:
        ws = _resolve_workspace(body)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = script_tree.resolve_path("install-nvloom-workspace.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_TIMEOUT", 28800))
    extra_env = {"NVIS_NVLOOM_BUILD_HOST": build_host}
    cmd = ["bash", str(script), str(ws), cuda_major]
    display_cmd = (
        f"NVIS_NVLOOM_BUILD_HOST={shlex.quote(build_host)} "
        "bash bin/scripts/install-nvloom-workspace.sh "
        f"{shlex.quote(str(ws))} {shlex.quote(cuda_major)}"
    )
    try:
        with _tools_exclusive_lock("install_nvloom_workspace"):
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(extra_env),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except _ToolsExclusiveLockBusyError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": f"install-nvloom-workspace timed out after {timeout}s"}), 504
    except Exception as e:
        _logger.debug("api_tools_install_nvloom_workspace: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "build_host": build_host,
        }
    ), (200 if ok else 502)


@bp.route("/api/tools/install-nvloom-workspace-stream", methods=["POST"])
@action("tools:install_nvloom", variant="stream")
def api_tools_install_nvloom_workspace_stream():
    """Stream ``install-nvloom-workspace.sh`` (POST JSON: ``workspace``, ``cuda_major``, ``build_host``)."""
    if os.environ.get("RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    cuda_major = str(body.get("cuda_major") or "13").strip()
    if cuda_major not in ("12", "13"):
        return jsonify({"ok": False, "error": 'cuda_major must be "12" or "13"'}), 400
    build_host, bh_err = _validate_cuda_nccl_build_host(body.get("build_host"))
    if bh_err is not None:
        return jsonify({"ok": False, "error": bh_err}), 400
    assert build_host is not None
    try:
        ws = _resolve_workspace(body)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = script_tree.resolve_path("install-nvloom-workspace.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_TIMEOUT", 28800))
    extra_env = {"NVIS_NVLOOM_BUILD_HOST": build_host}
    cmd = ["bash", str(script), str(ws), cuda_major]
    display_cmd = (
        f"NVIS_NVLOOM_BUILD_HOST={shlex.quote(build_host)} "
        "bash bin/scripts/install-nvloom-workspace.sh "
        f"{shlex.quote(str(ws))} {shlex.quote(cuda_major)}"
    )
    lock_fh, lock_busy = _tools_try_acquire_exclusive_flock_for_stream("install_nvloom_workspace")
    if lock_busy is not None:

        def _lock_busy_gen() -> Iterator[bytes]:
            yield f"ERROR: {lock_busy}\n".encode()

        return Response(
            stream_with_context(_lock_busy_gen()),
            status=409,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )

    def _nvloom_stream_gen() -> Iterator[bytes]:
        try:
            yield from _merged_stdout_stream_generator(
                cmd,
                str(REPO_ROOT),
                display_cmd,
                timeout,
                linebuffer_env_var="RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER",
                env_extra=extra_env,
            )
        finally:
            _tools_release_exclusive_flock_fh(lock_fh)

    return Response(
        stream_with_context(_nvloom_stream_gen()),
        mimetype="text/plain; charset=utf-8",
        headers=dict(_STREAM_NO_CACHE_HEADERS),
    )


def _validate_exemplar_params(
    body: dict,
) -> tuple[dict[str, str] | None, str | None, dict | None]:
    """Validate an Exemplar (dgxc-benchmarking / llmb) setup request.

    Returns ``(extra_env, error, meta)``: ``extra_env`` is the ``NVIS_EXEMPLAR_*`` set
    forwarded to ``install-dgxc-benchmarking-workspace.sh`` over SSH; ``meta`` carries
    resolved values for the response/display line.
    """
    mode = str(body.get("mode") or "record").strip().lower()
    if mode not in ("record", "install"):
        return None, 'mode must be "record" or "install"', None
    gpu_type = str(body.get("gpu_type") or "gb200").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", gpu_type):
        return None, "gpu_type has invalid characters", None
    arch = str(body.get("arch") or "aarch64").strip()
    if arch not in ("aarch64", "x86_64"):
        return None, 'arch must be "aarch64" or "x86_64"', None
    # Settings -> job env NVIS_EXEMPLAR_WORKLOADS lets an operator narrow the install without a
    # frontend field. The stock default also pulls pretrain_nemotron-h, which downloads a NeMo
    # container via Slurm and is the main reason this action runs for hours — when only the NCCL
    # microbenchmark is wanted that download is pure cost.
    workloads = str(
        body.get("workloads") or _eget("NVIS_EXEMPLAR_WORKLOADS") or "microbenchmark_nccl,pretrain_nemotron-h"
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.,\- ]+", workloads):
        return None, "workloads has invalid characters", None
    extra: dict[str, str] = {
        "NVIS_EXEMPLAR_MODE": mode,
        "NVIS_EXEMPLAR_GPU_TYPE": gpu_type,
        "NVIS_EXEMPLAR_ARCH": arch,
        "NVIS_EXEMPLAR_WORKLOADS": workloads,
    }
    for key, env_name in (
        ("install_path", "NVIS_EXEMPLAR_INSTALL_PATH"),
        ("slurm_account", "NVIS_EXEMPLAR_SLURM_ACCOUNT"),
        ("gpu_partition", "NVIS_EXEMPLAR_GPU_PARTITION"),
        ("cpu_partition", "NVIS_EXEMPLAR_CPU_PARTITION"),
        ("image_folder", "NVIS_EXEMPLAR_IMAGE_FOLDER"),
        ("repo_branch", "NVIS_EXEMPLAR_REPO_BRANCH"),
    ):
        # Fall back to Settings -> job env when the form has no field for it. The UI exposes only
        # workspace/action/gpu_type/image_folder/uv/trust-git, so repo_branch (and the rest) were
        # unreachable: the body key is absent and nothing read the env, so the installer silently
        # used its built-in default. Body still wins when the form does provide a value.
        val = str(body.get(key) or _eget(env_name) or "").strip()
        if val:
            if not re.fullmatch(r"[A-Za-z0-9_./:@+\- ]+", val):
                return None, f"{key} has invalid characters", None
            extra[env_name] = val
    gres = str(body.get("gpu_gres") or "").strip()
    if gres:
        if not gres.isdigit():
            return None, "gpu_gres must be an integer", None
        extra["NVIS_EXEMPLAR_GPU_GRES"] = gres
    hf = str(body.get("hf_token") or "").strip()
    if hf:
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", hf):
            return None, "hf_token has invalid characters", None
        extra["NVIS_EXEMPLAR_HF_TOKEN"] = hf
    # use_uv (default true): let install.sh bootstrap uv (standalone Python 3.12). Required on
    # nodes whose system Python lacks ensurepip/python3-venv; needs astral.sh egress.
    use_uv_raw = body.get("use_uv", True)
    use_uv = use_uv_raw is True or str(use_uv_raw).strip().lower() in ("1", "true", "yes", "on")
    if use_uv:
        extra["NVIS_EXEMPLAR_USE_UV"] = "1"
    # trust_git (default true): auto-fix git "dubious ownership" on the workspace + the installer's
    # nested dependency clones (…/.cache/git_repos/*.git) so a server-node install doesn't abort
    # deep in install.sh and force a manual `git config safe.directory` per repo.
    trust_git_raw = body.get("trust_git", True)
    trust_git = trust_git_raw is True or str(trust_git_raw).strip().lower() in ("1", "true", "yes", "on")
    extra["NVIS_EXEMPLAR_TRUST_GIT"] = "1" if trust_git else "0"
    meta = {"mode": mode, "gpu_type": gpu_type, "arch": arch, "workloads": workloads, "use_uv": use_uv, "trust_git": trust_git}
    return extra, None, meta


def _exemplar_display_cmd(meta: dict, ws: str) -> str:
    return (
        f"NVIS_EXEMPLAR_MODE={shlex.quote(meta['mode'])} "
        f"NVIS_EXEMPLAR_GPU_TYPE={shlex.quote(meta['gpu_type'])} "
        "bash bin/scripts/install-dgxc-benchmarking-workspace.sh "
        f"{shlex.quote(str(ws))}"
    )


@bp.route("/api/tools/install-dgxc-benchmarking-workspace", methods=["POST"])
@action("tools:install_dgxc_benchmarking")
def api_tools_install_dgxc_benchmarking_workspace():
    """Run ``install-dgxc-benchmarking-workspace.sh``: clone + (record|install) Exemplar/llmb."""
    if os.environ.get("RACK_TOOLS_INSTALL_DGXC_BENCHMARKING_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_DGXC_BENCHMARKING_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    extra_env, err, meta = _validate_exemplar_params(body)
    if err is not None:
        return jsonify({"ok": False, "error": err}), 400
    assert extra_env is not None and meta is not None
    try:
        ws = _resolve_workspace(body)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = script_tree.resolve_path("install-dgxc-benchmarking-workspace.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    # install mode downloads the NeMo container via SLURM and can run for hours; record is fast.
    default_to = 28800 if meta["mode"] == "install" else 1800
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_DGXC_BENCHMARKING_TIMEOUT", default_to))
    cmd = ["bash", str(script), str(ws)]
    display_cmd = _exemplar_display_cmd(meta, str(ws))
    try:
        with _tools_exclusive_lock("install_dgxc_benchmarking_workspace"):
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(extra_env),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except _ToolsExclusiveLockBusyError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": f"install-dgxc-benchmarking timed out after {timeout}s"}), 504
    except Exception as e:
        _logger.debug("api_tools_install_dgxc_benchmarking_workspace: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "mode": meta["mode"],
        }
    ), (200 if ok else 502)


@bp.route("/api/tools/install-dgxc-benchmarking-workspace-stream", methods=["POST"])
@action("tools:install_dgxc_benchmarking", variant="stream")
def api_tools_install_dgxc_benchmarking_workspace_stream():
    """Stream ``install-dgxc-benchmarking-workspace.sh`` (POST JSON: ``workspace``, ``mode``, ``gpu_type``, …).

    Routed to the privileged ``script`` SSH plane (like ``run_llmb_nccl.sh``) so the
    workloads install to the same ``LLMB_INSTALL`` the NCCL/NeMo launchers later read.
    """
    if os.environ.get("RACK_TOOLS_INSTALL_DGXC_BENCHMARKING_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_TOOLS_INSTALL_DGXC_BENCHMARKING_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    extra_env, err, meta = _validate_exemplar_params(body)
    if err is not None:
        return jsonify({"ok": False, "error": err}), 400
    assert extra_env is not None and meta is not None
    try:
        ws = _resolve_workspace(body)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    script = script_tree.resolve_path("install-dgxc-benchmarking-workspace.sh")
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"script missing: {script}"}), 500
    default_to = 28800 if meta["mode"] == "install" else 1800
    timeout = max(300, _env_int("RACK_TOOLS_INSTALL_DGXC_BENCHMARKING_TIMEOUT", default_to))
    cmd = ["bash", str(script), str(ws)]
    display_cmd = _exemplar_display_cmd(meta, str(ws))
    lock_fh, lock_busy = _tools_try_acquire_exclusive_flock_for_stream("install_dgxc_benchmarking_workspace")
    if lock_busy is not None:

        def _lock_busy_gen() -> Iterator[bytes]:
            yield f"ERROR: {lock_busy}\n".encode()

        return Response(
            stream_with_context(_lock_busy_gen()),
            status=409,
            mimetype="text/plain; charset=utf-8",
            headers=dict(_STREAM_NO_CACHE_HEADERS),
        )

    def _dgxc_stream_gen() -> Iterator[bytes]:
        try:
            yield from _merged_stdout_stream_generator(
                cmd,
                str(REPO_ROOT),
                display_cmd,
                timeout,
                linebuffer_env_var="RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER",
                env_extra=extra_env,
                privileged=True,
            )
        finally:
            _tools_release_exclusive_flock_fh(lock_fh)

    return Response(
        stream_with_context(_dgxc_stream_gen()),
        mimetype="text/plain; charset=utf-8",
        headers=dict(_STREAM_NO_CACHE_HEADERS),
    )


_WORKFLOW_TOOL_KEYS = {
    "RACK_STATUS_SCRIPT": ("rack_status.sh", "Native Status"),
    "RACK_PARSER_SCRIPT": ("parser.sh", "Native Status"),
    "RACK_VALIDATION_SCRIPT": ("Singlerack validation", "Validation"),
    "RACK_NEMO_XRACK_SCRIPT": ("Multinode NEMO", "Validation"),
    "RACK_LLAMA_SCRIPT": ("Llama run", "Validation"),
    "RACK_EXEMPLAR_LLAMA_SCRIPT": ("Llama 3.1 405B Exemplar", "Validation"),
    "RACK_MNUBERGEMM_STRESS_SCRIPT": ("MNU Bergemm stress", "Validation"),
    "RACK_NVLOOM_SINGLERACK_SCRIPT": ("NVLoom singlerack", "Validation"),
    "RACK_CLUSTERKIT_SCRIPT": ("ClusterKit singlerack", "Validation"),
    "RACK_LLMB_NCCL_SCRIPT": ("NCCL Microbenchmark (LLMB)", "Validation"),
    "RACK_SYSINFO_SCRIPT": ("sysinfo / Precheck", "Cluster Tools"),
    "RACK_HW_CHECK_SCRIPT": ("Hardware check (pdsh)", "Cluster Tools"),
    "RACK_GPU_RESET_SCRIPT": ("GPU reset", "Cluster Tools"),
    "RACK_NVSWITCH_AUDIT_SCRIPT": ("NVSwitch audit", "Cluster Tools"),
    "RACK_NVSWITCH_SDN_RESET_SCRIPT": ("NVSwitch SDN reset", "Cluster Tools"),
    "RACK_NVSWITCH_SYSTEM_RESET_SCRIPT": ("NVSwitch system reset", "Cluster Tools"),
    "RACK_IMEX_RESET_SCRIPT": ("IMEX reset", "Cluster Tools"),
    "RACK_IMEX_CHECK_SCRIPT": ("IMEX check", "Cluster Tools"),
}


@bp.route("/api/tools/available", methods=["GET"])
@action(get="tools:list")
def api_tools_available():
    """Return predefined scripts/tools available for workflow automation steps."""
    tools = []
    for env_key, (label, category) in _WORKFLOW_TOOL_KEYS.items():
        desc = ""
        for defn in JOB_ENV_UI_DEFINITIONS:
            if defn.get("key") == env_key:
                desc = defn.get("description", "")
                break
        tools.append({
            "id": env_key,
            "label": label,
            "description": desc,
            "category": category,
            "path": os.environ.get(env_key, ""),
        })
    return jsonify({"ok": True, "tools": tools})
