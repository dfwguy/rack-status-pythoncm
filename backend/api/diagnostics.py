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
"""Read-only rack diagnostics: sysinfo, hardware check, and the pre-validation precheck.

The non-destructive third of rack operations. Nothing here changes cluster state; these routes collect
what a rack currently reports, which is what an operator runs before a validation campaign and again when
something has gone wrong.

The hw-check argument handling is the part worth understanding before editing it. ``hw_check_pdsh.sh``
takes flags, and the UI lets an operator supply them, so a free-text field ends up on a root pdsh command
line. Three defences, and they are deliberately layered:

* ``_HW_CHECK_PRESETS`` is the normal path -- a named preset maps to a fixed argv the operator cannot
  edit, so the common cases never involve parsing text at all.
* ``_hw_check_argv_from_custom_string`` handles the escape hatch, and every token must match
  ``_HW_CHECK_ARG_TOKEN_RE``; a token that does not is rejected rather than quoted, because a quoted
  surprise is still a surprise.
* ``_HW_CHECK_ARGS_MAX_LEN`` / ``_MAX_TOKENS`` bound the input so a pathological string cannot build a
  command line long enough to fail in an interesting way.

``api_run_sysinfo_stream`` streams output as it arrives instead of buffering: a full-rack sysinfo takes
minutes, and an operator watching a blank page cannot tell it apart from a hang.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import host_exec
import script_tree
from audit.actions import action
from config_env import _env_int, _job_env_int, _subprocess_env
from flask import Blueprint, Response, jsonify, request, stream_with_context
from job_registry import (
    _reset_job_purge_later,
    _reset_jobs,
    _reset_jobs_lock,
    _track_sync_op,
    reset_job_progress,
)
from naming import _normalize_rack_id
from node_targets import _expand_pdsh_w_slurm_nodenames
from paths import (
    _REMOTE_BASE,
    _hw_check_pdsh_script_path,
    _script_exists,
    _scripts_dir,
)
from results_ingest import _lake_push_job
from slurm_query import _rack_node_hostnames_from_inventory
from stream_exec import _merged_stdout_stream_generator
from validation_launch import merge_ct_individuals_for_power

from ._common import operator_from_request as _operator_from_request
from ._common import parse_sysinfo_request as _parse_sysinfo_request

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("diagnostics", __name__, url_prefix="")


_HW_CHECK_PRESETS: dict[str, list[str]] = {
    "dry_run": ["-d"],
    "default": [],
    "no_dmesg": ["-e", "0"],
    "with_imex": ["-i", "1"],
    "with_spx": ["-x", "1"],
    "with_imex_spx": ["-i", "1", "-x", "1"],
    "with_process": ["-p", "1"],
    "sys_only": ["-e", "0", "-s", "1", "-g", "0", "-i", "0", "-b", "0", "-x", "0", "-p", "0"],
    # --- NscaleTX validation presets (MOP GB300 §7.1.1 / FastTrack §7) ---
    # dcgmi diag standardised on -r 3 (2026-07-26). Long-running: minutes per node.
    "dcgm_diag": ["-e", "0", "-s", "0", "-g", "0", "-i", "0", "-b", "0", "-x", "0",
                  "-p", "0", "-D", "1", "-L", "3"],
    "nvbandwidth": ["-e", "0", "-s", "0", "-g", "0", "-i", "0", "-b", "0", "-x", "0",
                    "-p", "0", "-N", "1"],
    # BOM / inventory: nvidia-smi -L, ECC, nvlink -s, fabric state, topo -p2p n
    "bom_inventory": ["-e", "0", "-s", "0", "-g", "1", "-i", "0", "-b", "0", "-x", "0", "-p", "0"],
    # Job-launch stack: slurmd, munge, Prolog/Epilog (incl. a dry execution), pyxis SPANK
    # plugin, enroot runtime dirs. Added 2026-07-28 after job 63850 was CANCELLED with
    # Reason=Prolog while every GPU/NVLink/CUDA check on the tray passed — nothing here
    # covered the launch path, so a tray that could not start a job still reported healthy.
    "slurm_stack": ["-e", "0", "-s", "0", "-g", "0", "-i", "0", "-b", "0", "-x", "0",
                    "-p", "0", "-S", "1"],
    # Full MOP pre-flight: BOM + dcgmi diag -r 3 + nvbandwidth + launch stack
    "mop_preflight": ["-e", "1", "-s", "1", "-g", "1", "-i", "0", "-b", "1", "-x", "0",
                      "-p", "0", "-D", "1", "-L", "3", "-N", "1", "-S", "1"],
}


_HW_CHECK_ARG_TOKEN_RE = re.compile(r"^[-a-zA-Z0-9_=.,]+$")


_HW_CHECK_ARGS_MAX_LEN = 512


_HW_CHECK_ARGS_MAX_TOKENS = 48


def _hw_check_validate_argv_tokens(tokens: list[str]) -> list[str]:
    if len(tokens) > _HW_CHECK_ARGS_MAX_TOKENS:
        raise ValueError(f"too many checker_args tokens (max {_HW_CHECK_ARGS_MAX_TOKENS})")
    for p in tokens:
        if not p:
            raise ValueError("empty checker_args token")
        if not _HW_CHECK_ARG_TOKEN_RE.fullmatch(p):
            raise ValueError(
                f"disallowed character in checker_args token {p!r}; "
                "allowed: letters, digits, hyphen, underscore, period, comma, equals"
            )
    return list(tokens)


def _hw_check_argv_from_custom_string(raw: str) -> list[str]:
    """Split user-supplied checker flags with shlex (POSIX); reject shell metacharacters per token."""
    s = raw.strip()
    if not s:
        raise ValueError("empty checker_args")
    if len(s) > _HW_CHECK_ARGS_MAX_LEN:
        raise ValueError(f"checker_args too long (max {_HW_CHECK_ARGS_MAX_LEN} characters)")
    try:
        parts = shlex.split(s, comments=False, posix=True)
    except ValueError as e:
        raise ValueError(f"invalid checker_args quoting: {e}") from e
    return _hw_check_validate_argv_tokens(parts)


def _hw_check_argv_for_preset(preset: str) -> list[str]:
    key = (preset or "").strip().lower() or "default"
    if key not in _HW_CHECK_PRESETS:
        allowed = ", ".join(sorted(_HW_CHECK_PRESETS))
        raise ValueError(f"unknown preset {preset!r}; use one of: {allowed}")
    return list(_HW_CHECK_PRESETS[key])


def run_hw_check(pdsh_w: str, checker_argv: list[str]) -> dict:
    """Run hw_check_pdsh.sh once; checker_argv is passed after -- to checker.sh on each node."""
    script = _hw_check_pdsh_script_path()
    if not _script_exists(script):
        raise OSError(f"hw_check_pdsh.sh not found: {script}")
    w = _expand_pdsh_w_slurm_nodenames((pdsh_w or "").strip())
    if not w:
        raise ValueError("empty pdsh -w pattern")
    timeout = max(60, _env_int("RACK_HW_CHECK_TIMEOUT", 3600))
    dry = os.environ.get("RACK_HW_CHECK_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    cmd = ["bash", str(script), w, "--", *checker_argv]
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    if dry:
        return {
            "returncode": 0,
            "stdout": "dry-run (RACK_HW_CHECK_DRY_RUN)",
            "stderr": "",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    try:
        # Route through the privileged script profile (root@slogin) like sysinfo:
        # hw_check_pdsh.sh pdsh's to compute nodes and needs root on each node, and
        # the script lives on the cluster filesystem (not the dashboard host).
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
            env=_subprocess_env(),
            profile="script",
        )
    except FileNotFoundError:
        raise OSError("bash executable not found") from None
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
        "pdsh_w": w,
    }


def _prepare_hw_check(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    if os.environ.get("RACK_HW_CHECK_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "HW check disabled (RACK_HW_CHECK_DISABLED)"}), 403)
    specs_raw = body.get("specs")
    pdsh_w = ""
    if isinstance(specs_raw, list) and specs_raw:
        try:
            specs = merge_ct_individuals_for_power([str(x) for x in specs_raw])
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)
        if not specs:
            return None, (jsonify({"ok": False, "error": "no valid tray specs after merge"}), 400)
        pdsh_w = ",".join(specs)
    else:
        # Fallback: derive the whole-rack node list from live inventory (real Slurm
        # NodeNames) when the client sent no tray specs. Keeps hw-check working on
        # sites where the dashboard tray parse is naming-template dependent (e.g.
        # Boston's bos01-aRR-NN-s1-dgx-GG-cNN hostnames).
        rack = (body.get("rack") or "").strip()
        hosts = _rack_node_hostnames_from_inventory(rack) if rack else []
        if hosts:
            pdsh_w = ",".join(hosts)
        else:
            return None, (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            'send JSON {"specs": [...], "preset": "…"} and/or optional "checker_args": "-d …" or ["-d","…"]'
                        ),
                    }
                ),
                400,
            )

    checker_args_display: str | None = None
    argv: list[str]
    preset: str

    custom = body.get("checker_args")
    if isinstance(custom, list) and custom:
        try:
            parts = [str(x).strip() for x in custom]
        except Exception:
            _logger.debug("_prepare_hw_check: ignoring error", exc_info=True)
            return None, (jsonify({"ok": False, "error": "checker_args list must contain strings"}), 400)
        parts = [p for p in parts if p]
        if not parts:
            preset_raw = body.get("preset")
            preset = str(preset_raw).strip().lower() if preset_raw is not None else "default"
            try:
                argv = _hw_check_argv_for_preset(preset)
            except ValueError as e:
                return None, (jsonify({"ok": False, "error": str(e)}), 400)
        else:
            try:
                argv = _hw_check_validate_argv_tokens(parts)
            except ValueError as e:
                return None, (jsonify({"ok": False, "error": str(e)}), 400)
            preset = "custom"
            checker_args_display = " ".join(shlex.quote(p) for p in argv)[:400]
    elif isinstance(custom, str) and custom.strip():
        try:
            argv = _hw_check_argv_from_custom_string(custom)
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)
        preset = "custom"
        checker_args_display = custom.strip()[:400]
    else:
        preset_raw = body.get("preset")
        preset = str(preset_raw).strip().lower() if preset_raw is not None else "default"
        try:
            argv = _hw_check_argv_for_preset(preset)
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)

    return {
        "pdsh_w": pdsh_w,
        "preset": preset,
        "checker_argv": argv,
        "checker_args_display": checker_args_display,
    }, None


def _hw_check_worker(job_id: str, pdsh_w: str, preset: str, checker_argv: list[str]) -> None:
    """Run hw_check_pdsh.sh in a background thread."""

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

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        result = run_hw_check(pdsh_w, checker_argv)
        ok = result.get("returncode") == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=result.get("returncode"),
            stdout=result.get("stdout", ""),
            stderr=result.get("stderr", ""),
            command_display=result.get("command_display", ""),
            pdsh_w=result.get("pdsh_w", ""),
            preset=preset,
            error=None if ok else "hw_check_pdsh.sh exited non-zero (see stdout/stderr)",
        )
    except Exception as e:
        _logger.debug("_hw_check_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display="",
            pdsh_w=pdsh_w,
            preset=preset,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@bp.route("/api/hw-check-async", methods=["POST"])
@action("diag:hw_check", variant="async")
def api_hw_check_async():
    """POST JSON {\"specs\": [...], \"preset\": \"…\", \"rack\": \"…\", \"checker_args\": \"…\"} — optional checker_args string (or JSON list) overrides preset."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_hw_check(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    pdsh_w = str(prep["pdsh_w"])
    preset = str(prep["preset"])
    argv = prep["checker_argv"]
    assert isinstance(argv, list)
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "hw_check",
            "operator": _operator_from_request(),
            "rack": rk,
            "preset": preset,
            "checker_args": prep.get("checker_args_display"),
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_hw_check_worker,
        args=(job_id, pdsh_w, preset, list(argv)),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "hw_check", "rack": rk})


def _checker_script_path() -> Path:
    """Bundled checker.sh (same directory as hw_check_pdsh.sh)."""
    return script_tree.resolve_path("checker.sh")


@bp.route("/api/hw-check-presets", methods=["GET"])
@action(get="diag:list")
def api_hw_check_presets():
    """Named checker argument bundles for ``/api/hw-check-async`` (``preset`` vs custom ``checker_args``)."""
    if os.environ.get("RACK_HW_CHECK_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "HW check disabled (RACK_HW_CHECK_DISABLED)"}), 403
    presets = {k: list(v) for k, v in sorted(_HW_CHECK_PRESETS.items())}
    return jsonify({"ok": True, "presets": presets})


@bp.route("/api/hw-check-help", methods=["GET"])
@action(get="diag:read")
def api_hw_check_help():
    """checker.sh --help on the dashboard host (RACK_WEB_PRINT_HELP=1 skips root check)."""
    if os.environ.get("RACK_HW_CHECK_HELP_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "disabled (RACK_HW_CHECK_HELP_DISABLED)", "help": ""}), 403
    script = _checker_script_path()
    if not _script_exists(script):
        return jsonify({"ok": False, "error": f"checker.sh not found: {script}", "help": ""}), 404
    env = _subprocess_env({"RACK_WEB_PRINT_HELP": "1"})
    timeout = max(5, _env_int("RACK_HW_CHECK_HELP_TIMEOUT", 20))
    try:
        # The script and its working directory belong to the scripts role, which may
        # be a different host from the app. Running this locally only worked while
        # the two happened to coincide.
        #
        # RACK_WEB_PRINT_HELP goes through env_extra, not env: env is a local-only
        # subprocess argument, so over ssh the flag that skips checker.sh's root
        # check would be dropped and --help would fail with "Please run as root".
        proc = host_exec.run_on_host(
            ["bash", str(script), "--help"],
            cwd=_scripts_dir(),
            env=env,
            env_extra={"RACK_WEB_PRINT_HELP": "1"},
            timeout=timeout,
            role="scripts",
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "checker.sh --help timed out", "help": ""}), 504
    except OSError as e:
        return jsonify({"ok": False, "error": str(e), "help": ""}), 500
    help_out = (proc.stdout or "").strip()
    err_out = (proc.stderr or "").strip()
    help_text = (help_out + ("\n" + err_out if err_out else "")).strip()
    ok = proc.returncode == 0
    return jsonify({"ok": ok, "help": help_text, "returncode": proc.returncode}), (200 if ok else 502)


def _prepare_precheck_async(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate JSON for async Precheck (sysinfo.sh)."""
    prep, err = _parse_sysinfo_request(body)
    if err is not None:
        return None, err
    assert prep is not None
    return {
        "log_dir": prep["log_dir"],
        "rack": prep["rack"],
        "target": prep["target"],
        "cmd": list(prep["cmd"]),
        "display_cmd": prep["display_cmd"],
    }, None


def _precheck_async_worker(job_id: str, log_dir: Path, cmd: list[str], display_cmd: str, rack: str) -> None:
    """Run async precheck via sysinfo.sh in a background thread."""
    timeout_sec = max(30, _job_env_int("RACK_SYSINFO_TIMEOUT", 1800))
    with reset_job_progress(
        job_id, display_cmd=display_cmd, label="Precheck", timeout_sec=timeout_sec
    ) as prog:
        # A cluster script with the log dir as cwd: both belong to the execution host, so this goes
        # through the connection on the privileged route (see docs/EXECUTION_ARCHITECTURE.md 1.2)
        # rather than a local subprocess.
        proc = host_exec.run_on_host(
            cmd, cwd=str(log_dir), env=_subprocess_env(), timeout=timeout_sec, role="script"
        )
        prog.finish(
            proc.returncode, proc.stdout, proc.stderr, nonzero_error="sysinfo.sh exited non-zero (see stdout/stderr)"
        )


@bp.route("/api/precheck-async", methods=["POST"])
@action("diag:precheck", variant="async")
def api_precheck_async():
    """POST JSON ``{"rack": "pNN-rMM", "log_dir": "…"}`` for async Precheck (sysinfo.sh)."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_precheck_async(body)
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
            "kind": "precheck",
            "operator": _operator_from_request(),
            "rack": rack,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_precheck_async_worker,
        args=(job_id, log_dir, cmd, display_cmd, rack),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "precheck", "rack": rack})


def _sysinfo_stream_generator(prep: dict):
    """Yield stdout+stderr bytes from sysinfo.sh until process exit (or timeout)."""
    timeout_sec = max(30, _job_env_int("RACK_SYSINFO_TIMEOUT", 1800))
    cwd = _REMOTE_BASE if (_REMOTE_BASE and host_exec.exec_kind() == "ssh") else str(prep["log_dir"])
    yield from _merged_stdout_stream_generator(
        list(prep["cmd"]),
        cwd,
        prep["display_cmd"],
        timeout_sec,
        linebuffer_env_var="RACK_SYSINFO_LINEBUFFER",
        linebuffer_default="1",
        privileged=True,
    )


@bp.route("/api/run-sysinfo", methods=["POST"])
@action("diag:sysinfo")
def api_run_sysinfo():
    """Run sysinfo.sh with one NODELIST target, e.g. p22-r05-ct[01-18] (cwd = log_dir)."""
    body = request.get_json(silent=True) or {}
    prep, err = _parse_sysinfo_request(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    try:
        with _track_sync_op(prep.get("rack") or "", "sysinfo", _operator_from_request()):
            proc = subprocess.run(
                prep["cmd"],
                cwd=str(prep["log_dir"]),
                env=_subprocess_env(),
                capture_output=True,
                text=True,
                timeout=max(30, _job_env_int("RACK_SYSINFO_TIMEOUT", 1800)),
                check=False,
            )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "sysinfo.sh timed out", "target": prep["target"]}), 504
    except Exception as e:
        _logger.debug("api_run_sysinfo: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e), "target": prep["target"]}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "rack": prep["rack"],
            "target": prep["target"],
            "command_display": prep["display_cmd"],
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    ), (200 if ok else 502)


@bp.route("/api/run-sysinfo-stream", methods=["POST"])
@action("diag:sysinfo", variant="stream")
def api_run_sysinfo_stream():
    """Stream merged stdout/stderr from sysinfo.sh as text/plain (UTF-8)."""
    body = request.get_json(silent=True) or {}
    prep, err = _parse_sysinfo_request(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None

    rk_for_lock = str(prep.get("rack") or "")
    operator = _operator_from_request()

    def _tracked_sysinfo_stream() -> Iterator[bytes]:
        with _track_sync_op(rk_for_lock, "sysinfo", operator):
            yield from _sysinfo_stream_generator(prep)

    return Response(
        stream_with_context(_tracked_sysinfo_stream()),
        mimetype="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
