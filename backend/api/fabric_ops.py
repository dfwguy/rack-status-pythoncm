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
"""NVLink fabric operations: NVSwitch audit/reset, IMEX check/reset, NVLink preflight, nvlmapper.

The NVLink domain is the part of an NVL72 rack that a node-level tool cannot see. These routes address
the switch trays and the IMEX daemons that make a rack one coherent domain, which is why they are here
rather than in ``rack_power``: the target is not a compute node.

Three things about this domain are not obvious from the code:

* NVSwitch trays are named on their own scheme, not the compute scheme, so
  ``nvswitch_hosts_for_rack_list`` builds targets through the naming templates. Deriving them from a
  rack id by hand produces hosts that do not resolve.
* An IMEX reset is disruptive in a way a GPU reset is not -- it tears down the fabric a running job is
  using -- so ``_prepare_imex_reset_async`` is a separate confirmation step from the check.
* nvlmapper writes report trees on the execution host and ``api_nvlmapper_zip`` serves them back.
  ``_nvlmapper_zip_path_allowed`` is the containment check that keeps that from becoming an arbitrary
  file read: the requested path must resolve inside the configured reports base. ``_nvlmapper_stage_
  cleanup_keep_outputs`` is deliberately narrow, removing staging while preserving the outputs an
  operator came for.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import host_exec
import nvlink_preflight
from config_env import _eget, _env_int, _job_env_int, _subprocess_env
from core import rpath as _rpath
from flask import Blueprint, Response, jsonify, request, send_file, stream_with_context
from job_registry import (
    _reset_job_purge_later,
    _reset_jobs,
    _reset_jobs_lock,
    _track_sync_op,
    reset_job_progress,
)
from naming import (
    _normalize_nvswitch_host_for_rack,
    _normalize_rack_id,
    _nvis_naming,
    _nvswitch_host_sort_key,
)
from node_targets import _expand_pdsh_w_slurm_nodenames, validation_target_for_rack
from paths import (
    _REMOTE_BASE,
    SCRIPTS_DIR,
    _imex_check_script_path,
    _imex_reset_script_path,
    _nvlmapper_reports_base,
    _nvswitch_sdn_reset_script_path,
    _nvswitch_system_reset_script_path,
    _nvswitches_rack_audit_script_path,
    _resolve_log_dir,
    _script_exists,
    _scripts_dir,
)
from platforms import get_platform as _get_platform
from results_ingest import _lake_push_job
from stream_exec import _merged_stdout_stream_generator
from validation_launch import _host_job_cwd

from ._common import operator_from_request as _operator_from_request
from ._common import parse_sysinfo_request as _parse_sysinfo_request

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("fabric_ops", __name__, url_prefix="")


def nvswitch_hosts_for_rack_list(rack: str) -> list[str]:
    """Expand NV switch hostnames for *rack* via ``nodeset`` when available; else template fallbacks."""
    naming = _nvis_naming()
    rk = _normalize_rack_id(rack)
    out: list[str] = []
    timeout = max(5, _env_int("RACK_NVSWITCH_NODESET_TIMEOUT", 30))
    for spec in naming.nvsw_nodeset_specs(rk):
        try:
            proc = subprocess.run(
                ["nodeset", "-e", spec],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            break
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode != 0:
            continue
        for tok in (proc.stdout or "").split():
            t = tok.strip().lower()
            if not t:
                continue
            try:
                _normalize_nvswitch_host_for_rack(rk, t)
            except ValueError:
                continue
            out.append(t)
        if out:
            break
    if not out:
        out = naming.nvsw_fallback_hosts(rk)
    return sorted(set(out), key=_nvswitch_host_sort_key)


def _prepare_nvswitch_fix_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async NMXC cycle (``nvswitches_rack_audit.sh --fix RACK``)."""
    if os.environ.get("RACK_NVSWITCH_AUDIT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "nvswitch audit disabled (RACK_NVSWITCH_AUDIT_DISABLED)"}),
            403,
        )
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    try:
        rack = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _nvswitches_rack_audit_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"nvswitch audit script not found: {script}"}), 500)
    cmd: list[str] = ["bash", str(script), "--no-color", "--fix", rack]
    display_cmd = "./" + script.name + " --fix " + shlex.quote(rack)
    return {
        "log_dir": log_dir,
        "rack": rack,
        "cmd": cmd,
        "display_cmd": display_cmd,
    }, None


def _nvswitch_fix_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run ``nvswitches_rack_audit.sh --fix`` in a background thread."""
    timeout_sec = max(60, _env_int("RACK_NVSWITCH_AUDIT_TIMEOUT", 7200))
    with reset_job_progress(
        job_id, display_cmd=display_cmd, label="nvswitch fix", timeout_sec=timeout_sec
    ) as prog:
        # A cluster script with the log dir as cwd: both belong to the execution host, so this goes
        # through the connection on the privileged route (see docs/EXECUTION_ARCHITECTURE.md 1.2)
        # rather than a local subprocess.
        proc = host_exec.run_on_host(
            cmd, cwd=str(log_dir), env=_subprocess_env(), timeout=timeout_sec, role="script"
        )
        prog.finish(
            proc.returncode, proc.stdout, proc.stderr, nonzero_error="nvswitches_rack_audit.sh --fix exited non-zero (see stdout/stderr)"
        )


@bp.route("/api/nvswitch-fix-async", methods=["POST"])
def api_nvswitch_fix_async():
    """POST JSON ``{\"rack\": \"pNN-rMM\", \"log_dir\": \"…\"}`` — rack-level ``--fix`` (NMXC cycle); poll ``/api/reset-job-status``."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_nvswitch_fix_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    # PurePosixPath is NOT a subclass of Path, and ``_resolve_log_dir()`` returns one whenever the
    # log dir lives on a remote host. Asserting Path alone raised AssertionError -> HTTP 500 on
    # every call to this route and the six sibling async routes below. Accept either flavour.
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nmxc_cycle",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_nvswitch_fix_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "nmxc_cycle", "rack": rack})


def _prepare_nvswitch_sdn_reset_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async NVLink SDN reset (``nvswitch_sdn_reset.sh RACK``)."""
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    try:
        rack = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _nvswitch_sdn_reset_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"NVLink SDN reset script not found: {script}"}), 500)
    cmd: list[str] = ["bash", str(script), rack]
    display_cmd = "./" + script.name + " " + shlex.quote(rack)
    return {
        "log_dir": log_dir,
        "rack": rack,
        "cmd": cmd,
        "display_cmd": display_cmd,
    }, None


def _nvswitch_sdn_reset_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run ``nvswitch_sdn_reset.sh RACK`` in a background thread."""
    timeout_sec = max(60, _env_int("RACK_NVSWITCH_SDN_RESET_TIMEOUT", 1800))
    with reset_job_progress(
        job_id, display_cmd=display_cmd, label="NVSwitch SDN reset", timeout_sec=timeout_sec
    ) as prog:
        # role="script" routes to the privileged node-operational SSH target (RACK_SCRIPT_SSH_*,
        # root+nmxc) when configured, else falls back to control. Held open rather than fire-and-wait
        # so an overrun is killed and reaped here instead of leaking an SSH channel.
        hp = host_exec.popen_on_host(
            cmd, cwd=_host_job_cwd(log_dir), env=_subprocess_env(), role="script"
        )
        out, err = host_exec.communicate_or_kill(hp, timeout_sec)
        prog.finish(hp.returncode, out, err, nonzero_error="nvswitch_sdn_reset.sh exited non-zero (see stdout/stderr)")


@bp.route("/api/nvswitch-sdn-reset-async", methods=["POST"])
def api_nvswitch_sdn_reset_async():
    """POST JSON ``{"rack": "pNN-rMM", "log_dir": "…"}`` for async ``./nvswitch_sdn_reset.sh RACK``; poll ``/api/reset-job-status``."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_nvswitch_sdn_reset_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvswitch_sdn_reset",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_nvswitch_sdn_reset_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "nvswitch_sdn_reset", "rack": rack})


def _prepare_nvswitch_system_reset_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async NVLink system reset (``nvswitch_system_reset.sh RACK NVSWITCH_HOST``)."""
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    try:
        rack = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    try:
        nvswitch_host = _normalize_nvswitch_host_for_rack(rack, body.get("nvswitch_host"))
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _nvswitch_system_reset_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"NVLink system reset script not found: {script}"}), 500)
    cmd: list[str] = ["bash", str(script), rack, nvswitch_host]
    display_cmd = "./" + script.name + " " + shlex.quote(rack) + " " + shlex.quote(nvswitch_host)
    return {
        "log_dir": log_dir,
        "rack": rack,
        "nvswitch_host": nvswitch_host,
        "cmd": cmd,
        "display_cmd": display_cmd,
    }, None


def _nvswitch_system_reset_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run ``nvswitch_system_reset.sh RACK`` in a background thread."""
    timeout_sec = max(60, _env_int("RACK_NVSWITCH_SYSTEM_RESET_TIMEOUT", 1800))
    with reset_job_progress(
        job_id, display_cmd=display_cmd, label="NVSwitch system reset", timeout_sec=timeout_sec
    ) as prog:
        # role="script" routes to the privileged node-operational SSH target (RACK_SCRIPT_SSH_*,
        # root+nmxc) when configured, else falls back to control. Held open rather than fire-and-wait
        # so an overrun is killed and reaped here instead of leaking an SSH channel.
        hp = host_exec.popen_on_host(
            cmd, cwd=_host_job_cwd(log_dir), env=_subprocess_env(), role="script"
        )
        out, err = host_exec.communicate_or_kill(hp, timeout_sec)
        prog.finish(hp.returncode, out, err, nonzero_error="nvswitch_system_reset.sh exited non-zero (see stdout/stderr)")


@bp.route("/api/nvswitch-hosts", methods=["GET"])
def api_nvswitch_hosts():
    """GET query ``rack=pNN-rMM`` — list NV switch hostnames for the system-reset picker (``nodeset`` when available)."""
    try:
        rack = _normalize_rack_id((request.args.get("rack") or "").strip())
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e), "hosts": []}), 400
    hosts = nvswitch_hosts_for_rack_list(rack)
    return jsonify({"ok": True, "rack": rack, "hosts": hosts})


@bp.route("/api/nvswitch-system-reset-async", methods=["POST"])
def api_nvswitch_system_reset_async():
    """POST JSON ``{"rack": "pNN-rMM", "nvswitch_host": "pNN-rMM-nvsw-01", "log_dir": "…"}`` — async system reset on one switch; poll ``/api/reset-job-status``."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_nvswitch_system_reset_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    nvswitch_host = str(prep.get("nvswitch_host") or "")
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvswitch_system_reset",
            "operator": _operator_from_request(),
            "rack": rack,
            "nvswitch_host": nvswitch_host,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_nvswitch_system_reset_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "kind": "nvswitch_system_reset",
            "rack": rack,
            "nvswitch_host": nvswitch_host,
        }
    )


def _prepare_imex_reset_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async IMEX reset (``imex_reset.sh RACK``)."""
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    try:
        rack = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _imex_reset_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"IMEX reset script not found: {script}"}), 500)
    cmd: list[str] = ["bash", str(script), rack]
    display_cmd = "./" + script.name + " " + shlex.quote(rack)
    return {
        "log_dir": log_dir,
        "rack": rack,
        "cmd": cmd,
        "display_cmd": display_cmd,
    }, None


def _imex_reset_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run ``imex_reset.sh RACK`` in a background thread."""
    timeout_sec = max(60, _env_int("RACK_IMEX_RESET_TIMEOUT", 1800))
    with reset_job_progress(
        job_id, display_cmd=display_cmd, label="IMEX reset", timeout_sec=timeout_sec
    ) as prog:
        # role="script" routes to the privileged node-operational SSH target (RACK_SCRIPT_SSH_*,
        # root+nmxc) when configured, else falls back to control. Held open rather than fire-and-wait
        # so an overrun is killed and reaped here instead of leaking an SSH channel.
        hp = host_exec.popen_on_host(
            cmd, cwd=_host_job_cwd(log_dir), env=_subprocess_env(), role="script"
        )
        out, err = host_exec.communicate_or_kill(hp, timeout_sec)
        prog.finish(hp.returncode, out, err, nonzero_error="imex_reset.sh exited non-zero (see stdout/stderr)")


@bp.route("/api/imex-reset-async", methods=["POST"])
def api_imex_reset_async():
    """POST JSON ``{"rack": "pNN-rMM", "log_dir": "…"}`` for async ``./imex_reset.sh RACK``; poll ``/api/reset-job-status``."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_imex_reset_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    # `_resolve_log_dir()` returns a **PurePosixPath** in ssh mode (the log dir lives on the head
    # node), and PurePosixPath is NOT a subclass of Path — so `assert isinstance(log_dir, Path)`
    # raised AssertionError -> HTTP 500 on EVERY call. This route has never worked in ssh mode.
    # Same bug class as the log readers fixed earlier (see the comment at api_rack_log_content);
    # this handler never got the memo. Accept either flavour.
    log_dir = prep["log_dir"]
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "imex_reset",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_imex_reset_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "imex_reset", "rack": rack})


def _prepare_nvlmapper_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async NVLMAPPER (stage under reports/<rack>, run ``./run_nvlmapper.sh <rack>``)."""
    if os.environ.get("RACK_NVLMAPPER_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "NVLMAPPER disabled (RACK_NVLMAPPER_DISABLED)"}), 403)
    try:
        rack = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    nvlmapper_bin = SCRIPTS_DIR / "nvlmapper"
    if not _script_exists(nvlmapper_bin):
        return None, (
            jsonify(
                {
                    "ok": False,
                    "error": (
                        f"nvlmapper binary not found ({nvlmapper_bin}); "
                        "copy from aihpc_val/tools/nvlmapper/dist_amd64"
                    ),
                }
            ),
            500,
        )
    runner = SCRIPTS_DIR / "run_nvlmapper.sh"
    if not _script_exists(runner):
        return None, (jsonify({"ok": False, "error": f"run_nvlmapper.sh not found ({runner})"}), 500)
    return {"rack": rack}, None


def _nvlmapper_zip_path_allowed(path: Path, rack: str) -> bool:
    """Legacy: allow only a finished-job zip under ``reports/<rack>/`` (new runs no longer create zips)."""
    base = _rpath(_nvlmapper_reports_base() / rack)
    try:
        _rpath(path).relative_to(base)
    except ValueError:
        return False
    name = path.name
    if not name.startswith(f"nvlmapper.{rack}.") or not name.lower().endswith(".zip"):
        return False
    return host_exec.is_file(path)


def _nvlmapper_stage_cleanup_keep_outputs(work: Path, rack: str) -> None:
    """Remove staged ``nvlmapper`` binary and ``run_nvlmapper.sh``; drop generated YAML with credentials.

    CSV / XLS / XLSX (and other outputs) remain under *work* for operators to copy from ``reports/<rack>/``.

    Runs on the execution host: the staging directory is wherever the tool ran,
    which is also the only place the credential-bearing YAML exists. Deleting it
    locally would leave the real one in place.
    """
    doomed = [
        f"{work}/{name}"
        for name in ("nvlmapper", "run_nvlmapper.sh", f"config-{rack}.yaml", f"config_{rack}.yaml")
    ]
    # -f so an absent file is not an error; the YAML only exists on some runs.
    host_exec.run_on_host(
        "rm -f " + " ".join(shlex.quote(p) for p in doomed),
        timeout=60,
        role="scripts",
    )


def _nvlmapper_async_worker(job_id: str, rack: str) -> None:
    """Stage ``nvlmapper`` + ``run_nvlmapper.sh``, run ``./run_nvlmapper.sh``, then clean staging only (keep outputs)."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    timeout_sec = max(120, _env_int("RACK_NVLMAPPER_TIMEOUT", 14400))
    work = _rpath(_nvlmapper_reports_base() / rack)
    display_cmd = f"(cd {shlex.quote(str(work))} && ./run_nvlmapper.sh {shlex.quote(rack)})"
    env = _subprocess_env()
    work_dir_s = str(work)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
            nvlmapper_work_dir=work_dir_s,
        )
        # Stage on the execution host, not here: the nvlmapper binary lives in the
        # scripts tree over there and the work dir is where the tool must run, so
        # a local mkdir/copy staged the files onto the wrong machine and then ran
        # nothing. One command keeps the copy on the far side of the connection.
        scripts_root = _scripts_dir()
        staged = [f"{work_dir_s}/nvlmapper", f"{work_dir_s}/run_nvlmapper.sh"]
        stage_cmd = " && ".join(
            [
                f"mkdir -p {shlex.quote(work_dir_s)}",
                f"cp -f {shlex.quote(scripts_root + '/nvlmapper')} {shlex.quote(staged[0])}",
                f"cp -f {shlex.quote(scripts_root + '/run_nvlmapper.sh')} {shlex.quote(staged[1])}",
                "chmod 755 " + " ".join(shlex.quote(p) for p in staged),
            ]
        )
        stage_proc = host_exec.run_on_host(stage_cmd, timeout=180, role="scripts")
        if stage_proc.returncode != 0:
            raise OSError(
                f"could not stage nvlmapper in {work_dir_s}: "
                f"{(stage_proc.stderr or '').strip()[:300]}"
            )
        proc = host_exec.run_on_host(
            ["./run_nvlmapper.sh", rack],
            cwd=work_dir_s,
            env=env,
            timeout=timeout_sec,
            role="scripts",
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        ok = proc.returncode == 0
        _nvlmapper_stage_cleanup_keep_outputs(work, rack)
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=proc.returncode,
            stdout=out,
            stderr=err,
            command_display=display_cmd,
            nvlmapper_zip="",
            nvlmapper_work_dir=work_dir_s,
            error=None
            if ok
            else "NVLMAPPER run exited non-zero (see stdout/stderr); CSV/XLS outputs may still be present under the work directory",
        )
    except subprocess.TimeoutExpired:
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr=f"timeout after {timeout_sec}s",
            command_display=display_cmd,
            nvlmapper_zip="",
            nvlmapper_work_dir=work_dir_s,
            error=f"NVLMAPPER timed out after {timeout_sec}s",
        )
    except Exception as e:
        _logger.debug("_nvlmapper_async_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display=display_cmd,
            nvlmapper_zip="",
            nvlmapper_work_dir=work_dir_s,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@bp.route("/api/nvlmapper-async", methods=["POST"])
def api_nvlmapper_async():
    """POST JSON ``{"rack": "pNN-rMM"}`` — stage, ``./run_nvlmapper.sh``; outputs stay under ``reports/<rack>/`` (no zip). Poll ``/api/reset-job-status``."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_nvlmapper_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    rack = str(prep["rack"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvlmapper",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
            "nvlmapper_zip": "",
            "nvlmapper_work_dir": str(_rpath(_nvlmapper_reports_base() / rack)),
        }
    threading.Thread(
        target=_nvlmapper_async_worker,
        args=(job_id, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "nvlmapper", "rack": rack})


@bp.route("/api/nvlmapper-zip", methods=["GET"])
def api_nvlmapper_zip():
    """GET ``?job_id=…`` — legacy zip download (NVLMAPPER no longer builds zips; outputs live under ``nvlmapper_work_dir``)."""
    job_id = (request.args.get("job_id") or "").strip()
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    with _reset_jobs_lock:
        row = _reset_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        payload = dict(row)
    if str(payload.get("kind") or "") != "nvlmapper":
        return jsonify({"ok": False, "error": "not an nvlmapper job"}), 400
    ph = str(payload.get("phase") or "").lower()
    if ph not in ("done", "error"):
        return jsonify({"ok": False, "error": "job not finished yet"}), 400
    z = str(payload.get("nvlmapper_zip") or "").strip()
    if not z:
        wd = str(payload.get("nvlmapper_work_dir") or "").strip()
        msg = (
            "NVLMAPPER no longer produces a zip; copy CSV/XLS from the rack directory under the reports base."
            + (f" Work dir: {wd}" if wd else "")
        )
        return jsonify({"ok": False, "error": msg}), 400
    path = Path(z).resolve()
    rack = str(payload.get("rack") or "").strip().lower()
    if not rack or not _nvlmapper_zip_path_allowed(path, rack):
        return jsonify({"ok": False, "error": "invalid or missing zip path"}), 400
    return send_file(
        path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=path.name,
    )


def _prepare_nvswitch_check_async(
    body: dict,
) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async NVSwitch check (nvswitch audit without --fix)."""
    body2 = dict(body)
    body2["fix"] = False
    prep, err = _parse_nvswitch_audit_request(body2)
    if err is not None:
        return None, err
    assert prep is not None
    return {
        "log_dir": prep["log_dir"],
        "rack": prep["rack"],
        "cmd": list(prep["cmd"]),
        "display_cmd": prep["display_cmd"],
    }, None


def _nvswitch_check_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run the NVSwitch audit in a background thread."""
    timeout_sec = max(60, _env_int("RACK_NVSWITCH_AUDIT_TIMEOUT", 7200))
    with reset_job_progress(
        job_id, display_cmd=display_cmd, label="NVSwitch check", timeout_sec=timeout_sec
    ) as prog:
        # A cluster script with the log dir as cwd: both belong to the execution host, so this goes
        # through the connection on the privileged route (see docs/EXECUTION_ARCHITECTURE.md 1.2)
        # rather than a local subprocess.
        proc = host_exec.run_on_host(
            cmd, cwd=str(log_dir), env=_subprocess_env(), timeout=timeout_sec, role="script"
        )
        prog.finish(
            proc.returncode, proc.stdout, proc.stderr, nonzero_error="nvswitch check exited non-zero (see stdout/stderr)"
        )


@bp.route("/api/nvswitch-check-async", methods=["POST"])
def api_nvswitch_check_async():
    """POST JSON ``{"rack": "pNN-rMM", "log_dir": "…"}`` for async nvswitch check."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_nvswitch_check_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvswitch_check",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_nvswitch_check_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "nvswitch_check", "rack": rack})


def _prepare_imex_check_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async IMEX check (imex-related checks from sysinfo only)."""
    prep, err = _parse_sysinfo_request(body)
    if err is not None:
        return None, err
    assert prep is not None
    script = _imex_check_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"IMEX check script not found: {script}"}), 500)
    cmd: list[str] = ["bash", str(script), str(prep["target"])]
    display_cmd = "./" + script.name + " " + shlex.quote(str(prep["target"]))
    return {
        "log_dir": prep["log_dir"],
        "rack": prep["rack"],
        "cmd": cmd,
        "display_cmd": display_cmd,
    }, None


def _imex_check_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run async IMEX check in a background thread."""
    timeout_sec = max(30, _job_env_int("RACK_SYSINFO_TIMEOUT", 1800))
    with reset_job_progress(
        job_id, display_cmd=display_cmd, label="IMEX check", timeout_sec=timeout_sec
    ) as prog:
        # A cluster script with the log dir as cwd: both belong to the execution host, so this goes
        # through the connection on the privileged route (see docs/EXECUTION_ARCHITECTURE.md 1.2)
        # rather than a local subprocess.
        proc = host_exec.run_on_host(
            cmd, cwd=str(log_dir), env=_subprocess_env(), timeout=timeout_sec, role="script"
        )
        prog.finish(
            proc.returncode, proc.stdout, proc.stderr, nonzero_error="imex_check.sh exited non-zero (see stdout/stderr)"
        )


@bp.route("/api/imex-check-async", methods=["POST"])
def api_imex_check_async():
    """POST JSON ``{"rack": "pNN-rMM", "log_dir": "…"}`` for async IMEX check."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_imex_check_async(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    assert isinstance(log_dir, (Path, PurePosixPath))
    rack = str(prep["rack"])
    cmd = list(prep["cmd"])
    assert isinstance(cmd, list)
    display_cmd = str(prep["display_cmd"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "imex_check",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_imex_check_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "imex_check", "rack": rack})


def _nvlink_preflight_script_path() -> Path:
    raw = (_eget("RACK_NVLINK_PREFLIGHT_SCRIPT") or "").strip()
    if raw:
        return _rpath(Path(raw))
    return _rpath(SCRIPTS_DIR / "nvlink_preflight.sh")


def run_nvlink_preflight_pdsh(pdsh_w: str, *, quick: bool) -> dict:
    """pdsh ``nvlink_preflight.sh`` across *pdsh_w*; returns per-node rows plus raw output.

    The script runs ON each node, so the deployed copy must be readable there (it lives under
    the shared RACK_REMOTE_BASE_PATH, which is NFS on BCM). Unlike hw-check there is no wrapper
    script doing the fan-out, so the pdsh command is built here.
    """
    script = _nvlink_preflight_script_path()
    if not _script_exists(script):
        raise OSError(f"nvlink_preflight.sh not found: {script}")
    w = _expand_pdsh_w_slurm_nodenames((pdsh_w or "").strip())
    if not w:
        raise ValueError("empty pdsh -w pattern")
    # Expand brackets rather than splitting on commas: this site's tray range CONTAINS a comma
    # (``[11-18,28-37]``), and scontrol expansion above is best-effort, so ``w`` may still be
    # bracketed. A naive split produced "r-[11-18" and "28-37]" and reported a healthy 18-node
    # rack as "2 nodes did not respond".
    expected = nvlink_preflight.expand_pdsh_w(w)
    # The functional probe alone allows 120s per node; pdsh fans out in parallel, so the wall
    # clock is roughly one node's worth plus scheduling. Quick mode skips it entirely.
    default_timeout = 600 if quick else 1800
    timeout = max(60, _env_int("RACK_NVLINK_PREFLIGHT_TIMEOUT", default_timeout))
    remote = f"bash {shlex.quote(str(script))}" + (" --quick" if quick else " --full")
    plat = _get_platform()
    cmd = plat.pdsh_cmd(w, remote)
    # Force the ssh transport. pdsh on this fleet defaults to rsh, which the compute nodes refuse
    # with "rcmd: socket: Permission denied" — every pdsh script in bin/scripts sets both of these
    # for that reason, but Platform.pdsh_cmd() does not, so a direct caller has to.
    if "pdsh" in (cmd[0] if cmd else "") and "-R" not in cmd:
        cmd = [cmd[0], "-R", "ssh", *cmd[1:]]
    env = _subprocess_env()
    env["PDSH_RCMD_TYPE"] = "ssh"
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
            env=env,
            profile="script",
        )
    except FileNotFoundError as e:
        raise OSError(f"pdsh not available on the execution host: {e}") from e
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    rows = nvlink_preflight.parse_nvlink_preflight_output(out, err, expected)
    summary = nvlink_preflight.summarize(rows)
    # pdsh exits non-zero when ANY node's command failed, and nvlink_preflight.sh exits 1 on a
    # FAIL verdict — so a non-zero rc here is the normal "some node is not ready" case, not a
    # tooling error. The verdict comes from the parsed rows, never from the exit code.
    return {
        "returncode": proc.returncode,
        "stdout": out,
        "stderr": err,
        "command_display": cmd_display,
        "pdsh_w": w,
        "quick": quick,
        "rows": rows,
        "summary": summary,
    }


def _nvlink_preflight_worker(job_id: str, pdsh_w: str, quick: bool, rack: str) -> None:
    """Run the NVLink preflight in a background thread; patches ``_reset_jobs[job_id]``."""

    def _patch(**kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            _lake_push_job(job_id, finished)

    try:
        _patch(phase="running", started_running_at=datetime.now(timezone.utc).isoformat())
        result = run_nvlink_preflight_pdsh(pdsh_w, quick=quick)
        summary = result.get("summary") or {}
        ready = bool(summary.get("ready"))
        _patch(
            # "done" means the check completed and every node is ready; anything else is
            # surfaced as an error phase so the UI cannot paint an un-ready rack green.
            #
            # ``phase`` and ``summary.ready`` are the verdict. Do NOT read ``ok`` from
            # /api/reset-job-status: that route sets ok=True to mean "status query succeeded"
            # and overwrites whatever the worker stored here.
            phase="done" if ready else "error",
            ok=ready,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=result.get("returncode"),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            command_display=result.get("command_display", ""),
            pdsh_w=result.get("pdsh_w", ""),
            quick=quick,
            rows=result.get("rows", []),
            summary=summary,
            error=None if ready else _nvlink_preflight_error_text(summary),
        )
    except (OSError, ValueError) as e:
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )
    finally:
        _reset_job_purge_later(job_id)


def _nvlink_preflight_error_text(summary: dict) -> str:
    """One line naming what is not ready, so the operator does not have to read the table.

    Only blocking conditions appear here. Skipped checks and warnings are reported in the payload
    (``nodes_with_skips`` / ``nodes_with_warnings``) but never make a rack not-ready.
    """
    bits: list[str] = []
    if summary.get("failed"):
        bits.append(f"{summary['failed']} node(s) FAILED")
    if summary.get("incomplete"):
        bits.append(f"{summary['incomplete']} incomplete")
    if summary.get("no_response"):
        bits.append(f"{summary['no_response']} did not respond")
    return "NVLink preflight: " + (", ".join(bits) if bits else "rack not ready")


@bp.route("/api/nvlink-preflight-async", methods=["POST"])
def api_nvlink_preflight_async():
    """POST ``{"rack": …, "specs"|"target": …, "quick": bool}`` — NVLink/NVLS readiness gate.

    Async because the full gate runs a 120s forced-NVLink probe per node. Poll the shared
    ``/api/reset-job-status?job_id=…``; the finished job carries ``rows`` and ``summary``.
    """
    if os.environ.get("RACK_NVLINK_PREFLIGHT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "NVLink preflight disabled (RACK_NVLINK_PREFLIGHT_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    rack_raw = body.get("rack")
    if not isinstance(rack_raw, str) or not rack_raw.strip():
        return jsonify({"ok": False, "error": "rack is required"}), 400
    try:
        rack = _normalize_rack_id(rack_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    # Prefer the operator's node selection; fall back to the whole rack.
    pdsh_w = ""
    target_raw = body.get("target")
    if isinstance(target_raw, str) and target_raw.strip():
        pdsh_w = target_raw.strip()
    else:
        specs_raw = body.get("specs")
        if isinstance(specs_raw, list) and specs_raw:
            pdsh_w = ",".join(str(x).strip() for x in specs_raw if str(x).strip())
    if not pdsh_w:
        try:
            pdsh_w = validation_target_for_rack(rack, None)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    quick = bool(body.get("quick", False))
    script = _nvlink_preflight_script_path()
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"nvlink_preflight.sh not found: {script}"}), 500

    job_id = str(uuid.uuid4())
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvlink_preflight",
            "operator": _operator_from_request(),
            "rack": rack,
            "quick": quick,
            "phase": "starting",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    threading.Thread(
        target=_nvlink_preflight_worker,
        args=(job_id, pdsh_w, quick, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "nvlink_preflight", "rack": rack, "pdsh_w": pdsh_w})


def _parse_nvswitch_audit_request(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Validate POST body for NVSwitch rack audit stream; prep has log_dir, rack, display_cmd, cmd."""
    if os.environ.get("RACK_NVSWITCH_AUDIT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "nvswitch audit disabled (RACK_NVSWITCH_AUDIT_DISABLED)"}),
            403,
        )
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    rack = (body.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    fix_raw = body.get("fix")
    fix = fix_raw is True or (
        isinstance(fix_raw, str) and fix_raw.strip().lower() in ("1", "true", "yes")
    )
    script = _nvswitches_rack_audit_script_path()
    if not _script_exists(script):
        return None, (
            jsonify({"ok": False, "error": f"nvswitch audit script not found: {script}"}),
            500,
        )
    cmd: list[str] = ["bash", str(script), "--no-color"]
    if fix:
        cmd.append("--fix")
    cmd.append(rk)
    if fix:
        display_cmd = "./" + script.name + " --fix " + shlex.quote(rk)
    else:
        display_cmd = "./" + script.name + " " + shlex.quote(rk)
    return (
        {
            "log_dir": log_dir,
            "rack": rk,
            "fix": fix,
            "display_cmd": display_cmd,
            "cmd": cmd,
        },
        None,
    )


def _nvswitch_audit_stream_generator(prep: dict):
    """Stream nvswitches_rack_audit.sh (check-only or --fix)."""
    timeout_sec = max(60, _env_int("RACK_NVSWITCH_AUDIT_TIMEOUT", 7200))
    cwd = _REMOTE_BASE if (_REMOTE_BASE and host_exec.exec_kind() == "ssh") else str(prep["log_dir"])
    yield from _merged_stdout_stream_generator(
        list(prep["cmd"]),
        cwd,
        prep["display_cmd"],
        timeout_sec,
        linebuffer_env_var="RACK_NVSWITCH_AUDIT_LINEBUFFER",
        linebuffer_default="1",
        privileged=True,
    )


@bp.route("/api/run-nvswitch-audit-stream", methods=["POST"])
def api_run_nvswitch_audit_stream():
    """Stream nvswitches_rack_audit.sh for one rack (optional body.fix true => --fix). cwd = log_dir."""
    body = request.get_json(silent=True) or {}
    prep, err = _parse_nvswitch_audit_request(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None

    rk_for_lock = str(prep.get("rack") or "")
    operator = _operator_from_request()

    def _tracked_nvswitch_audit_stream() -> Iterator[bytes]:
        with _track_sync_op(rk_for_lock, "nvswitch_audit", operator):
            yield from _nvswitch_audit_stream_generator(prep)

    return Response(
        stream_with_context(_tracked_nvswitch_audit_stream()),
        mimetype="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
