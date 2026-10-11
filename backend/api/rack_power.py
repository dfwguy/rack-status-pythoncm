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
"""Node-level operations on a rack: power, GPU reset, BMC recovery, MAC swap, service restarts.

The destructive end of the product. Every route here can take a tray or a whole rack out of service, so
each one is a prepare/confirm pair: ``_prepare_*`` validates the request and returns the exact target
list, and only then does a worker thread run the command. The browser never sends a command, only a
rack or a node spec.

Two constraints shaped this code and are easy to undo by accident:

* Node specs are matched against strict patterns (``_SINGLE_NODE_SPEC_RE``, ``_HOSTNAME_RE``,
  ``_MAC_RE``, ``_IFACE_NAME_RE``) before reaching a command line. These operations run as root via
  pdsh or cmsh, so an unvalidated token is a command-injection surface, not just a bad request.
* ``power_specs_from_ct_down_unk`` folds the trays a rack reports as down/unknown into cmsh power
  specs, because power actions address trays through the chassis manager rather than through Slurm --
  a node that Slurm cannot see is exactly the one that needs powering.

Long operations run in threads and report through the shared job registry, so ``GET /api/rack-locks``
shows a rack as busy while a reset is in flight and a second operator is warned before starting another.
"""

# Notes that stayed in app.py when this module took the code, during the api/ migration. The
# prose describes subjects that live here now; it is reunited with them rather than deleted.
#
# ---------------------------------------------------------------------------
# BMC recovery and MAC change
#
# Both target a single head node where BCM (cmsh / pythoncm) + ipmitool live.
# Invocation always routes through host_exec so the same code path serves
# local-on-head and dashboard-in-container deployments. Each call writes its
# stdout/stderr/returncode into the shared _reset_jobs table; the frontend
# polls /api/reset-job-status?job_id=… (no new polling endpoint needed).
# ---------------------------------------------------------------------------
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import host_exec
import script_tree
from audit.actions import action
from config_env import _eget, _env_int
from core import rpath as _rpath
from flask import Blueprint, jsonify, request
from job_registry import (
    _reset_job_purge_later,
    _reset_jobs,
    _reset_jobs_lock,
    _track_sync_op,
)
from naming import _ct_power_token_re, _normalize_rack_id, _nvis_naming
from node_targets import (
    _expand_pdsh_w_slurm_nodenames,
    _validation_ct_inner,
)
from paths import _script_exists
from platforms import get_platform as _get_platform
from results_ingest import _lake_push_job
from validation_launch import merge_ct_individuals_for_power

from ._common import operator_from_request as _operator_from_request

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("rack_power", __name__, url_prefix="")


def _power_reset_worker(job_id: str, specs: list[str]) -> None:
    """Run cmsh power cycle in a background thread."""

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

    sleep_sec = max(0, _env_int("RACK_POWER_RESET_SLEEP_SEC", 10))
    try:
        _patch(
            phase="running",
            reset_sleep_sec=sleep_sec,
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        results = run_cmsh_power_reset(specs)
        ok_all = all(r.get("returncode") == 0 for r in results)
        _patch(
            phase="done" if ok_all else "error",
            ok=ok_all,
            results=results,
            reset_sleep_sec=sleep_sec,
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=None if ok_all else "one or more cmsh power reset steps failed (see results)",
        )
    except Exception as e:
        _logger.debug("_power_reset_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            results=[],
            reset_sleep_sec=sleep_sec,
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )
    _reset_job_purge_later(job_id)


def _node_hosts(pdsh_w: str) -> list[str]:
    """The hostnames a ``-w`` pattern names, expanded (``a-[01-03],b`` -> three plus one)."""
    from universal_parser import expand_nodelist

    w = _expand_pdsh_w_slurm_nodenames((pdsh_w or "").strip())
    if not w:
        raise ValueError("empty pdsh -w pattern")
    return [h for h in expand_nodelist(w) if h]


def _run_node_command(pdsh_w: str, command: str, *, timeout: int, dry_run: bool, dry_env: str) -> dict:
    """Run *command* on the nodes *pdsh_w* names, by the cluster's node route and sudo setting.

    Success is counted per node: the result's ``returncode`` is 0 only when every node ran it
    successfully, and ``failed_nodes`` names the rest.
    """
    hosts = _node_hosts(pdsh_w)
    if dry_run:
        return {
            "returncode": 0,
            "stdout": f"dry-run ({dry_env})",
            "stderr": "",
            "command_display": f"on {len(hosts)} node(s): {command}",
            "pdsh_w": ",".join(hosts),
            "failed_nodes": [],
        }
    results = host_exec.run_on_nodes(hosts, command, timeout=timeout)
    return host_exec.node_results_summary(results, command)


def _node_job_error(what: str, result: dict) -> str | None:
    if result.get("returncode") == 0:
        return None

    def _names(nodes: list[str]) -> str:
        return ", ".join(nodes[:10]) + (f" (+{len(nodes) - 10} more)" if len(nodes) > 10 else "")

    failed = result.get("failed_nodes") or []
    skipped = result.get("skipped_nodes") or []
    parts = []
    if failed:
        parts.append(f"{what} failed on {len(failed)} node(s): {_names(failed)} (see stderr)")
    if skipped:
        parts.append(f"skipped {len(skipped)} node(s) running a job: {_names(skipped)}")
    return "; ".join(parts) or f"{what} exited non-zero (see stdout/stderr)"


def _nodes_running_jobs(hosts: list[str]) -> set[str]:
    """Which of *hosts* Slurm shows running a job. Raises when Slurm cannot be asked: a check
    that cannot read its input must not report the nodes free."""
    from universal_parser import expand_nodelist

    if not hosts:
        return set()
    proc = host_exec.run_slurm_on_host(
        _get_platform().squeue_cmd(f"-h -w {shlex.quote(','.join(hosts))} -o %N"),
        timeout=max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120)),
    )
    if proc.returncode != 0:
        raise OSError(f"squeue failed (rc={proc.returncode}): {(proc.stderr or '').strip()[:200]}")
    busy: set[str] = set()
    for line in (proc.stdout or "").split():
        busy.update(expand_nodelist(line))
    return busy & set(hosts)


def run_node_reboot(pdsh_w: str) -> dict:
    """Reboot the nodes *pdsh_w* names from the node itself: ``systemctl --no-block reboot``.

    For nodes Slurm cannot reboot: ``scontrol reboot`` is delivered by the node's slurmd, and a
    node Slurm holds INVALID or DOWN never gets it -- on Nscale NC gpu105-su21 (one GPU failed
    to initialise, so it registered 3 GPUs of 4) took "reboot issued" and timed out without
    rebooting. This runs through the cluster's node route (direct or via the head, under sudo
    when the cluster says so), so it reaches the node whatever Slurm thinks of it.

    A node Slurm shows running a job is skipped, never rebooted. A Slurm reboot still pending
    for a target is cancelled first, so Slurm does not reboot it a second time once it is back.
    ``--no-block`` queues the reboot and returns, so the node can answer before it goes down.
    """
    from slurm_admin import _run_scontrol

    hosts = _node_hosts(pdsh_w)
    dry = os.environ.get("RACK_NODE_REBOOT_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    busy = set() if dry else _nodes_running_jobs(hosts)
    targets = [h for h in hosts if h not in busy]
    skipped = [h for h in hosts if h in busy]
    if not targets:
        return {
            "returncode": 1, "stdout": "", "stderr": "", "pdsh_w": ",".join(hosts),
            "command_display": "", "failed_nodes": [], "skipped_nodes": skipped,
        }
    if not dry:
        cancelled = _run_scontrol(["cancel_reboot", ",".join(targets)])
        if not cancelled.get("ok"):
            # Nothing pending is the usual answer; it is not a reason to stop.
            _logger.info("cancel_reboot before node reboot: %s", (cancelled.get("stderr") or "")[:200])
    result = _run_node_command(
        ",".join(targets),
        "systemctl --no-block reboot",
        timeout=max(30, _env_int("RACK_NODE_REBOOT_TIMEOUT", 120)),
        dry_run=dry,
        dry_env="RACK_NODE_REBOOT_DRY_RUN",
    )
    result["skipped_nodes"] = skipped
    if skipped:
        result["returncode"] = 1
    return result


def run_services_restart(pdsh_w: str) -> dict:
    """On each node: mount -a, restart munge/slurmd/nvidia-imex, then nvidia-dcgm or cuda-dcgm."""
    return _run_node_command(
        pdsh_w,
        _get_platform().services_restart_remote_cmd(),
        timeout=max(30, _env_int("RACK_SERVICES_RESTART_TIMEOUT", 1800)),
        dry_run=os.environ.get("RACK_SERVICES_RESTART_DRY_RUN", "").strip().lower() in ("1", "true", "yes"),
        dry_env="RACK_SERVICES_RESTART_DRY_RUN",
    )


_PDSH_HOST_LINE_RE = re.compile(r"^([^:]+):\s*(.*)$")


def _parse_pdsh_prefixed_lines(stdout: str) -> list[dict[str, str]]:
    """Parse default pdsh stdout lines ``hostname: remote command output``."""
    rows: list[dict[str, str]] = []
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _PDSH_HOST_LINE_RE.match(line)
        if m:
            rows.append({"host": m.group(1).strip(), "uptime": m.group(2).strip()})
        else:
            rows.append({"host": "", "uptime": line})
    rows.sort(key=lambda r: (r.get("host") or "", r.get("uptime") or ""))
    return rows


def run_rack_uptime_pdsh(rack: str) -> dict:
    """Run ``pdsh -w <rack><ct_joiner>[RANGE] uptime`` (RANGE from RACK_VALIDATION_CT_RANGE)."""
    rk = _normalize_rack_id(rack)
    w = _expand_pdsh_w_slurm_nodenames(
        _nvis_naming().uptime_pdsh_wildcard(rack, _validation_ct_inner())
    )
    timeout = max(15, _env_int("RACK_UPTIME_PDSH_TIMEOUT", 120))
    dry = os.environ.get("RACK_UPTIME_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    plat = _get_platform()
    cmd = plat.pdsh_cmd(w, "uptime")
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    if dry:
        sample = f"{rk}-ct01:  12:00:00 up 1 day,  1 user,  load average: 0.00, 0.00, 0.00"
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "dry-run (RACK_UPTIME_DRY_RUN)\n" + sample,
            "stderr": "",
            "command_display": cmd_display,
            "pdsh_w": w,
            "rack": rk,
            "rows": _parse_pdsh_prefixed_lines(sample),
        }
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise OSError("pdsh executable not found") from None
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "pdsh_w": w,
            "rack": rk,
            "rows": [],
            "error": f"pdsh uptime timed out after {timeout}s",
        }
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    rows = _parse_pdsh_prefixed_lines(out)
    ok = proc.returncode == 0
    payload: dict[str, object] = {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": out,
        "stderr": err,
        "command_display": cmd_display,
        "pdsh_w": w,
        "rack": rk,
        "rows": rows,
    }
    if not ok:
        payload["error"] = "pdsh uptime exited non-zero (see stdout/stderr)"
    return payload


def _node_action_worker(job_id: str, pdsh_w: str, what: str, run: Callable[[str], dict]) -> None:
    """Run a per-node action (services restart, GPU reset, node reboot) in a background thread.

    The job is ``done`` only when every node succeeded; otherwise ``error`` names the nodes that
    did not (``failed_nodes``) and those the action left alone (``skipped_nodes``).
    """

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
        result = run(pdsh_w)
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
            failed_nodes=result.get("failed_nodes", []),
            skipped_nodes=result.get("skipped_nodes", []),
            error=_node_job_error(what, result),
        )
    except Exception as e:
        _logger.debug("_node_action_worker(%s): ignoring error", what, exc_info=True)
        _patch(
            phase="error",
            ok=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display="",
            pdsh_w=pdsh_w,
            error=str(e),
        )
    _reset_job_purge_later(job_id)


def _services_restart_worker(job_id: str, pdsh_w: str) -> None:
    _node_action_worker(job_id, pdsh_w, "services restart", run_services_restart)


def _gpu_reset_worker(job_id: str, pdsh_w: str) -> None:
    _node_action_worker(job_id, pdsh_w, "GPU reset", run_gpu_reset)


def _node_reboot_worker(job_id: str, pdsh_w: str) -> None:
    _node_action_worker(job_id, pdsh_w, "node reboot", run_node_reboot)


def power_specs_from_ct_down_unk(raw: str | None) -> list[str]:
    """Extract tray specs from ct_down_unk for cmsh power reset — only DN: (and legacy UNK:) bodies.

    INST:/UP: trays are excluded (non-down / installing / healthy).
    """
    s = (raw or "").strip()
    if not s or s in ("-", "?"):
        return []
    seen: set[str] = set()
    out: list[str] = []

    def add_from_fragment(fragment: str) -> None:
        for m in _ct_power_token_re().finditer(fragment):
            tok = m.group(1).lower()
            if tok in seen:
                continue
            seen.add(tok)
            out.append(tok)

    if re.search(r"(^|;)\s*(dn|unk)\s*:", s, re.IGNORECASE):
        for seg in s.split(";"):
            t = seg.strip()
            m = re.match(r"^(dn|unk)\s*:\s*(.*)$", t, re.IGNORECASE)
            if not m:
                continue
            add_from_fragment(m.group(2))
    else:
        add_from_fragment(s)
    return out


def _run_cmsh_device_power(
    cmsh: str,
    inner: str,
    timeout: int,
    spec: str,
    phase: str,
) -> dict:
    plat = _get_platform()
    cmd = plat.device_power_cmd(inner)
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise OSError(f"cluster manager executable not found: {cmsh}") from None
    except subprocess.TimeoutExpired:
        return {
            "spec": spec,
            "phase": phase,
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command": cmd_display,
        }
    return {
        "spec": spec,
        "phase": phase,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command": cmd_display,
    }


def run_cmsh_power_reset(specs: list[str]) -> list[dict]:
    """For each tray spec: cmsh device; power … off, sleep, then device; power … on."""
    cmsh = _eget("RACK_CMSH", "cmsh") or "cmsh"
    timeout = max(5, _env_int("RACK_POWER_TIMEOUT", 120))
    sleep_sec = max(0, _env_int("RACK_POWER_RESET_SLEEP_SEC", 10))
    dry = os.environ.get("RACK_POWER_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    results: list[dict] = []
    for spec in specs:
        inner_off = f"device; power -n {spec} off"
        inner_on = f"device; power -n {spec} on"
        if dry:
            results.append(
                {
                    "spec": spec,
                    "phase": "off",
                    "returncode": 0,
                    "stdout": "dry-run (RACK_POWER_DRY_RUN)",
                    "stderr": "",
                    "command": f"{shlex.quote(cmsh)} -c {shlex.quote(inner_off)}",
                }
            )
            results.append(
                {
                    "spec": spec,
                    "phase": "on",
                    "returncode": 0,
                    "stdout": "dry-run (RACK_POWER_DRY_RUN)",
                    "stderr": "",
                    "command": f"{shlex.quote(cmsh)} -c {shlex.quote(inner_on)}",
                }
            )
            continue
        results.append(_run_cmsh_device_power(cmsh, inner_off, timeout, spec, "off"))
        if sleep_sec:
            time.sleep(float(sleep_sec))
        results.append(_run_cmsh_device_power(cmsh, inner_on, timeout, spec, "on"))
    return results


def _prepare_power_reset(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Shared parse for sync and async power reset; returns ({\"specs\": [...]}, None) or (None, err)."""
    if os.environ.get("RACK_POWER_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "power-on disabled (RACK_POWER_DISABLED)"}), 403)
    specs_raw = body.get("specs")
    ctv = body.get("ct_down_unk")
    specs: list[str]
    if specs_raw is not None:
        if not isinstance(specs_raw, list):
            return None, (jsonify({"ok": False, "error": "specs must be an array of strings"}), 400)
        if not specs_raw:
            return None, (jsonify({"ok": False, "error": "specs must be a non-empty array"}), 400)
        try:
            specs = merge_ct_individuals_for_power([str(x) for x in specs_raw])
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)
        if not specs:
            return None, (jsonify({"ok": False, "error": "no valid tray specs after merge"}), 400)
    elif isinstance(ctv, str):
        specs = power_specs_from_ct_down_unk(ctv)
        if not specs:
            return None, (
                jsonify(
                    {
                        "ok": False,
                        "error": "no valid pNN-rMM-ct[digits/ranges] tokens in ct_down_unk",
                    }
                ),
                400,
            )
    else:
        return None, (
            jsonify(
                {
                    "ok": False,
                    "error": 'send JSON {"specs": ["pNN-rMM-ct[id]", ...]} or {"ct_down_unk": "…"}',
                }
            ),
            400,
        )
    return {"specs": specs}, None


@bp.route("/api/power-on", methods=["POST"])
@action("rack.power:on")
def api_power_on():
    """POST JSON {\"specs\": [\"pNN-rMM-ct[id]\", …]} or {\"ct_down_unk\": \"…\"} — cmsh power off/sleep/on per tray."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_power_reset(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    specs = prep["specs"]
    assert isinstance(specs, list)
    sleep_sec = max(0, _env_int("RACK_POWER_RESET_SLEEP_SEC", 10))
    rk_for_lock = (body.get("rack") or "").strip()
    try:
        with _track_sync_op(rk_for_lock, "power", _operator_from_request()):
            results = run_cmsh_power_reset(specs)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e), "results": []}), 500
    ok_all = all(r.get("returncode") == 0 for r in results)
    payload: dict = {
        "ok": ok_all,
        "results": results,
        "reset_sleep_sec": sleep_sec,
    }
    if not ok_all:
        payload["error"] = "one or more cmsh power reset steps failed (see results)"
    return jsonify(payload), (200 if ok_all else 502)


@bp.route("/api/power-on-async", methods=["POST"])
@action("rack.power:on", variant="async")
def api_power_on_async():
    """Start power reset in a background thread; poll GET /api/reset-job-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_power_reset(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    specs = prep["specs"]
    assert isinstance(specs, list)
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "power",
            "operator": _operator_from_request(),
            "rack": rk,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_power_reset_worker,
        args=(job_id, specs),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "power", "rack": rk})


def _gpu_reset_script_path() -> Path:
    raw = (_eget("RACK_GPU_RESET_SCRIPT") or "").strip()
    if raw:
        return _rpath(Path(raw))
    return script_tree.resolve_path("gpu_reset.sh")


def run_gpu_reset(pdsh_w: str) -> dict:
    """Reset the GPUs on the nodes *pdsh_w* names.

    By default this is the platform's ``gpu_reset_remote_cmd`` run on each node through the
    cluster's node route (direct or via the head, sudo or not), counted per node. A site that
    named its own script with ``RACK_GPU_RESET_SCRIPT`` keeps running that script on the head,
    as before.
    """
    if not (_eget("RACK_GPU_RESET_SCRIPT") or "").strip():
        return _run_node_command(
            pdsh_w,
            _get_platform().gpu_reset_remote_cmd(),
            timeout=max(30, _env_int("RACK_GPU_RESET_TIMEOUT", 3600)),
            dry_run=os.environ.get("RACK_GPU_RESET_DRY_RUN", "").strip().lower() in ("1", "true", "yes"),
            dry_env="RACK_GPU_RESET_DRY_RUN",
        )
    script = _gpu_reset_script_path()
    if not _script_exists(script):
        raise OSError(f"gpu_reset script not found: {script}")
    w = _expand_pdsh_w_slurm_nodenames((pdsh_w or "").strip())
    if not w:
        raise ValueError("empty pdsh -w pattern")
    timeout = max(30, _env_int("RACK_GPU_RESET_TIMEOUT", 3600))
    dry = os.environ.get("RACK_GPU_RESET_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    cmd = ["bash", str(script), w]
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    if dry:
        return {
            "returncode": 0,
            "stdout": "dry-run (RACK_GPU_RESET_DRY_RUN)",
            "stderr": "",
            "command_display": cmd_display,
            "pdsh_w": w,
        }
    try:
        proc = host_exec.run_on_host(
            cmd,
            timeout=timeout,
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


def _prepare_gpu_reset(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Shared parse for sync and async GPU reset."""
    if os.environ.get("RACK_GPU_RESET_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "GPU reset disabled (RACK_GPU_RESET_DISABLED)"}), 403)
    specs_raw = body.get("specs")
    if not isinstance(specs_raw, list) or not specs_raw:
        return None, (jsonify({"ok": False, "error": 'send JSON {"specs": ["pNN-rMM-ct[id]", ...]}'}), 400)
    try:
        specs = merge_ct_individuals_for_power([str(x) for x in specs_raw])
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    if not specs:
        return None, (jsonify({"ok": False, "error": "no valid tray specs after merge"}), 400)
    pdsh_w = ",".join(specs)
    return {"pdsh_w": pdsh_w}, None


@bp.route("/api/gpu-reset", methods=["POST"])
@action("rack.gpu:reset")
def api_gpu_reset():
    """POST JSON {\"specs\": [\"pNN-rMM-ct[id]\", …]} — merged tray pattern(s) passed to gpu_reset.sh."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_gpu_reset(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    pdsh_w = str(prep["pdsh_w"])
    rk_for_lock = (body.get("rack") or "").strip()
    try:
        with _track_sync_op(rk_for_lock, "gpu", _operator_from_request()):
            result = run_gpu_reset(pdsh_w)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = result.get("returncode") == 0
    payload: dict = {"ok": ok, **result}
    if not ok:
        payload["error"] = "gpu_reset.sh exited non-zero (see stdout/stderr)"
    return jsonify(payload), (200 if ok else 502)


@bp.route("/api/gpu-reset-async", methods=["POST"])
@action("rack.gpu:reset", variant="async")
def api_gpu_reset_async():
    """Start GPU reset in a background thread; poll GET /api/reset-job-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_gpu_reset(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    pdsh_w = str(prep["pdsh_w"])
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "gpu",
            "operator": _operator_from_request(),
            "rack": rk,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_gpu_reset_worker,
        args=(job_id, pdsh_w),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "gpu", "rack": rk})


_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


_IFACE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


_SINGLE_NODE_SPEC_RE = re.compile(r"^([a-z0-9-]+-ct)\[(\d+)\]$", re.IGNORECASE)


_HOSTNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.IGNORECASE)


def _bmc_recovery_script_path() -> Path:
    raw = (_eget("RACK_BMC_RECOVERY_SCRIPT") or "").strip()
    if raw:
        return _rpath(Path(raw))
    return script_tree.resolve_path("bmc-recovery.sh")


def _macswap_script_path() -> Path:
    raw = (_eget("RACK_MACSWAP_SCRIPT") or "").strip()
    if raw:
        return _rpath(Path(raw))
    return script_tree.resolve_path("macswap/macswap")


def _macswap_python_module() -> str:
    """Environment Modules name to load before invoking macswap (default ``python3``)."""
    raw = (_eget("RACK_MACSWAP_PYTHON_MODULE") or "").strip()
    return raw or "python3"


def _spec_to_hostname(spec: str) -> str | None:
    """Resolve a single-node tray spec or exact compute hostname.

    Returns ``None`` for range forms (e.g. ``pNN-rMM-ct[01-04]``) so the
    caller can fan-out only across explicit single-node selections.
    """
    token = spec.strip()
    m = _SINGLE_NODE_SPEC_RE.match(token)
    if m:
        return f"{m.group(1)}{m.group(2)}".lower()
    return token.lower() if _HOSTNAME_RE.match(token) else None


def run_bmc_recovery_one(node: str, iface: str) -> dict:
    """Run ``bmc-recovery.sh <node> <iface>`` on the head node and capture output."""
    script = _bmc_recovery_script_path()
    if not _script_exists(script):
        raise OSError(f"bmc-recovery script not found: {script}")
    if not _HOSTNAME_RE.match(node):
        raise ValueError(f"invalid node hostname: {node!r}")
    if not _IFACE_NAME_RE.match(iface):
        raise ValueError(f"invalid interface name: {iface!r}")
    timeout = max(30, _env_int("RACK_BMC_RECOVERY_TIMEOUT", 120))
    dry = os.environ.get("RACK_BMC_RECOVERY_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    cmd = ["bash", str(script), node, iface]
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    if dry:
        return {
            "returncode": 0,
            "stdout": f"dry-run (RACK_BMC_RECOVERY_DRY_RUN)\n{cmd_display}",
            "stderr": "",
            "command_display": cmd_display,
            "node": node,
            "iface": iface,
        }
    try:
        proc = host_exec.run_on_host(cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError("bash executable not found") from None
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "node": node,
            "iface": iface,
        }
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
        "node": node,
        "iface": iface,
    }


def _prepare_bmc_recovery(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate the request body; return ({nodes, iface}, None) or (None, (response, code))."""
    if os.environ.get("RACK_BMC_RECOVERY_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "bmc recovery disabled (RACK_BMC_RECOVERY_DISABLED)"}), 403)
    specs_raw = body.get("specs")
    if not isinstance(specs_raw, list) or not specs_raw:
        return None, (jsonify({"ok": False, "error": 'send JSON {"specs": ["pNN-rMM-ct[id]", ...]}'}), 400)
    nodes: list[str] = []
    for raw in specs_raw:
        host = _spec_to_hostname(str(raw))
        if host is None:
            return None, (
                jsonify({"ok": False, "error": f"spec {raw!r} is not a single-node form pNN-rMM-ct[NN]"}),
                400,
            )
        if host not in nodes:
            nodes.append(host)
    iface_raw = body.get("iface")
    iface = "rf0" if iface_raw is None else str(iface_raw).strip()
    if not _IFACE_NAME_RE.match(iface):
        return None, (jsonify({"ok": False, "error": f"invalid interface name: {iface!r}"}), 400)
    return {"nodes": nodes, "iface": iface}, None


def _bmc_recovery_worker(job_id: str, nodes: list[str], iface: str) -> None:
    """Run bmc-recovery.sh against each selected node serially; aggregate into _reset_jobs."""

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

    results: list[dict] = []
    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        for node in nodes:
            try:
                res = run_bmc_recovery_one(node, iface)
            except (OSError, ValueError) as e:
                res = {
                    "returncode": -1,
                    "stdout": "",
                    "stderr": str(e),
                    "command_display": "",
                    "node": node,
                    "iface": iface,
                }
            results.append(res)
            _patch(results=list(results))
        ok_all = all(r.get("returncode") == 0 for r in results)
        _patch(
            phase="done" if ok_all else "error",
            ok=ok_all,
            results=results,
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=None if ok_all else "bmc-recovery.sh failed on one or more nodes (see results)",
        )
    except Exception as e:
        _logger.debug("_bmc_recovery_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            results=results,
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@bp.route("/api/bmc-recovery-async", methods=["POST"])
@action("rack.bmc:recover", variant="async")
def api_bmc_recovery_async():
    """Start BMC recovery in a background thread; poll GET /api/reset-job-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_bmc_recovery(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    nodes = prep["nodes"]
    iface = prep["iface"]
    assert isinstance(nodes, list) and isinstance(iface, str)
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "bmc",
            "operator": _operator_from_request(),
            "rack": rk,
            "nodes": list(nodes),
            "iface": iface,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_bmc_recovery_worker,
        args=(job_id, list(nodes), iface),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "bmc", "rack": rk})


def run_mac_change(
    node: str,
    node_mac: str | None,
    ifspecs: list[tuple[str, str]],
    dry_run: bool,
) -> dict:
    """Invoke macswap in change mode through ``bash -lc 'module load python3 && macswap …'``."""
    script = _macswap_script_path()
    if not _script_exists(script):
        raise OSError(f"macswap script not found: {script}")
    if not _HOSTNAME_RE.match(node):
        raise ValueError(f"invalid node hostname: {node!r}")
    if node_mac is not None and not _MAC_RE.match(node_mac):
        raise ValueError(f"invalid node MAC: {node_mac!r}")
    for name, mac in ifspecs:
        if not _IFACE_NAME_RE.match(name):
            raise ValueError(f"invalid interface name: {name!r}")
        if not _MAC_RE.match(mac):
            raise ValueError(f"invalid MAC for interface {name!r}: {mac!r}")
    timeout = max(30, _env_int("RACK_MAC_CHANGE_TIMEOUT", 600))
    dry_env = os.environ.get("RACK_MAC_CHANGE_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    inner_argv: list[str] = ["python3", str(script), node]
    if node_mac:
        inner_argv += ["--node-mac", node_mac]
    for name, mac in ifspecs:
        inner_argv += ["--interface", f"{name}={mac}"]
    if dry_run or dry_env:
        inner_argv.append("--dry-run")
    module_name = _macswap_python_module()
    inner_cmd = f"module load {shlex.quote(module_name)} && {shlex.join(inner_argv)}"
    cmd = ["bash", "-lc", inner_cmd]
    cmd_display = " ".join(shlex.quote(c) for c in cmd)
    try:
        proc = host_exec.run_on_host(cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError("bash executable not found") from None
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
            "node": node,
            "dry_run": bool(dry_run or dry_env),
        }
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
        "node": node,
        "dry_run": bool(dry_run or dry_env),
    }


def _prepare_mac_change(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Validate the request body; return ({node, node_mac, ifspecs, dry_run}, None) or (None, err)."""
    if os.environ.get("RACK_MAC_CHANGE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "mac change disabled (RACK_MAC_CHANGE_DISABLED)"}), 403)
    node_raw = body.get("node")
    if not isinstance(node_raw, str) or not node_raw.strip():
        return None, (jsonify({"ok": False, "error": '"node" is required (single hostname)'}), 400)
    node = node_raw.strip().lower()
    if not _HOSTNAME_RE.match(node):
        return None, (jsonify({"ok": False, "error": f"invalid node hostname: {node!r}"}), 400)

    node_mac_raw = body.get("node_mac")
    node_mac: str | None
    if node_mac_raw is None or (isinstance(node_mac_raw, str) and not node_mac_raw.strip()):
        node_mac = None
    elif not isinstance(node_mac_raw, str):
        return None, (jsonify({"ok": False, "error": '"node_mac" must be a string'}), 400)
    elif not _MAC_RE.match(node_mac_raw.strip()):
        return None, (jsonify({"ok": False, "error": f"invalid node MAC: {node_mac_raw!r}"}), 400)
    else:
        node_mac = node_mac_raw.strip().lower()

    ifaces_raw = body.get("interfaces") or []
    if not isinstance(ifaces_raw, list):
        return None, (jsonify({"ok": False, "error": '"interfaces" must be an array of {name, mac} objects'}), 400)
    ifspecs: list[tuple[str, str]] = []
    for idx, row in enumerate(ifaces_raw):
        if not isinstance(row, dict):
            return None, (jsonify({"ok": False, "error": f"interfaces[{idx}] must be an object"}), 400)
        name_raw = row.get("name")
        mac_raw = row.get("mac")
        if (name_raw is None or not str(name_raw).strip()) and (mac_raw is None or not str(mac_raw).strip()):
            continue
        if not isinstance(name_raw, str) or not _IFACE_NAME_RE.match(name_raw.strip()):
            return None, (jsonify({"ok": False, "error": f'interfaces[{idx}].name is missing or invalid'}), 400)
        if not isinstance(mac_raw, str) or not _MAC_RE.match(mac_raw.strip()):
            return None, (jsonify({"ok": False, "error": f'interfaces[{idx}].mac is missing or invalid'}), 400)
        ifspecs.append((name_raw.strip(), mac_raw.strip().lower()))

    if node_mac is None and not ifspecs:
        return None, (
            jsonify({"ok": False, "error": "provide --node-mac or at least one interface"}),
            400,
        )

    dry_raw = body.get("dry_run")
    dry_run = bool(dry_raw) if dry_raw is not None else False

    return {"node": node, "node_mac": node_mac, "ifspecs": ifspecs, "dry_run": dry_run}, None


def _mac_change_worker(
    job_id: str,
    node: str,
    node_mac: str | None,
    ifspecs: list[tuple[str, str]],
    dry_run: bool,
) -> None:
    """Run macswap once (single node) and aggregate into _reset_jobs."""

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
        try:
            result = run_mac_change(node, node_mac, ifspecs, dry_run)
        except (OSError, ValueError) as e:
            result = {
                "returncode": -1,
                "stdout": "",
                "stderr": str(e),
                "command_display": "",
                "node": node,
                "dry_run": dry_run,
            }
        ok = result.get("returncode") == 0
        _patch(
            phase="done" if ok else "error",
            ok=ok,
            results=[result],
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=None if ok else "macswap exited non-zero (see stdout/stderr)",
        )
    except Exception as e:
        _logger.debug("_mac_change_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            ok=False,
            results=[],
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(e),
        )
    _reset_job_purge_later(job_id)


@bp.route("/api/mac-change-async", methods=["POST"])
@action("rack.bmc:set_mac", variant="async")
def api_mac_change_async():
    """Start a MAC change in a background thread; poll GET /api/reset-job-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_mac_change(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    node = str(prep["node"])
    node_mac = prep["node_mac"]
    assert node_mac is None or isinstance(node_mac, str)
    ifspecs = prep["ifspecs"]
    assert isinstance(ifspecs, list)
    dry_run = bool(prep["dry_run"])
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "mac",
            "operator": _operator_from_request(),
            "rack": rk,
            "node": node,
            "node_mac": node_mac,
            "interfaces": [{"name": n, "mac": m} for n, m in ifspecs],
            "dry_run": dry_run,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_mac_change_worker,
        args=(job_id, node, node_mac, list(ifspecs), dry_run),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "mac", "rack": rk})


def _prepare_services_restart(body: dict) -> tuple[dict[str, object] | None, tuple[object, int] | None]:
    """Same JSON as GPU reset: merged tray specs → one pdsh -w pattern."""
    if os.environ.get("RACK_SERVICES_RESTART_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "services restart disabled (RACK_SERVICES_RESTART_DISABLED)"}),
            403,
        )
    specs_raw = body.get("specs")
    if not isinstance(specs_raw, list) or not specs_raw:
        return None, (jsonify({"ok": False, "error": 'send JSON {"specs": ["pNN-rMM-ct[id]", ...]}'}), 400)
    try:
        specs = merge_ct_individuals_for_power([str(x) for x in specs_raw])
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    if not specs:
        return None, (jsonify({"ok": False, "error": "no valid tray specs after merge"}), 400)
    pdsh_w = ",".join(specs)
    return {"pdsh_w": pdsh_w}, None


@bp.route("/api/services-restart", methods=["POST"])
@action("rack.services:restart")
def api_services_restart():
    """POST JSON {\"specs\": [\"pNN-rMM-ct[id]\", …]} — pdsh mount + systemd restarts on selected hosts."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_services_restart(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    pdsh_w = str(prep["pdsh_w"])
    rk_for_lock = (body.get("rack") or "").strip()
    try:
        with _track_sync_op(rk_for_lock, "services", _operator_from_request()):
            result = run_services_restart(pdsh_w)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    ok = result.get("returncode") == 0
    payload: dict = {"ok": ok, **result}
    if not ok:
        payload["error"] = "services restart (pdsh) exited non-zero (see stdout/stderr)"
    return jsonify(payload), (200 if ok else 502)


@bp.route("/api/services-restart-async", methods=["POST"])
@action("rack.services:restart", variant="async")
def api_services_restart_async():
    """Start services restart in a background thread; poll GET /api/reset-job-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepare_services_restart(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    pdsh_w = str(prep["pdsh_w"])
    try:
        rk = _normalize_rack_id((body.get("rack") or "").strip())
    except ValueError:
        rk = ""
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "services",
            "operator": _operator_from_request(),
            "rack": rk,
            "phase": "starting",
            "created_at": created,
        }
    threading.Thread(
        target=_services_restart_worker,
        args=(job_id, pdsh_w),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "services", "rack": rk})


@bp.route("/api/node-reboot-async", methods=["POST"])
@action("rack.power:reboot", variant="async")
def api_node_reboot_async():
    """POST JSON {\"specs\": [\"host\", …]} — reboot nodes from the node itself; poll
    GET /api/reset-job-status?job_id=…

    For nodes Slurm cannot reboot (INVALID, DOWN); a node Slurm manages is better rebooted with
    ``/api/scheduler-reboot``, which waits for its jobs. Nodes running a job are skipped.
    """
    if os.environ.get("RACK_NODE_REBOOT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "node reboot disabled (RACK_NODE_REBOOT_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    specs_raw = body.get("specs")
    if not isinstance(specs_raw, list) or not specs_raw or not all(isinstance(x, str) for x in specs_raw):
        return jsonify({"ok": False, "error": 'send JSON {"specs": ["host", ...]}'}), 400
    hosts = [x.strip() for x in specs_raw if x.strip()]
    if not hosts or not all(_HOSTNAME_RE.fullmatch(h) for h in hosts):
        return jsonify({"ok": False, "error": "specs must be node hostnames"}), 400
    pdsh_w = ",".join(hosts)
    job_id = str(uuid.uuid4())
    with _reset_jobs_lock:
        _reset_jobs[job_id] = {
            "job_id": job_id,
            "kind": "node-reboot",
            "operator": _operator_from_request(),
            "rack": "",
            "phase": "starting",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    threading.Thread(target=_node_reboot_worker, args=(job_id, pdsh_w), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "kind": "node-reboot", "nodes": hosts})


@bp.route("/api/rack-uptime", methods=["POST"])
@action("rack:uptime")
def api_rack_uptime():
    """POST JSON ``{"rack": "pNN-rMM"}`` — run ``pdsh -w pNN-rMM-ct[RANGE] uptime``; returns table rows + raw output."""
    if os.environ.get("RACK_UPTIME_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "rack uptime disabled (RACK_UPTIME_DISABLED)"}), 403
    body = request.get_json(silent=True) or {}
    rack_raw = body.get("rack")
    if not isinstance(rack_raw, str) or not rack_raw.strip():
        return jsonify({"ok": False, "error": "rack is required"}), 400
    try:
        with _track_sync_op(rack_raw, "uptime", _operator_from_request()):
            result = run_rack_uptime_pdsh(rack_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(result)
