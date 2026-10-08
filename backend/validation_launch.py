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
import os
import re
import secrets
import shlex
import subprocess
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import host_exec
import script_tree
import validation_activities
import validation_presets
from activity_hydration import _update_validation_activity
from catalog import context as catalog_context
from catalog import models
from catalog.store import TABLE_TOOLS, get_repo
from config_env import _eget, _eget_int, _env_int, _job_env_int, _subprocess_env
from core import is_valid_node_hostname
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
    Merge tray tokens into rack ranges while preserving exact compute hostnames.
    """
    naming = _nvis_naming()
    single_re = _ct_power_single_ct_re()
    full_re = _ct_power_tray_full_re()
    by_rack: dict[str, list[int]] = {}
    exact: list[str] = []
    for raw in nodes:
        if not isinstance(raw, str):
            raise ValueError("each entry in specs must be a string")  # noqa: TRY004 - routes catch ValueError to answer 400; TypeError would 500
        token = raw.strip()
        m = single_re.match(token)
        if not m:
            if not is_valid_node_hostname(token):
                raise ValueError(
                    f"invalid node token {raw!r} — expected a hostname or rack{ naming.ct_joiner }[number]"
                )
            if token not in exact:
                exact.append(token)
            continue
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
    return [*out, *exact]


def _campaign_from_catalog() -> dict[str, object] | None:
    """The active cluster's pinned roster, or None if it keeps none.

    Preferred over the environment manifest below, because this one travels with the
    project: it is visible to the cluster switcher, editable from the Projects screen, and
    reproducible on a second deployment. The manifest is a path on one host, and none of
    those are true of it.

    Which roster a site means is `RACK_MULTIRACK_CAMPAIGN_ID` when set, so switching
    between waves needs no code change; otherwise the cluster's only campaign, since
    having to name the single option would be ceremony.
    """
    try:
        _project, cluster, _platform = catalog_context.resolve()
    except Exception:  # noqa: BLE001 - an unreadable catalog is the caller's 503, not ours
        return None
    if cluster is None or not cluster.campaigns:
        return None

    wanted = (_eget("RACK_MULTIRACK_CAMPAIGN_ID") or "").strip()
    campaign = cluster.campaign(wanted) if wanted else (
        cluster.campaigns[0] if len(cluster.campaigns) == 1 else None
    )
    if campaign is None:
        return None

    problems = campaign.validate()
    if problems:
        # Refuse rather than run a roster we know is malformed: a duplicate node would
        # inflate the node count the acceptance record reports.
        raise RuntimeError(f"campaign {campaign.id!r} is not valid: {'; '.join(problems)}")

    rack_nodes = _group_nodes_by_rack(campaign.nodes)
    if campaign.nodes_per_rack:
        _require_nodes_per_rack(rack_nodes, campaign.nodes_per_rack)
    return {
        "ok": True,
        "active": True,
        "id": campaign.id,
        "label": campaign.label,
        # The version a launch must echo back. Derived from the roster, so it cannot
        # disagree with the nodes it describes.
        "version": campaign.fingerprint(),
        "source": "catalog",
        "nodes_per_rack": campaign.nodes_per_rack,
        "rack_nodes": rack_nodes,
    }


def _group_nodes_by_rack(nodes: list[str]) -> dict[str, list[str]]:
    """Map exact hostnames onto their racks, refusing anything that is not a compute node."""
    rack_nodes: dict[str, list[str]] = {}
    for node in nodes:
        parsed = _nvis_naming().sinfo_rack_from_host(node)
        if parsed is None or parsed[1] is None:
            raise RuntimeError(f"campaign contains an invalid compute node: {node}")
        rack_nodes.setdefault(_normalize_rack_id(parsed[0]), []).append(node)
    return rack_nodes


def _require_nodes_per_rack(rack_nodes: dict[str, list[str]], expected: int) -> None:
    wrong = {rack: len(hosts) for rack, hosts in rack_nodes.items() if len(hosts) != expected}
    if wrong:
        details = ", ".join(f"{rack}={count}" for rack, count in sorted(wrong.items()))
        raise RuntimeError(f"campaign requires exactly {expected} nodes per rack; got {details}")


def _multirack_campaign_manifest() -> dict[str, object]:
    """The exact-node campaign: the cluster record first, the environment manifest second.

    One resolver with an explicit order, rather than two callers each picking a source --
    that is the mistake `paths` and `workflow_engine` made with script directories, where
    two resolvers agreed until one learned something and then the API and a workflow step
    ran different copies of the same script. The winning source is reported so an operator
    can see which answered.
    """
    from_catalog = _campaign_from_catalog()
    if from_catalog is not None:
        return from_catalog

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
    rack_nodes = _group_nodes_by_rack(nodes)
    expected = max(1, _eget_int("RACK_MULTIRACK_CAMPAIGN_NODES_PER_RACK", 16))
    _require_nodes_per_rack(rack_nodes, expected)
    return {
        "ok": True,
        "active": True,
        "id": campaign_id,
        "manifest": raw_path,
        # A manifest gets a fingerprint for the same reason the catalog roster does, and
        # here it is load-bearing: the id is an environment variable and the roster is a
        # separate file, so editing the file changed the nodes while the id stayed put and
        # the staleness check passed. Two different node sets, one identifier, in the
        # audit trail. Deriving the version from the content closes that.
        "version": models.ClusterCampaign(id=campaign_id, nodes=nodes).fingerprint(),
        "source": "manifest",
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


def _incompatible_platform_error(label: str, **extra: Any) -> tuple[object, int]:
    """Describe an actual platform mismatch consistently across launch paths."""
    _project, _cluster, platform = catalog_context.resolve()
    platform_id = platform.id if platform is not None else ""
    platform_name = platform.name if platform is not None else "the active platform"
    return (
        jsonify({
            "ok": False,
            "error": f"{label} is not compatible with {platform_name}",
            "reason": "incompatible_platform",
            "platform_id": platform_id,
            **extra,
        }),
        409,
    )


def _catalog_tool_compatibility_error(tool_id: str) -> tuple[object, int] | None:
    """Distinguish an absent catalog tool from a platform mismatch."""
    raw = get_repo().get(TABLE_TOOLS, tool_id)
    if raw is None:
        return jsonify({
            "ok": False, "error": f"Unknown catalog tool {tool_id!r}",
            "reason": "unknown_tool", "tool_id": tool_id,
        }), 404
    if catalog_context.tool_for_cluster(tool_id) is not None:
        return None
    return _incompatible_platform_error(models.Tool.from_dict(raw).name, tool_id=tool_id)


def _validation_preset_compatibility_error(body: dict) -> tuple[object, int] | None:
    """Apply catalog compatibility to a code-owned validation recipe."""
    try:
        preset = validation_presets.resolve_preset(body.get("preset_id", body.get("validation")))
    except ValueError:
        return None
    for raw in catalog_context.tools_for_cluster():
        tool = models.Tool.from_dict(raw)
        if tool.launch.kind == "validation-preset" and tool.launch.preset_id == preset.id:
            return None
    bound_tools = (models.Tool.from_dict(raw) for raw in get_repo().list(TABLE_TOOLS))
    if not any(tool.launch.kind == "validation-preset" and tool.launch.preset_id == preset.id
               for tool in bound_tools):
        return jsonify({
            "ok": False, "error": f"No catalog tool is bound to {preset.label}",
            "reason": "missing_preset_tool", "preset_id": preset.id,
        }), 409
    return _incompatible_platform_error(preset.label, preset_id=preset.id)


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
        campaign_version = str(campaign.get("version") or "")
        requested_campaign = str(body.get("campaign") or "").strip()
        if requested_campaign and requested_campaign != campaign_id:
            return None, (jsonify({"ok": False, "error": "stale or unknown exact-node campaign id"}), 409)
        # The id alone was never enough. It is a label a human chose, so a roster edited
        # under a stable id passed this check and ran different hardware than the operator
        # reviewed -- recorded under the same name. The version is a digest of the roster,
        # so a caller holding a stale list cannot echo the current one.
        requested_version = str(body.get("campaign_version") or "").strip()
        if requested_version and campaign_version and requested_version != campaign_version:
            return None, (
                jsonify({
                    "ok": False,
                    "error": (
                        f"exact-node campaign {campaign_id!r} has changed since this page "
                        f"loaded (reviewed {requested_version}, current {campaign_version}). "
                        "Reload and confirm the roster before launching."
                    ),
                    "campaign": campaign_id,
                    "campaign_version": campaign_version,
                }),
                409,
            )
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

    script = script_tree.resolve_path("run-validation-preset.sh")
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
    # Re-rooted at this site's container mount. Only the directory moves -- the basename is
    # the reviewed artifact, and the size and SHA256 gates below still have to pass, so a
    # site can point at its own mirror and cannot substitute a different image.
    shared_image = validation_presets.resolve_shared_image(preset.shared_image)
    image_path = preset.local_image if options["storage"] == "local" else shared_image
    checks = ["exact Slurm node identity", "scheduler eligibility at allocation time"]
    if preset.shared_image:
        checks += ["allowlisted image byte size", "allowlisted image SHA256"]
    if preset.requires_infiniband:
        checks += ["/dev/infiniband and /sys/class/infiniband visibility"]
    if preset.id == "nccl-2.31.2":
        checks += ["selected CUDA runtime >= 13.0", "ncclGetVersion == 23007 (NCCL 2.30.7)"]
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
        # `source` is the resolved path so the reviewed plan shows the operator the file that
        # will actually be staged, not the shipped default they may not be using.
        "artifact": ({"path": image_path, "source": shared_image, "size": preset.image_size,
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
    plan["plan_hash"] = plan_digest({key: plan[key] for key in (
        "preset_id", "scope", "nodes", "node_groups", "racks", "campaign", "storage", "rsync_to_local",
        "local_container_dir", "container_profile", "cuda_version", "nccl_version", "max_steps",
        "max_runtime_minutes", "artifact", "log_dir",
    )})
    return plan, None


# ---------------------------------------------------------------------------------------------
# Reviewed-target binding (REQ-6)
# ---------------------------------------------------------------------------------------------
#
# The preset path has bound a reviewed target to its launch since it was written: `prepare` returns
# a digest, `launch` re-resolves and compares. A PLC requirements validation on 2026-08-24 found
# that binding covered ONE launch endpoint out of thirteen -- every legacy per-tool launch resolved
# its target at launch time and relied on a client-side confirmation dialog, which detects nothing
# when what the operator reviewed has since changed. REQ-6 held on one path and failed on twelve.
#
# These helpers are what the legacy endpoints share so the binding is one implementation rather
# than thirteen. The digest function is the same one the preset uses, deliberately: two hash
# implementations would drift, and a digest that drifts refuses valid launches.


def plan_digest(fields: Mapping[str, Any]) -> str:
    """SHA-256 over a canonical JSON rendering of the launch-defining fields.

    Canonical means sorted keys and no incidental whitespace, so the digest depends on the values
    and not on dict ordering or json formatting. Shared by the preset path and the legacy paths.
    """
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


#: The fields of a legacy `_prepare_run_*` result that define WHAT WILL RUN AND WHERE. All ten
#: legacy prepares return this same shape -- verified empirically, along with their determinism for
#: a fixed body, which is what makes a re-resolve-and-compare gate possible at all.
#:
#: `cmd` is the resolved argv, so it already embeds the nodelist, the container and version pins,
#: sizes and every other flag the prepare decided. Digesting it covers target drift without having
#: to enumerate per-endpoint semantics, which is why this works uniformly where the preset needed a
#: hand-picked field list.
_LEGACY_DIGEST_KEYS = ("cmd", "target", "rk", "rack", "racks", "nodes", "log_dir")


def launch_digest(prep: Mapping[str, Any]) -> str:
    """Digest of a legacy prepare result. Absent keys are omitted, not defaulted.

    Omitting rather than defaulting matters: a prepare that grows a `racks` key later would
    otherwise have been digesting `"racks": None` all along, and the digest would silently change
    for every caller on the day the key appeared.
    """
    fields: dict[str, Any] = {}
    for key in _LEGACY_DIGEST_KEYS:
        if key not in prep:
            continue
        value = prep[key]
        fields[key] = [str(item) for item in value] if isinstance(value, (list, tuple)) else str(value)
    return plan_digest(fields)


def review_mode() -> str:
    """``strict`` (default), ``warn`` or ``off`` — see docs/ENVIRONMENT.md.

    Default is strict because a permissive default would mean REQ-6 is unmet on a stock deployment,
    which is precisely the criticism the requirement exists to answer. `warn` exists for a site with
    its own automation posting to these endpoints: it keeps launching, and every unbound launch is
    audited so the migration has a worklist instead of a guess.
    """
    mode = (os.environ.get("RACK_LAUNCH_REVIEW") or "strict").strip().lower()
    return mode if mode in ("strict", "warn", "off") else "strict"


def reviewed_launch_error(body: Mapping[str, Any], prep: Mapping[str, Any]):
    """``None`` when this launch may proceed, or a Flask ``(response, status)`` refusing it.

    The gate is a re-resolve-and-compare, not a checksum of the request: the caller has already run
    the prepare again by the time this is called, so a rack that drained, a campaign that was
    superseded or a cluster switched under a stale browser tab all produce a different digest and a
    409 carrying the new one, which is what lets the UI re-present the changed plan.

    Constant-time compare because the digest is echoed by the client. Cheap, and it removes the
    question rather than leaving it to be asked in review.
    """
    mode = review_mode()
    if mode == "off":
        return None
    expected = launch_digest(prep)
    supplied = str(body.get("plan_hash") or "").strip()

    if not supplied:
        if mode == "warn":
            _logger.warning(
                "launch without a reviewed-target digest (RACK_LAUNCH_REVIEW=warn); "
                "expected plan_hash=%s", expected,
            )
            return None
        return jsonify({
            "ok": False,
            "error": "plan_hash from /api/launch/prepare is required",
            "plan_hash": expected,
        }), 409

    if not secrets.compare_digest(supplied, expected):
        # Returned even in warn mode: a MISMATCH is not a legacy caller that never learned the
        # protocol, it is a caller whose reviewed target no longer holds. That is the case the
        # requirement is about, and honouring it in warn mode would make the mode meaningless.
        return jsonify({
            "ok": False,
            "error": "launch target changed since it was reviewed; prepare and review it again",
            "plan_hash": expected,
        }), 409

    return None


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
