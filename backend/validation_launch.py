# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Launching a validation run: build the command, vet the nodes, drive the worker thread.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The launch side of the validation family -- single-rack, cross-rack NEMO, Llama, and the audited
presets. ``backend-api.mdc`` names this extraction as the prerequisite for moving the validation and
rack-action routes out of ``app.py``, because the routes are thin and everything that matters is here:
what command gets built, which nodes it is allowed to touch, and what happens while it runs.

Three responsibilities, deliberately together because a launch is not correct unless all three hold:

**Vetting.** ``_validation_node_eligibility`` decides which nodes a run may use. A launch against a
drained or foreign node fails minutes later inside Slurm with a message that looks like a cluster
fault, so it is refused up front instead.

**Command construction.** ``_prepare_validation_preset`` turns a preset id plus an exact node list into
the command that will run, with the image, version pins and step counts the preset owns. The browser
chooses a preset and targets; it never supplies a command. That asymmetry is the security property --
see ``bin/scripts/run-validation-preset.sh``, which fails closed if the node groups are not a clean
partition of the reviewed selection.

**The worker threads.** Each ``*_worker`` runs its command on the execution host and mirrors phase and
output into the in-memory registry AND the durable activity record. They are threads rather than a
queue because the process that starts a run must survive it: the registry is ephemeral, the activity
record is not, so a restart loses the live view but never the audit trail.

``merge_ct_individuals_for_power`` folds individual tray tokens into a rack range for the power path,
which needs one target expression rather than N.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import host_exec
import validation_activities
import validation_presets
from activity_hydration import _update_validation_activity
from config_env import _eget, _eget_int, _env_int, _job_env_int, _subprocess_env
from core import rpath as _rpath
from flask import jsonify
from job_registry import (
    _llama_job_purge_later,
    _llama_jobs,
    _llama_jobs_lock,
    _stress_job_purge_later,
    _stress_jobs,
    _stress_jobs_lock,
    _validation_job_purge_later,
    _validation_jobs,
    _validation_jobs_lock,
    _xrack_job_purge_later,
    _xrack_jobs,
    _xrack_jobs_lock,
)
from naming import (
    _ct_power_single_ct_re,
    _ct_power_tray_full_re,
    _normalize_rack_id,
    _nvis_naming,
)
from node_targets import (
    _scontrol_hostnames_preserving_case,
)
from paths import (
    _resolve_log_dir,
    _script_exists,
    _scripts_dir,
)
from platforms import get_platform as _get_platform
from results_ingest import _lake_push_job
from slurm_query import (
    _all_slurm_nodenames,
    _dedupe_hostnames_preserve_spelling,
    _rack_node_hostnames_from_inventory,
)

# Flask is imported for ``jsonify`` only: _prepare_validation_preset validates the browser's request
# and answers 400 itself, so the refusal and the reason travel together. Same shape as web_auth.py --
# not an endpoint module, but it does build one kind of response.
_logger = logging.getLogger(__name__)


def _validation_worker(
    job_id: str,
    cmd: list[str],
    cwd: str,
    timeout: int,
    display_cmd: str,
    *,
    env_updates: dict[str, str] | None = None,
) -> None:
    """Run validation bash in a background thread; update _validation_jobs[job_id].

    ``env_updates`` carries per-launch env knobs (NCCL debug + custom vars) from the launch
    modal; they are merged into the subprocess env and forwarded to the remote over SSH.
    """

    def _patch(**kwargs: object) -> None:
        finished = None
        with _validation_jobs_lock:
            row = _validation_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        _update_validation_activity(job_id, **kwargs)
        if finished is not None:
            _lake_push_job(job_id, finished)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        # NeMo/validation launchers run on the privileged "script" target (root@slogin):
        # they pdsh-rsync the .sqsh into root-owned /raid/data on compute nodes and submit
        # via sbatch — neither possible as the unprivileged control-plane user on the head.
        hp = host_exec.popen_on_host(cmd, cwd=cwd, env=_subprocess_env(env_updates),
                                     profile="script", env_extra=env_updates)
        try:
            out, err = hp.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            hp.kill()
            try:
                hp.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _patch(
                phase="timeout",
                returncode=-1,
                stdout="",
                stderr=f"timeout after {timeout}s",
                ok=False,
                error="validation script timed out",
                finished_at=datetime.now(timezone.utc).isoformat(),
                command_display=display_cmd,
            )
            _validation_job_purge_later(job_id)
            return
        rc = hp.returncode if hp.returncode is not None else -1
        ok = rc == 0
        _patch(
            phase="done" if ok else "error",
            returncode=rc,
            stdout=(out or "").strip(),
            stderr=(err or "").strip(),
            ok=ok,
            error=None if ok else "validation script exited non-zero",
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    except Exception as e:
        _logger.debug("_validation_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            returncode=-1,
            stdout="",
            stderr="",
            ok=False,
            error=str(e),
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    _validation_job_purge_later(job_id)


def _stress_worker(
    job_id: str,
    cmd: list[str],
    cwd: str,
    timeout: int,
    display_cmd: str,
    *,
    error_label: str = "mnubergemm stress script",
    env_updates: dict[str, str] | None = None,
    profile: str = "control",
    persist: bool = False,
) -> None:
    """Run a stress-style bash driver (mnubergemm, nvloom, …) in a background thread; update _stress_jobs[job_id].

    When *persist* is set, phase transitions are also mirrored into the durable
    ``validation_activities`` store so the launch survives restarts / other sessions.
    """

    def _patch(**kwargs: object) -> None:
        with _stress_jobs_lock:
            row = _stress_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
        if persist:
            _update_validation_activity(job_id, **kwargs)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        hp = host_exec.popen_on_host(cmd, cwd=cwd, env=_subprocess_env(env_updates),
                                     profile=profile, env_extra=env_updates)
        try:
            out, err = hp.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            hp.kill()
            try:
                hp.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _patch(
                phase="timeout",
                returncode=-1,
                stdout="",
                stderr=f"timeout after {timeout}s",
                ok=False,
                error=f"{error_label} timed out",
                finished_at=datetime.now(timezone.utc).isoformat(),
                command_display=display_cmd,
            )
            _stress_job_purge_later(job_id)
            return
        rc = hp.returncode if hp.returncode is not None else -1
        ok = rc == 0
        _patch(
            phase="done" if ok else "error",
            returncode=rc,
            stdout=(out or "").strip(),
            stderr=(err or "").strip(),
            ok=ok,
            error=None if ok else f"{error_label} exited non-zero",
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    except Exception as e:
        _logger.debug("_stress_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            returncode=-1,
            stdout="",
            stderr="",
            ok=False,
            error=str(e),
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    _stress_job_purge_later(job_id)


def _xrack_worker(
    job_id: str,
    cmd: list[str],
    cwd: str,
    timeout: int,
    display_cmd: str,
    *,
    env_updates: dict[str, str] | None = None,
) -> None:
    """Run run-nemo-xrack.sh in a background thread; update _xrack_jobs[job_id].

    Phase transitions are mirrored into the durable ``validation_activities`` store so the launch
    is visible across sessions / restarts. ``env_updates`` carries per-launch env knobs
    (NCCL debug + custom vars) from the launch modal, merged into the subprocess env.
    """

    def _patch(**kwargs: object) -> None:
        with _xrack_jobs_lock:
            row = _xrack_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
        _update_validation_activity(job_id, **kwargs)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        # NeMo/validation launchers run on the privileged "script" target (root@slogin):
        # they pdsh-rsync the .sqsh into root-owned /raid/data on compute nodes and submit
        # via sbatch — neither possible as the unprivileged control-plane user on the head.
        hp = host_exec.popen_on_host(cmd, cwd=cwd, env=_subprocess_env(env_updates),
                                     profile="script", env_extra=env_updates)
        try:
            out, err = hp.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            hp.kill()
            try:
                hp.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _patch(
                phase="timeout",
                returncode=-1,
                stdout="",
                stderr=f"timeout after {timeout}s",
                ok=False,
                error="run-nemo-xrack.sh timed out",
                finished_at=datetime.now(timezone.utc).isoformat(),
                command_display=display_cmd,
            )
            _xrack_job_purge_later(job_id)
            return
        rc = hp.returncode if hp.returncode is not None else -1
        ok = rc == 0
        _patch(
            phase="done" if ok else "error",
            returncode=rc,
            stdout=(out or "").strip(),
            stderr=(err or "").strip(),
            ok=ok,
            error=None if ok else "run-nemo-xrack.sh exited non-zero",
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    except Exception as e:
        _logger.debug("_xrack_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            returncode=-1,
            stdout="",
            stderr="",
            ok=False,
            error=str(e),
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    _xrack_job_purge_later(job_id)


def _llama_worker(job_id: str, cmd: list[str], cwd: str, timeout: int, display_cmd: str) -> None:
    """Run ``run-llama.sh`` (via bash -lc module load slurm) in a background thread."""

    def _patch(**kwargs: object) -> None:
        with _llama_jobs_lock:
            row = _llama_jobs.get(job_id)
            if row is not None:
                row.update(kwargs)
        _update_validation_activity(job_id, **kwargs)

    try:
        _patch(
            phase="running",
            started_running_at=datetime.now(timezone.utc).isoformat(),
        )
        # NeMo/validation launchers run on the privileged "script" target (root@slogin):
        # they pdsh-rsync the .sqsh into root-owned /raid/data on compute nodes and submit
        # via sbatch — neither possible as the unprivileged control-plane user on the head.
        hp = host_exec.popen_on_host(cmd, cwd=cwd, env=_subprocess_env(), profile="script")
        try:
            out, err = hp.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            hp.kill()
            try:
                hp.communicate(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass
            _patch(
                phase="timeout",
                returncode=-1,
                stdout="",
                stderr=f"timeout after {timeout}s",
                ok=False,
                error="run-llama.sh timed out",
                finished_at=datetime.now(timezone.utc).isoformat(),
                command_display=display_cmd,
            )
            _llama_job_purge_later(job_id)
            return
        rc = hp.returncode if hp.returncode is not None else -1
        ok = rc == 0
        _patch(
            phase="done" if ok else "error",
            returncode=rc,
            stdout=(out or "").strip(),
            stderr=(err or "").strip(),
            ok=ok,
            error=None if ok else "run-llama.sh exited non-zero",
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    except Exception as e:
        _logger.debug("_llama_worker: ignoring error", exc_info=True)
        _patch(
            phase="error",
            returncode=-1,
            stdout="",
            stderr="",
            ok=False,
            error=str(e),
            finished_at=datetime.now(timezone.utc).isoformat(),
            command_display=display_cmd,
        )
    _llama_job_purge_later(job_id)


def _host_job_cwd(log_dir: Path) -> str:
    """Working-directory string for a launched job's ``cd`` (popen_on_host).

    In SSH mode *log_dir* is a **remote** path, so ``Path.resolve()`` must NOT run: on macOS
    it rewrites ``/home/...`` to ``/System/Volumes/Data/home/...`` (firmlink), which does not
    exist on the head node and makes the remote ``cd`` fail before the script runs. Resolve
    only when executing locally.
    """
    return str(_rpath(log_dir))


def merge_ct_individuals_for_power(nodes: list[str]) -> list[str]:
    """
    Merge per-node tokens (``<rack><ct_joiner>[dd]`` with one tray id) into tray specs for cmsh power.
    """
    naming = _nvis_naming()
    single_re = _ct_power_single_ct_re()
    full_re = _ct_power_tray_full_re()
    by_rack: dict[str, list[int]] = {}
    for raw in nodes:
        if not isinstance(raw, str):
            raise ValueError("each entry in specs must be a string")  # noqa: TRY004 - routes catch ValueError to answer 400; TypeError would 500
        token = raw.strip()
        m = single_re.match(token)
        if not m:
            raise ValueError(
                f"invalid node token {raw!r} — expected rack{ naming.ct_joiner }[number] with a single tray id"
            )
        rack_tok = m.group(1).strip()
        rr = naming.canonical_rack_id(rack_tok) or rack_tok
        li = m.lastindex or 0
        if li < 2:
            raise ValueError(
                f"invalid node token {raw!r} — expected rack{ naming.ct_joiner }[number] with a single tray id"
            )
        n = int(m.group(li), 10)
        by_rack.setdefault(rr, []).append(n)
    out: list[str] = []
    for rr in sorted(by_rack.keys(), key=str.lower):
        inner = naming.condense_tray_range_inner(by_rack[rr])
        if inner:
            tok = naming.ct_blob(rr, inner)
            if not full_re.match(tok):
                raise ValueError(f"invalid merged tray spec {tok!r}")
            out.append(tok)
    return out


def _multirack_campaign_manifest() -> dict[str, object]:
    """Load the configured exact-node campaign manifest from the scripts host."""
    campaign_id = (_eget("RACK_MULTIRACK_CAMPAIGN_ID") or "").strip()
    raw_path = (_eget("RACK_MULTIRACK_CAMPAIGN_MANIFEST") or "").strip()
    if not campaign_id or not raw_path:
        return {"ok": True, "active": False, "id": campaign_id, "rack_nodes": {}}
    if raw_path.startswith("local:"):
        path = Path(raw_path[6:])
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"cannot read campaign manifest {path}: {exc}") from exc
    else:
        text, meta = host_exec.read_text(raw_path, whole_bytes=4 * 1024 * 1024, role="scripts")
        if not bool(meta.get("ok", True)):
            raise RuntimeError(f"cannot read campaign manifest {raw_path}: {meta.get('error') or 'read failed'}")
    nodes = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not nodes:
        raise RuntimeError("campaign manifest is empty")
    if len({node.casefold() for node in nodes}) != len(nodes):
        raise RuntimeError("campaign manifest contains duplicate nodes")
    rack_nodes: dict[str, list[str]] = {}
    for node in nodes:
        parsed = _nvis_naming().sinfo_rack_from_host(node)
        if parsed is None or parsed[1] is None:
            raise RuntimeError(f"campaign manifest contains an invalid compute node: {node}")
        rack = _normalize_rack_id(parsed[0])
        rack_nodes.setdefault(rack, []).append(node)
    expected = max(1, _eget_int("RACK_MULTIRACK_CAMPAIGN_NODES_PER_RACK", 16))
    wrong = {rack: len(hosts) for rack, hosts in rack_nodes.items() if len(hosts) != expected}
    if wrong:
        details = ", ".join(f"{rack}={count}" for rack, count in sorted(wrong.items()))
        raise RuntimeError(f"campaign manifest requires exactly {expected} nodes per rack; got {details}")
    return {
        "ok": True,
        "active": True,
        "id": campaign_id,
        "manifest": raw_path,
        "nodes_per_rack": expected,
        "rack_nodes": rack_nodes,
    }


def _multirack_campaign_snapshot() -> dict[str, object]:
    manifest = _multirack_campaign_manifest()
    if not manifest.get("active"):
        return manifest
    rack_nodes = manifest["rack_nodes"]
    assert isinstance(rack_nodes, dict)
    proc = host_exec.run_slurm_on_host(
        _get_platform().sinfo_cmd('-N -h -o "%N|%T"'),
        timeout=max(20, _env_int("RACK_SINFO_NODES_TIMEOUT", 60)),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "sinfo failed").strip())
    states: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        node, sep, state = line.partition("|")
        if sep and node.strip():
            states[node.strip().casefold()] = state.strip().lower().rstrip("*")
    allowed = {"idle", "alloc", "allocated"}
    rows: list[dict[str, object]] = []
    for rack, raw_nodes in sorted(rack_nodes.items()):
        hosts = list(raw_nodes) if isinstance(raw_nodes, list) else []
        unavailable = []
        for node in hosts:
            state = states.get(node.casefold(), "missing")
            if state not in allowed:
                unavailable.append({"node": node, "state": state})
        eligible = not unavailable
        reason = "" if eligible else "exact manifest nodes unavailable (" + ", ".join(
            f"{state}={sum(1 for row in unavailable if row['state'] == state)}"
            for state in sorted({str(row["state"]) for row in unavailable})
        ) + ")"
        rows.append({
            "rack": rack,
            "nodes": hosts,
            "node_count": len(hosts),
            "eligible": eligible,
            "reason": reason,
            "unavailable_nodes": unavailable,
        })
    return {
        **{key: value for key, value in manifest.items() if key != "rack_nodes"},
        "rack_count": len(rows),
        "node_count": sum(int(row["node_count"]) for row in rows),
        "eligible_count": sum(1 for row in rows if row["eligible"]),
        "racks": rows,
    }


def _validation_node_eligibility(nodes: list[str]) -> tuple[dict[str, str], list[str]]:
    """Return live Slurm states and fail-closed reasons for an exact target set."""
    spec = ",".join(nodes)
    proc = host_exec.run_slurm_on_host(
        _get_platform().sinfo_cmd(
            f"-N -h --nodes={shlex.quote(spec)} -o '%N|%T'"
        ),
        timeout=max(20, _env_int("RACK_SINFO_NODES_TIMEOUT", 60)),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "unable to query exact node states").strip())
    states: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        node, sep, state = line.partition("|")
        if sep and node.strip():
            states[node.strip().casefold()] = state.strip().lower().rstrip("*")
    excluded = {
        token.strip().casefold()
        for token in (_eget("RACK_VALIDATION_EXCLUDED_NODES") or "").split(",")
        if token.strip()
    }
    reasons: list[str] = []
    for node in nodes:
        state = states.get(node.casefold(), "missing")
        if node.casefold() in excluded:
            reasons.append(f"{node}=known-bad")
        elif state != "idle":
            reasons.append(f"{node}={state}")
    return ({node: states.get(node.casefold(), "missing") for node in nodes}, reasons)


def _prepare_validation_preset(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Resolve an allowlisted preset into an exact-node, shell-free launch plan."""
    try:
        preset = validation_presets.resolve_preset(body.get("preset_id", body.get("validation")))
        options = validation_presets.normalize_options(body, preset)
        log_raw = body.get("log_dir")
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as exc:
        return None, (jsonify({"ok": False, "error": str(exc)}), 400)

    scope = str(options["scope"])
    racks_raw = body.get("racks")
    if racks_raw is None and body.get("rack"):
        racks_raw = [body.get("rack")]
    if racks_raw is not None and not isinstance(racks_raw, list):
        return None, (jsonify({"ok": False, "error": "racks must be an array"}), 400)
    racks: list[str] = []
    try:
        for raw in racks_raw or []:
            rack = _normalize_rack_id(str(raw).strip())
            if rack not in racks:
                racks.append(rack)
    except ValueError as exc:
        return None, (jsonify({"ok": False, "error": str(exc)}), 400)

    nodes_raw = body.get("nodes")
    if nodes_raw is not None and (not isinstance(nodes_raw, list) or not nodes_raw):
        return None, (jsonify({"ok": False, "error": "nodes must be a non-empty array"}), 400)
    nodes: list[str] = []
    if isinstance(nodes_raw, list):
        for raw in nodes_raw:
            node = str(raw).strip()
            if not node or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", node):
                return None, (jsonify({"ok": False, "error": f"invalid node name: {raw!r}"}), 400)
            if node.casefold() not in {item.casefold() for item in nodes}:
                nodes.append(node)
    target = body.get("target")
    if not nodes and isinstance(target, str) and target.strip():
        nodes = _scontrol_hostnames_preserving_case(target)
        if not nodes:
            return None, (jsonify({"ok": False, "error": "target did not resolve to any Slurm nodes"}), 400)

    campaign_id = ""
    rack_node_groups: list[dict[str, object]] = []
    if not nodes and racks:
        try:
            campaign = _multirack_campaign_snapshot()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            return None, (jsonify({"ok": False, "error": f"exact-node campaign unavailable: {exc}"}), 503)
        campaign_id = str(campaign.get("id") or "")
        requested_campaign = str(body.get("campaign") or "").strip()
        if requested_campaign and requested_campaign != campaign_id:
            return None, (jsonify({"ok": False, "error": "stale or unknown exact-node campaign id"}), 409)
        if campaign.get("active"):
            indexed = {str(row.get("rack") or ""): row for row in campaign.get("racks", []) if isinstance(row, dict)}
            for rack in racks:
                row = indexed.get(rack)
                if row is None:
                    return None, (jsonify({"ok": False, "error": f"rack {rack} is outside exact-node campaign {campaign_id}"}), 409)
                if not row.get("eligible"):
                    return None, (jsonify({"ok": False, "error": f"rack {rack}: {row.get('reason') or 'not eligible'}"}), 409)
                group_nodes = [str(node) for node in row.get("nodes", []) if str(node).strip()]
                rack_node_groups.append({"rack": rack, "nodes": group_nodes})
                nodes.extend(group_nodes)
        else:
            for rack in racks:
                resolved = _rack_node_hostnames_from_inventory(rack)
                if not resolved:
                    return None, (jsonify({"ok": False, "error": f"no Slurm inventory nodes found for rack {rack}"}), 409)
                rack_node_groups.append({"rack": rack, "nodes": list(resolved)})
                nodes.extend(resolved)

    nodes = _dedupe_hostnames_preserve_spelling(nodes)
    if not nodes:
        return None, (jsonify({"ok": False, "error": "explicit nodes, target, or racks are required"}), 400)
    inventory = _all_slurm_nodenames()
    if inventory:
        known = {node.casefold() for node in inventory}
        unknown = [node for node in nodes if node.casefold() not in known]
        if unknown:
            return None, (jsonify({"ok": False, "error": f"nodes not in current Slurm inventory: {', '.join(unknown)}"}), 409)
    if scope == "rack" and len(racks) != 1:
        return None, (jsonify({"ok": False, "error": "rack scope requires exactly one rack"}), 400)
    if scope == "multirack" and len(racks) < 2 and not isinstance(nodes_raw, list):
        return None, (jsonify({"ok": False, "error": "multirack scope requires at least two racks or an explicit node list"}), 400)
    if preset.id == "mnubergemm-26.03-v1.17" and scope == "multirack":
        # MNUberGEMM is an NVLink-domain test: its audited sbatch disables IB and
        # enables MNNVL. A single Slurm job spanning physical racks would combine
        # separate NVLink domains and can hang. Preserve one exact target group per
        # operator-selected rack so the dispatcher fans out independent jobs.
        if not rack_node_groups and racks:
            selected = {node.casefold(): node for node in nodes}
            for rack in racks:
                inventory_nodes = _rack_node_hostnames_from_inventory(rack)
                group_nodes = [selected[node.casefold()] for node in inventory_nodes if node.casefold() in selected]
                if group_nodes:
                    rack_node_groups.append({"rack": rack, "nodes": group_nodes})
        grouped_nodes = [
            str(node)
            for group in rack_node_groups
            for node in (group.get("nodes") or [])
            if str(node).strip()
        ]
        if len(rack_node_groups) != len(racks) or len(rack_node_groups) < 2:
            return None, (jsonify({
                "ok": False,
                "error": "MNUberGEMM multirack requires one resolvable exact-node group per selected rack",
            }), 409)
        if len({node.casefold() for node in grouped_nodes}) != len(grouped_nodes):
            return None, (jsonify({"ok": False, "error": "MNUberGEMM rack node groups overlap"}), 409)
        if {node.casefold() for node in grouped_nodes} != {node.casefold() for node in nodes}:
            return None, (jsonify({
                "ok": False,
                "error": "MNUberGEMM rack groups do not exactly cover the reviewed node target",
            }), 409)
    if preset.max_nodes is not None and len(nodes) > preset.max_nodes:
        return None, (jsonify({"ok": False, "error": f"{preset.id} supports at most {preset.max_nodes} node(s); resolved {len(nodes)}"}), 400)
    if preset.exact_nodes is not None and len(nodes) != preset.exact_nodes:
        return None, (jsonify({"ok": False, "error": f"{preset.id} requires exactly {preset.exact_nodes} nodes/1024 GPUs; resolved {len(nodes)}"}), 409)
    try:
        node_states, ineligible = _validation_node_eligibility(nodes)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return None, (jsonify({"ok": False, "error": f"unable to verify exact-node scheduler eligibility: {exc}"}), 503)
    if ineligible:
        shown = ", ".join(ineligible[:12])
        suffix = f" (+{len(ineligible) - 12} more)" if len(ineligible) > 12 else ""
        return None, (jsonify({"ok": False, "error": f"exact target is not idle/eligible: {shown}{suffix}"}), 409)

    script = Path(_scripts_dir()) / "run-validation-preset.sh"
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"validation preset launcher not found: {script}"}), 500)
    node_spec = ",".join(nodes)
    cmd = ["bash", str(script), "--preset", preset.id, "--nodelist", node_spec,
           "--storage", str(options["storage"]), "--local-container-dir",
           str(options["local_container_dir"]), "--log-dir", str(log_dir)]
    if options["rsync_to_local"]:
        cmd.append("--rsync")
    if preset.id == "mnubergemm-26.03-v1.17" and scope == "multirack":
        for group in rack_node_groups:
            cmd += ["--group-nodelist", ",".join(str(node) for node in group["nodes"])]
    display_cmd = " ".join(shlex.quote(part) for part in cmd)
    image_path = preset.local_image if options["storage"] == "local" else preset.shared_image
    checks = ["exact Slurm node identity", "scheduler eligibility at allocation time"]
    if preset.shared_image:
        checks += ["allowlisted image byte size", "allowlisted image SHA256"]
    if preset.requires_infiniband:
        checks += ["/dev/infiniband and /sys/class/infiniband visibility"]
    if preset.id == "nccl-2.31.2":
        checks += ["selected CUDA runtime >= 13.3", "ncclGetVersion == 23102"]
    if preset.id == "exemplar-system-info":
        checks += ["DGXC v26.06.01 summary present", "nonfatal findings and warnings classified per node"]
    if preset.id == "mnubergemm-26.03-v1.17":
        checks += ["MNUberGEMM payload duration=3300 seconds", "Slurm TimeLimit=01:00:00"]
    summary = f"{preset.label}: {len(nodes)} exact node(s), storage={options['storage']}, rsync={str(options['rsync_to_local']).lower()}"
    plan = {
        "preset_id": preset.id,
        "validation": preset.kind,
        "summary": summary,
        "scope": scope,
        "nodes": nodes,
        "racks": racks,
        "target": node_spec,
        "node_count": len(nodes),
        "node_groups": rack_node_groups,
        "campaign": campaign_id,
        "storage": options["storage"],
        "rsync_to_local": options["rsync_to_local"],
        "local_container_dir": options["local_container_dir"],
        "container_profile": options["container_profile"],
        "cuda_version": options["cuda_version"],
        "nccl_version": options["nccl_version"],
        "max_steps": options["max_steps"],
        "max_runtime_minutes": options["max_runtime_minutes"],
        "requires_infiniband": options["requires_infiniband"],
        "artifact": ({"path": image_path, "source": preset.shared_image, "size": preset.image_size,
                      "sha256": preset.image_sha256} if preset.shared_image else None),
        "checks": checks,
        "node_states": node_states,
        "command_display": display_cmd,
        "cmd": cmd,
        "log_dir": str(log_dir),
        "timeout": max(120, _job_env_int("RACK_VALIDATION_PRESET_TIMEOUT", 28800)),
        "env_updates": {"NVIS_PREPARED_NODE_COUNT": str(len(nodes)),
                        "NVIS_PRESET_SCOPE_MULTIRACK": "1" if scope == "multirack" else "0"},
        "image_override_note": ("DGXC recipe v26.06.01 with audited NeMo 26.04.01 image override" if preset.id == "exemplar-system-info" else None),
    }
    hash_fields = {key: plan[key] for key in (
        "preset_id", "scope", "nodes", "node_groups", "racks", "campaign", "storage", "rsync_to_local",
        "local_container_dir", "container_profile", "cuda_version", "nccl_version", "max_steps",
        "max_runtime_minutes", "artifact", "log_dir",
    )}
    plan["plan_hash"] = hashlib.sha256(
        json.dumps(hash_fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return plan, None


def _validation_preset_status_payload(job_id: str):
    if not job_id:
        return jsonify({"ok": False, "error": "job_id is required"}), 400
    with _stress_jobs_lock:
        row = _stress_jobs.get(job_id)
        payload = dict(row) if row is not None else None
    if payload is None:
        payload = validation_activities.get_activity(job_id)
    if payload is None:
        return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
    payload["ok"] = True
    return jsonify(payload)
