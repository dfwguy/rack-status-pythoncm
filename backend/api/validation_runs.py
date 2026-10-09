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
"""Launching validation: every run-* endpoint, and the preset path that is replacing them.

The write side of validation. Each ``_prepare_run_*`` validates a request and builds an exact argv; the
worker threads and node vetting live in ``validation_launch``, and the durable record in
``validation_activities``. Nothing here executes a command directly.

TWO GENERATIONS live here at once, deliberately:

* the per-tool ``run-*`` routes, one endpoint per script, each with its own body schema; and
* the preset path (``/api/validation-presets``, ``prepare``, ``run-validation-preset-async``), where the
  tool, its image, its pins and its scope come from a catalog document instead of from Python.

The preset path is the direction (core-designs.mdc citizen 2): a frozen list of tools in code ships one
site's absolute paths to every other site, and adding a test needs a deploy. New tools should arrive as
catalog documents and be launched through the preset routes -- do not add another run-* endpoint.

Operator-supplied environment is the sharp edge. ``_custom_env_from_body`` and ``_launch_env_from_body``
bound what reaches an sbatch: ``_ENV_KEY_RE`` for the names, ``_ENV_MAX_VARS`` and
``_ENV_MAX_VALUE_LEN`` for the size, and ``_NEMO_MAX_STEPS_LIMIT`` / ``_NCCL_DEBUG_LEVELS`` for the two
knobs where a plausible-looking value is expensive rather than invalid -- a step count that never
finishes holds a rack, and NCCL_DEBUG=TRACE on 90 nodes writes hundreds of MB of logs that the parser
then has to read.
"""

# Notes that stayed in app.py when this module took the code, during the api/ migration. The
# prose describes subjects that live here now; it is reunited with them rather than deleted.
#
# Custom launch env vars (UI "Extra environment variables" field). A key must be a valid
# POSIX-ish env name; values are single-line and length-capped; the count is bounded. These
# guardrails keep a well-formed KEY=VALUE map without allowing injection of newlines / huge
# payloads into the job environment. Forwarded as env_extra (bypasses the SSH allowlist) and
# chained to the container via each launcher's --export=ALL.
from __future__ import annotations

import logging
import os
import re
import secrets
import shlex
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import host_exec
import script_tree
import validation_presets
from activity_hydration import _record_validation_activity
from audit.actions import action
from cluster_metadata import read_cluster_dashboard_metadata
from config_env import (
    _eget,
    _env_int,
    _job_env_int,
    _nvis_workspace_dir_effective,
    _subprocess_env,
)
from core import truthy_env as _truthy_env
from flask import Blueprint, jsonify, request
from job_registry import (
    _llama_jobs,
    _llama_jobs_lock,
    _stress_jobs,
    _stress_jobs_lock,
    _track_sync_op,
    _validation_jobs,
    _validation_jobs_lock,
    _xrack_jobs,
    _xrack_jobs_lock,
)
from naming import _normalize_rack_id, _nvis_naming
from node_targets import (
    _scontrol_hostnames_preserving_case,
    _validation_ct_inner,
    launch_scope_for_target,
    validation_target_for_rack,
)
from paths import (
    _clusterkit_script_path,
    _mnubergemm_stress_script_path,
    _nemo_xrack_script_path,
    _nvloom_singlerack_script_path,
    _resolve_log_dir,
    _run_exemplar_llama_script_path,
    _run_llama_script_path,
    _run_llmb_nccl_script_path,
    _run_nccl_mpi_script_path,
    _script_exists,
    _scripts_dir,
    _sysinfo_script_path,
    _validation_script_path,
)
from slurm_query import (
    _all_slurm_nodenames,
    _dedupe_hostnames_preserve_spelling,
    _rack_node_hostnames_from_inventory,
)
from validation_launch import (
    _catalog_tool_compatibility_error,
    _host_job_cwd,
    _llama_worker,
    _multirack_campaign_snapshot,
    _prepare_validation_preset,
    _stress_worker,
    _validation_node_eligibility,
    _validation_preset_compatibility_error,
    _validation_preset_status_payload,
    _validation_worker,
    _xrack_worker,
    launch_digest,
    reviewed_launch_error,
)

from ._common import operator_from_request as _operator_from_request

_logger = logging.getLogger(__name__)

#: A launch that needs the cluster's workspace (HPC-X, nvloom, LLMB...) on a cluster without one.
_NO_WORKSPACE = (
    "no workspace is configured for this cluster: set it under Projects -> cluster -> Workspace"
)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("validation_runs", __name__, url_prefix="")


def _prepared(
    prepare,
    body: dict,
    *,
    kind: str,
) -> tuple[dict | None, tuple[object, int] | None]:
    """Run a legacy prepare, then apply the reviewed-target gate (REQ-6).

    Wrapping the prepare rather than editing ten launch bodies is deliberate. The ten call sites
    were byte-identical (`prep, err = _prepare_X(body)` / `if err is not None: return err[0],
    err[1]` / `assert prep is not None`), so inlining the gate would have produced ten copies of the
    same four lines -- which pylint's duplicate-code check would flag, and rightly: the next
    endpoint added would be the one that forgot the gate. Here it is impossible to add a launch
    endpoint through this helper and not be bound.

    Returns the prepare's own error unchanged when the prepare failed, or the gate's 409 when the
    reviewed target no longer holds. Both are the `(response, status)` shape the call sites already
    unpack, so no call site changes shape.
    """
    tool_id = _CATALOG_TOOL_BY_KIND.get(kind)
    if tool_id:
        compatibility_error = _catalog_tool_compatibility_error(tool_id)
        if compatibility_error is not None:
            return None, compatibility_error
    prep, err = prepare(body)
    if err is not None:
        return None, err
    assert prep is not None
    gate = reviewed_launch_error(body, prep)
    if gate is not None:
        return None, gate
    return prep, None


#: Launch kinds `POST /api/launch/prepare` can resolve, mapped to the prepare that owns each.
#: Populated below, after the prepares are defined -- a dict literal up here would reference names
#: that do not exist yet.
_PREPARE_BY_KIND: dict[str, object] = {}


def _validation_tool_and_stage() -> tuple[str, str]:
    """Map configured validation script to activity tool + stage."""
    name = _validation_script_path().name.lower()
    if "llmb_nemo" in name:
        return "llmb-nemo-combined", "combined"
    return "singlerack", "stage2"


def _clusterkit_product_line_for_job(body: dict) -> str:
    """Prefer POST product_line, then saved cluster dashboard metadata, then process env."""
    v = body.get("product_line")
    if isinstance(v, str) and v.strip():
        return v.strip()
    try:
        meta = read_cluster_dashboard_metadata()
        pl = (meta.get("product_line") or "").strip()
        if pl:
            return pl
    except Exception:
        _logger.debug("_clusterkit_product_line_for_job: ignoring error", exc_info=True)
    return (_eget("RACK_CLUSTER_PRODUCT_LINE") or _eget("RACK_PRODUCT_LINE") or "").strip()


def _normalize_llama_nodes_spec(raw: object) -> str:
    """Slurm nodeset / hostlist fragment for ``run-llama.sh`` (no shell metacharacters)."""
    if not isinstance(raw, str):
        raise ValueError("nodes_spec must be a string")  # noqa: TRY004 - routes catch ValueError to answer 400; TypeError would 500
    t = raw.strip()
    if not t or len(t) > 800:
        raise ValueError("nodes_spec is empty or too long")
    for c in ";|&$`'\n\r()<>":
        if c in t:
            raise ValueError("nodes_spec contains forbidden characters")
    return t


_NCCL_DEBUG_LEVELS = ("VERSION", "WARN", "INFO", "TRACE")


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


_ENV_MAX_VARS = 32


_ENV_MAX_VALUE_LEN = 2048


_NEMO_MAX_STEPS_LIMIT = 1_000_000


def _custom_env_from_body(body: dict) -> dict[str, str]:
    """Validated user-supplied ``env`` map from the POST body ({"KEY": "VALUE", ...}).

    Silently drops entries with an invalid key, a multi-line/oversized value, or beyond the
    per-launch cap (the UI validates too, so a bad row just doesn't get applied).
    """
    extra = body.get("env")
    if not isinstance(extra, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in extra.items():
        key = str(k).strip()
        if not _ENV_KEY_RE.match(key):
            continue
        val = str(v)
        if "\n" in val or "\r" in val or len(val) > _ENV_MAX_VALUE_LEN:
            continue
        out[key] = val
        if len(out) >= _ENV_MAX_VARS:
            break
    return out


def _launch_env_from_body(body: dict) -> dict[str, str]:
    """Per-launch env knobs parsed from the POST body: NCCL debug level + custom env vars.

    ``nccl_debug`` accepts VERSION/WARN/INFO/TRACE (case-insensitive). ``off``/``default``/
    empty/absent means "leave the script default" (no override). We set BOTH
    ``NVIS_NCCL_DEBUG`` (honored by the NEMO launchers, which otherwise hard-code the
    level) and raw ``NCCL_DEBUG`` (read directly by ``nccl_allreduce.sbatch`` and the
    portable Stage-1 launcher) so the toggle reaches every NCCL/NEMO launch path.

    ``env`` is a user-supplied KEY=VALUE map from the launch dialog (e.g.
    ``OMPI_MCA_coll_hcoll_enable=0``). Explicit keys win over the NCCL-debug defaults.

    All of these are threaded to the worker as ``env_extra`` so they reach the remote
    cluster over SSH (the forward allowlist is bypassed for explicit ``env_extra`` keys)
    and are forwarded into the container by each launcher's ``--export=ALL``.
    """
    out: dict[str, str] = {}
    raw = body.get("nccl_debug")
    if raw is not None:
        lvl = str(raw).strip().upper()
        if lvl and lvl not in ("OFF", "DEFAULT", "NONE") and lvl in _NCCL_DEBUG_LEVELS:
            out["NVIS_NCCL_DEBUG"] = lvl
            out["NCCL_DEBUG"] = lvl
    # NEMO training length. NVIS_NEMO_MAX_STEPS is what the job runs; NEMO_TARGET_STEPS is the
    # minimum DECLARED total the log labeller requires before calling a run finished
    # (lib/llm_label.inc.sh, default 2010). They MUST move together — a 50-step run left at the
    # 2010 threshold completes but is never labelled done.
    steps = body.get("max_steps")
    if isinstance(steps, bool):
        steps = None
    if isinstance(steps, (int, float)) and 1 <= int(steps) <= _NEMO_MAX_STEPS_LIMIT:
        out["NVIS_NEMO_MAX_STEPS"] = str(int(steps))
        out["NEMO_TARGET_STEPS"] = str(int(steps))
    # User-supplied vars win over the debug defaults above.
    custom = _custom_env_from_body(body)
    out.update(custom)
    # Advertise the exact set of user-supplied keys so multi-node launchers can forward
    # precisely these to remote ranks. nccl_allreduce.sbatch only propagates an explicit
    # -x allowlist to orted on nodes other than the mpirun host; without this, a custom
    # var (e.g. NCCL_NET_PLUGIN=none) would apply to local ranks only.
    if custom:
        out["NVIS_EXTRA_ENV_KEYS"] = " ".join(sorted(custom))
    return out


def _nccl_version_cli(body: dict) -> list[str] | tuple[object, int]:
    """Validate ``body['nccl_version']`` → ``["--nccl-version", v]`` for run_nccl_mpi.sh.

    Shared by the direct NCCL launch surfaces (single-rack Stage-1 + cross-rack) so the
    version selector behaves identically. Returns ``[]`` when unset/default, or an
    ``(jsonify(error), 400)`` tuple on an invalid token. "system"/"default" = host build
    (2.28.9); "X.Y.Z" = a vetted NFS build loaded via LD_LIBRARY_PATH.
    """
    ver = (body.get("nccl_version") or _eget("RACK_NCCL_MPI_VERSION") or "").strip()
    if not ver:
        return []
    if not re.match(r"^(system|default|[0-9]+(\.[0-9]+){1,3})$", ver):
        return (jsonify({"ok": False, "error": f"invalid nccl_version: {ver!r}"}), 400)
    return ["--nccl-version", ver]


def _nccl_rounds_from_body(body: dict, default: int | None) -> int | None:
    """NCCL sweep rounds from the launch body (``iters``), clamped to 1..50.

    ``default`` is returned when the body omits ``iters`` (single-rack Stage-1 passes 5 so the
    acceptance sweep runs 5 times; the x-rack path passes ``None`` to leave the sbatch default).
    """
    raw = body.get("iters")
    if raw is None:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, 50))


def _prepare_run_validation(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Shared parse for sync and async validation. Returns (prep, None) or (None, (jsonify(...), code))."""
    if os.environ.get("RACK_VALIDATION_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "run-validation disabled (RACK_VALIDATION_DISABLED)"}), 403)
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    rack = (body.get("rack") or "").strip()
    if rack:
        try:
            rk = _normalize_rack_id(rack)
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)
    else:
        rk = ""
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, (jsonify({"ok": False, "error": "target must be a string"}), 400)
    try:
        target = validation_target_for_rack(rack, tgt_ov if isinstance(tgt_ov, str) else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _validation_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"validation script not found: {script}"}), 500)
    # Pass the resolved rack id as $2 when available so the script names rack logs correctly for
    # any naming convention. Rackless exact-node launches leave $2 empty; the wrapper uses the
    # explicit target only as its legacy log label.
    cmd = ["bash", str(script), target, rk]
    display_cmd = "./" + script.name + " " + shlex.quote(target) + " " + shlex.quote(rk)
    timeout = max(30, _job_env_int("RACK_VALIDATION_TIMEOUT", 600))
    return (
        {
            "log_dir": log_dir,
            "rk": rk,
            "target": target,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            # run_llmb_nccl.sh derives LLMB_INSTALL / LLMB_VENV from the cluster's workspace
            # (connection.workspace_dir -> NVIS_WORKSPACE_DIR, workspace-dir-common.sh); explicit
            # LLMB_* Settings values still override. Over SSH only these env_extra keys and the
            # NVIS_/RACK_ prefixed overlay reach the script, so they are passed explicitly.
            "env_updates": {
                **{
                    k: v
                    for k in ("LLMB_INSTALL", "LLMB_REPO", "LLMB_VENV", "NVIS_WORKSPACE_DIR")
                    if (v := (_eget(k) or "").strip())
                },
                # Launch-dialog vars win over the stored Settings values.
                **_launch_env_from_body(body),
            },
        },
        None,
    )


@bp.route("/api/run-validation", methods=["POST"])
@action("validation:run")
def api_run_validation():
    """Run singlerack validation script with one target, e.g. p26-r05-ct[01-18] (cwd = log_dir)."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepared(
        _prepare_run_validation,
        body,
        kind="validation",
    )
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    target = str(prep["target"])
    rk = str(prep["rk"])
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    try:
        with _track_sync_op(rk, "validation", _operator_from_request()):
            # Privileged "script" target (root@slogin): pdsh-rsync into the node image cache
            # + sbatch. Routes via host_exec so SSH mode runs on the cluster, not locally.
            proc = host_exec.run_on_host(
                cmd,
                timeout=timeout,
                cwd=str(log_dir),
                env=_subprocess_env(),
                profile="script",
            )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "validation script timed out", "target": target}), 504
    except Exception as e:
        _logger.debug("api_run_validation: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e), "target": target}), 500
    ok = proc.returncode == 0
    return jsonify(
        {
            "ok": ok,
            "returncode": proc.returncode,
            "rack": rk,
            "target": target,
            "command_display": display_cmd,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
        }
    ), (200 if ok else 502)


@bp.route("/api/run-validation-async", methods=["POST"])
@action("validation:run", variant="async")
def api_run_validation_async():
    """Start validation in a background thread; poll GET /api/run-validation-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepared(
        _prepare_run_validation,
        body,
        kind="validation",
    )
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    rk = str(prep["rk"])
    target = str(prep["target"])
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) and _eu else None
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    val_tool, val_stage = _validation_tool_and_stage()
    scope, scope_nodes = launch_scope_for_target(target)
    _record_validation_activity(
        val_tool,
        stage=val_stage,
        scope=scope if rk else "node",
        nodes=scope_nodes,
        rack=rk,
        racks=[rk] if rk else [],
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    if env_merged is None:
        env_merged = {}
    env_merged = {**env_merged, "NVIS_ACTIVITY_ID": job_id}
    with _validation_jobs_lock:
        _validation_jobs[job_id] = {
            "job_id": job_id,
            "kind": "validation",
            "operator": operator,
            "rack": rk,
            "target": target,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_validation_worker,
        args=(job_id, cmd, str(log_dir), timeout, display_cmd),
        kwargs={"env_updates": env_merged},
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rk,
            "target": target,
            "command_display": display_cmd,
        }
    )


def _prepare_run_nemo_xrack(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Parse POST body for ``run-nemo-xrack.sh`` (comma-separated rack-ct[inner] specs)."""
    if os.environ.get("RACK_NEMO_XRACK_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "multinode NEMO launch disabled (RACK_NEMO_XRACK_DISABLED)"}),
            403,
        )
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    racks_raw = body.get("racks")
    if not isinstance(racks_raw, list) or len(racks_raw) < 1:
        return None, (jsonify({"ok": False, "error": "racks must be a non-empty array of rack ids"}), 400)
    inner = _validation_ct_inner()
    naming = _nvis_naming()
    specs: list[str] = []
    seen: set[str] = set()
    for item in racks_raw:
        rk = _normalize_rack_id(str(item).strip())
        if rk in seen:
            continue
        seen.add(rk)
        # Build the tray spec from NVIS_CT_ID, never a literal "-ct". The joiner is a naming
        # property: Bright is <rack>-ct<NN>, but Nscale TX is <rack>-<NN>, so the hardcoded form
        # produced ink50-dh01-h02-ct[11-18,...] — hosts that exist in neither DNS nor Slurm.
        # pdsh then failed ssh 255 on every node and sbatch rejected the list with
        # "Invalid node name specified". Every other tray-spec site already uses ct_blob.
        specs.append(naming.ct_blob(rk, inner))
    if not specs:
        return None, (jsonify({"ok": False, "error": "no valid rack ids"}), 400)
    script = _nemo_xrack_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"run-nemo-xrack script not found: {script}"}), 500)
    arg = ",".join(specs)
    cmd = ["bash", str(script), arg]
    display_cmd = "./" + script.name + " " + shlex.quote(arg)
    timeout = max(300, _job_env_int("RACK_NEMO_XRACK_TIMEOUT", 7200))
    return (
        {
            "log_dir": log_dir,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "arg": arg,
            "racks": sorted(seen),
            "env_updates": _launch_env_from_body(body),
        },
        None,
    )


@bp.route("/api/run-nemo-xrack-async", methods=["POST"])
@action("validation:run_nemo", variant="async")
def api_run_nemo_xrack_async():
    """Start ``run-nemo-xrack.sh`` in the background; poll ``/api/run-nemo-xrack-status``.

    Process cwd is *log_dir* (same as ``/api/run-validation``) so Slurm batch stdout files land there.
    """
    body = request.get_json(silent=True) or {}
    prep, err = _prepared(
        _prepare_run_nemo_xrack,
        body,
        kind="nemo-xrack",
    )
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    arg = str(prep["arg"])
    racks = [str(r) for r in prep.get("racks") or []]
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) and _eu else None
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    # Same as run-validation: cwd=log_dir so Slurm --output=… writes under the UI log dir.
    cwd = _host_job_cwd(log_dir)
    with _xrack_jobs_lock:
        _xrack_jobs[job_id] = {
            "job_id": job_id,
            "kind": "xrack",
            "operator": operator,
            "rack": "multinode",
            "racks": racks,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
            "xrack_arg": arg,
        }
    _record_validation_activity(
        "nemo-xrack",
        stage="multirack",
        scope="multirack",
        rack=racks[0] if racks else "",
        racks=racks,
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    threading.Thread(
        target=_xrack_worker,
        args=(job_id, cmd, cwd, timeout, display_cmd),
        kwargs={"env_updates": env_merged},
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "command_display": display_cmd,
        }
    )


def _prepare_run_llama(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Parse POST body for ``run-llama.sh`` (nodeset string, cwd = log_dir)."""
    if os.environ.get("RACK_LLAMA_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "LLAMA launch disabled (RACK_LLAMA_DISABLED)"}), 403)
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
    try:
        nodes_spec = _normalize_llama_nodes_spec(body.get("nodes_spec"))
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _run_llama_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"run-llama.sh not found: {script}"}), 500)
    inner = f"module load slurm && exec bash {shlex.quote(str(script))} {shlex.quote(nodes_spec)}"
    cmd = ["bash", "-lc", inner]
    display_cmd = f"module load slurm && ./{script.name} {shlex.quote(nodes_spec)}"
    timeout = max(120, _job_env_int("RACK_LLAMA_TIMEOUT", 7200))
    return (
        {
            "log_dir": log_dir,
            "rack": rk,
            "nodes_spec": nodes_spec,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
        },
        None,
    )


def _prepare_run_exemplar_llama(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Build the exact 512-GPU GB300 Llama 3.1 405B Exemplar launch.

    Each selected NVL72 rack contributes 16 tray hosts (64 GPUs), so an accepted
    request is exactly eight racks / 128 hosts / 512 GPUs. The wrapper repeats the
    node-count and Slurm-state checks immediately before ``llmb-run submit``.
    """
    if _truthy_env("RACK_EXEMPLAR_LLAMA_DISABLED"):
        return None, (
            jsonify({"ok": False, "error": "Exemplar Llama launch disabled (RACK_EXEMPLAR_LLAMA_DISABLED)"}),
            403,
        )

    racks_raw = body.get("racks")
    if not isinstance(racks_raw, list):
        return None, (jsonify({"ok": False, "error": "racks must be an array of eight rack ids"}), 400)
    racks: list[str] = []
    seen: set[str] = set()
    try:
        for item in racks_raw:
            rack = _normalize_rack_id(str(item).strip())
            if rack not in seen:
                seen.add(rack)
                racks.append(rack)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    if len(racks) != 8:
        return None, (
            jsonify({"ok": False, "error": f"the 512-GPU Exemplar gate requires exactly 8 racks; got {len(racks)}"}),
            400,
        )

    dtype = str(body.get("dtype") or "nvfp4").strip().lower()
    if dtype not in ("fp8", "nvfp4"):
        return None, (jsonify({"ok": False, "error": "dtype must be fp8 or nvfp4"}), 400)
    image_profile = str(body.get("image_profile") or "nccl2.30.7").strip().lower()
    if image_profile not in ("nccl2.30.7", "stock"):
        return None, (
            jsonify({"ok": False, "error": "image_profile must be nccl2.30.7 or stock"}),
            400,
        )
    tuning = str(body.get("tuning") or "baseline").strip().lower()
    if tuning not in ("baseline", "gb300"):
        return None, (jsonify({"ok": False, "error": "tuning must be baseline or gb300"}), 400)

    inner = (_eget("RACK_EXEMPLAR_LLAMA_CT_RANGE", "11-18,28-35") or "11-18,28-35").strip()
    if not re.fullmatch(r"\d{1,3}(?:-\d{1,3})?(?:,\d{1,3}(?:-\d{1,3})?)*", inner):
        return None, (
            jsonify({"ok": False, "error": f"invalid RACK_EXEMPLAR_LLAMA_CT_RANGE: {inner!r}"}),
            500,
        )
    naming = _nvis_naming()
    nodelist = ",".join(naming.ct_blob(rack, inner) for rack in sorted(racks))

    script = _run_exemplar_llama_script_path()
    if not _script_exists(script):
        return None, (
            jsonify({"ok": False, "error": f"run-exemplar-llama script not found: {script}"}),
            500,
        )
    dry_run = bool(body.get("dry_run"))
    cmd = [
        "bash",
        str(script),
        "--dtype",
        dtype,
        "--nodelist",
        nodelist,
        "--image",
        image_profile,
        "--tuning",
        tuning,
    ]
    if dry_run:
        cmd.append("--dry-run")
    display_cmd = shlex.join(cmd)
    llmb_install = (_eget("RACK_LLMB_INSTALL", "/home/cmods/workspace/llmb") or "/home/cmods/workspace/llmb").strip()
    log_dir = f"{llmb_install.rstrip('/')}/workloads/pretrain_llama3.1/experiments"
    timeout = max(120, _job_env_int("RACK_EXEMPLAR_LLAMA_TIMEOUT", 900))
    return (
        {
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "cwd": llmb_install,
            "log_dir": log_dir,
            "racks": sorted(racks),
            "nodelist": nodelist,
            "dtype": dtype,
            "image_profile": image_profile,
            "tuning": tuning,
            "dry_run": dry_run,
        },
        None,
    )


@bp.route("/api/run-exemplar-llama-async", methods=["POST"])
@action("validation:run_exemplar_llama", variant="async")
def api_run_exemplar_llama_async():
    """Submit Llama 3.1 405B Exemplar pretraining across eight selected racks."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepared(
        _prepare_run_exemplar_llama,
        body,
        kind="exemplar-llama",
    )
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    racks = [str(r) for r in prep["racks"]]
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    display_cmd = str(prep["display_cmd"])
    log_dir = str(prep["log_dir"])
    dry_run = bool(prep["dry_run"])
    _record_validation_activity(
        "llama-exemplar",
        stage="stage2",
        scope="multirack",
        rack=racks[0],
        racks=racks,
        log_dir=log_dir,
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
        dry_run=dry_run,
    )
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "llama-exemplar",
            "operator": operator,
            "rack": "8 racks",
            "racks": racks,
            "nodelist": prep["nodelist"],
            "gpu_count": 512,
            "dtype": prep["dtype"],
            "image_profile": prep["image_profile"],
            "tuning": prep["tuning"],
            "dry_run": dry_run,
            "log_dir": log_dir,
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    env_updates = {"NVIS_ACTIVITY_ID": job_id}
    threading.Thread(
        target=_stress_worker,
        args=(job_id, prep["cmd"], str(prep["cwd"]), int(prep["timeout"]), display_cmd),
        kwargs={
            "error_label": "run-exemplar-llama script",
            "profile": "script",
            "persist": True,
            "env_updates": env_updates,
        },
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "racks": racks,
            "nodelist": prep["nodelist"],
            "gpu_count": 512,
            "dtype": prep["dtype"],
            "image_profile": prep["image_profile"],
            "tuning": prep["tuning"],
            "dry_run": dry_run,
            "command_display": display_cmd,
        }
    )


@bp.route("/api/run-llama-async", methods=["POST"])
@action("validation:run_llama", variant="async")
def api_run_llama_async():
    """Start ``run-llama.sh`` in the background; poll ``/api/run-llama-status``."""
    body = request.get_json(silent=True) or {}
    prep, err = _prepared(
        _prepare_run_llama,
        body,
        kind="llama",
    )
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    rk = str(prep["rack"])
    nodes_spec = str(prep["nodes_spec"])
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    cwd = _host_job_cwd(log_dir)
    _record_validation_activity(
        "llama",
        stage="other",
        scope="rack",
        rack=rk,
        racks=[rk],
        log_dir=cwd,
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    with _llama_jobs_lock:
        _llama_jobs[job_id] = {
            "job_id": job_id,
            "kind": "llama",
            "operator": operator,
            "rack": rk,
            "log_dir": cwd,
            "nodes_spec": nodes_spec,
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_llama_worker,
        args=(job_id, cmd, cwd, timeout, display_cmd),
        daemon=True,
    ).start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rk,
            "command_display": display_cmd,
        }
    )


def _prepare_run_mnubergemm_stress(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Shared parse for async mnubergemm stress. Returns (prep, None) or (None, (jsonify(...), code))."""
    if os.environ.get("RACK_MNUBERGEMM_STRESS_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "run-stress disabled (RACK_MNUBERGEMM_STRESS_DISABLED)"}),
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
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, (jsonify({"ok": False, "error": "target must be a string"}), 400)
    try:
        target = validation_target_for_rack(rack, tgt_ov if isinstance(tgt_ov, str) else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _mnubergemm_stress_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"stress script not found: {script}"}), 500)
    cmd = ["bash", str(script), target]
    display_cmd = "./" + script.name + " " + shlex.quote(target)
    timeout = max(120, _env_int("RACK_MNUBERGEMM_STRESS_TIMEOUT", 86400))
    ws = _nvis_workspace_dir_effective()
    if not ws:
        return None, (jsonify({"ok": False, "error": _NO_WORKSPACE}), 400)
    cuda_m = (_eget("RACK_MNUBERGEMM_CUDA_MAJOR") or _eget("RACK_NVLOOM_CUDA_MAJOR") or _eget("CUDA_MAJ") or "13").strip()
    if cuda_m not in ("12", "13"):
        cuda_m = "13"
    env_updates: dict[str, str] = {
        "WORKSPACE_DIR": ws,
        "NVIS_WORKSPACE_DIR": ws,
        "TOPDIR": ws,
        "NVIS_BIN_SCRIPTS": script_tree.scripts_dir(),
        "CUDA_MAJ": cuda_m,
        "NVIS_SYSINFO_SH": str(_sysinfo_script_path()),
        "NVIS_CHECKER_SH": script_tree.resolve("checker.sh"),
    }
    for k in ("NVIS_HPCX_ROOT", "NVIS_NCCL_PATH", "RACK_MNUBERGEMM_PARTITION"):
        v = (_eget(k) or "").strip()
        if v:
            env_updates[k] = v
    return (
        {
            "log_dir": log_dir,
            "rk": rk,
            "target": target,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "env_updates": env_updates,
        },
        None,
    )


def _llmb_env_updates(body: dict) -> dict[str, str]:
    """Env for the llmb-run launchers.

    ``_eget()`` reads Settings out of ``var/cache/job_env_overrides.json``, which is NOT part
    of ``os.environ`` and therefore does NOT cross the SSH hop to the head node. Without this,
    ``run_llmb_nccl.sh`` silently falls back to its built-in ``/home/nvis/...`` defaults and
    cannot find llmb-run. Forward the workspace keys explicitly.
    """
    out: dict[str, str] = {}
    for key in ("LLMB_INSTALL", "LLMB_REPO", "LLMB_VENV", "LLMB_RUN", "NVIS_WORKSPACE_DIR"):
        val = (_eget(key) or "").strip()
        if val:
            out[key] = val
    out.update(_launch_env_from_body(body))
    return out


def _prepare_run_nccl_fullrack(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Shared parse for the standalone Stage-1 NCCL benchmark.

    Converged onto the SAME direct path as the cross-rack test (``run_nccl_mpi.sh`` →
    ``nccl_allreduce.sbatch``: host nccl-tests + mpirun, NCCL over the IB rails, library
    version selectable via LD_LIBRARY_PATH). ``--app-log-rack`` names the Slurm output
    ``<user>-NCCL-1Rack-<rack>-<n>N-<jobid>.txt`` so the dashboard row + parser flip the
    rack's Stage-1 NCCL status, exactly as the legacy portable launcher did. Uses the
    ``script`` profile (root@slogin) like the other NCCL launchers.
    """
    if os.environ.get("RACK_NCCL_FULLRACK_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "NCCL benchmark disabled (RACK_NCCL_FULLRACK_DISABLED)"}),
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
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, (jsonify({"ok": False, "error": "target must be a string"}), 400)
    tgt_ov_s = tgt_ov.strip() if isinstance(tgt_ov, str) else ""
    # Resolve the node target. When the UI sends an explicit node selection we submit those
    # exact trays via ``--nodelist`` (so e.g. a rack minus a down/faulted node runs on 17);
    # with no selection we pass the rack id and let run_nccl_mpi.sh resolve the full rack.
    try:
        target = validation_target_for_rack(rack, tgt_ov_s or None)
    except ValueError:
        target = rk
    # Stage-1 runs through llmb-run (run_llmb_nccl.sh), not raw sbatch. Operator ask
    # 2026-07-28: "fix stage 1 NCCL to use llmb run - no more sbatch - only for MNUberGEMM
    # everything else is llmbrun". One llmb submit runs the whole pre-generated testset
    # (24 tests: all_reduce, all_gather, reduce_scatter, alltoall, sendrecv), so the
    # per-collective knobs below (--size-max / --nccl-version / --rounds) do not apply --
    # the sweep is defined by llmb_repo/nccl/config/testset.toml and baked into launch.sh.
    # Set RACK_NCCL_FULLRACK_LAUNCHER=mpi to fall back to run_nccl_mpi.sh + nccl_allreduce.sbatch.
    launcher = (_eget("RACK_NCCL_FULLRACK_LAUNCHER") or "llmb").strip().lower()
    use_llmb = launcher not in ("mpi", "sbatch", "direct", "nccl_mpi")
    script = _run_llmb_nccl_script_path() if use_llmb else _run_nccl_mpi_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"NCCL launcher not found: {script}"}), 500)
    env_updates: dict[str, str] = {"RACK_JOB_LOG_DIR": str(log_dir), "NVIS_RACK": rk}

    if use_llmb:
        cmd = ["bash", str(script)]
        reservation = (body.get("reservation") or _eget("RACK_LLMB_NCCL_RESERVATION") or "").strip()
        if reservation:
            cmd += ["--reservation", reservation]
        if bool(body.get("dry_run")):
            cmd.append("--dry-run")
        # Explicit tray selection -> --nodelist; otherwise let the script expand the rack id.
        if tgt_ov_s:
            cmd += ["--nodelist", target]
            node_arg_display = " --nodelist " + shlex.quote(target)
        else:
            cmd.append(rk)
            node_arg_display = " " + shlex.quote(rk)
        display_cmd = "./" + script.name + (
            (" --reservation " + shlex.quote(reservation)) if reservation else ""
        ) + (" --dry-run" if body.get("dry_run") else "") + node_arg_display
        timeout = max(120, _job_env_int("RACK_LLMB_NCCL_TIMEOUT", 14400))
        env_updates.update(_llmb_env_updates(body))
    else:
        # Cap the all_reduce sweep. Accept the legacy `end`/RACK_NCCL_FULLRACK_END knobs too.
        size_max = (
            body.get("size_max") or body.get("end") or _eget("RACK_NCCL_MPI_SIZE_MAX")
            or _eget("RACK_NCCL_FULLRACK_END") or "16G"
        ).strip()
        cmd = ["bash", str(script), "--app-log-rack", rk]
        if size_max:
            cmd += ["--size-max", size_max]
        vc = _nccl_version_cli(body)
        if isinstance(vc, tuple):
            return None, vc
        cmd += vc
        nccl_version = vc[1] if vc else ""
        # Stage-1 acceptance runs the sweep 5× by default; the UI modal can override via ``iters``.
        rounds = _nccl_rounds_from_body(body, 5) or 5
        cmd += ["--rounds", str(rounds)]
        if tgt_ov_s:
            cmd += ["--nodelist", target]
            node_arg_display = " --nodelist " + shlex.quote(target)
        else:
            cmd.append(rk)
            node_arg_display = " " + shlex.quote(rk)
        display_cmd = "./" + script.name + " --app-log-rack " + shlex.quote(rk) + (
            (" --size-max " + shlex.quote(size_max)) if size_max else ""
        ) + (
            (" --nccl-version " + shlex.quote(nccl_version)) if nccl_version else ""
        ) + " --rounds " + str(rounds) + node_arg_display
        timeout = max(120, _job_env_int("RACK_NCCL_FULLRACK_TIMEOUT", 7200))
        # run_nccl_mpi.sh writes its Slurm --output under RACK_JOB_LOG_DIR (defaults to cwd; the
        # worker cwd is log_dir, but set it explicitly so the app log always lands in log_dir).
        env_updates.update(_launch_env_from_body(body))
    return (
        {
            "log_dir": log_dir,
            "rk": rk,
            "target": target,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "env_updates": env_updates,
        },
        None,
    )


def _prepare_run_nvloom_singlerack(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Shared parse for async nvloom singlerack Slurm job. Returns (prep, None) or (None, (jsonify(...), code))."""
    if os.environ.get("RACK_NVLOOM_SINGLERACK_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "nvloom singlerack disabled (RACK_NVLOOM_SINGLERACK_DISABLED)"}),
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
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, (jsonify({"ok": False, "error": "target must be a string"}), 400)
    try:
        target = validation_target_for_rack(rack, tgt_ov if isinstance(tgt_ov, str) else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _nvloom_singlerack_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"nvloom script not found: {script}"}), 500)
    cmd = ["bash", str(script), target]
    display_cmd = "./" + script.name + " " + shlex.quote(target)
    timeout = max(120, _env_int("RACK_NVLOOM_SINGLERACK_TIMEOUT", 10800))
    ws = _nvis_workspace_dir_effective()
    if not ws:
        return None, (jsonify({"ok": False, "error": _NO_WORKSPACE}), 400)
    cuda_m = (_eget("RACK_NVLOOM_CUDA_MAJOR") or _eget("CUDA_MAJ") or "13").strip()
    if cuda_m not in ("12", "13"):
        cuda_m = "13"
    env_updates: dict[str, str] = {
        "NVIS_SYSINFO_SH": str(_sysinfo_script_path()),
        "RACK_SAMPLES": str(max(1, min(256, _env_int("RACK_NVLOOM_RACK_SAMPLES", 5)))),
        "RACK_NVLOOM_SINGLERACK_RACK": rk,
        "WORKSPACE_DIR": ws,
        "NVIS_WORKSPACE_DIR": ws,
        "TOPDIR": ws,
        "NVIS_BIN_SCRIPTS": script_tree.scripts_dir(),
        "CUDA_MAJ": cuda_m,
    }
    for k in ("NVIS_NVLOOM_HPCX_INIT", "NVIS_NVLOOM_CLI", "NVIS_NVLOOM_UCX_TLS", "RACK_NVLOOM_PARTITION"):
        v = (_eget(k) or "").strip()
        if v:
            env_updates[k] = v
    return (
        {
            "log_dir": log_dir,
            "rk": rk,
            "target": target,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "env_updates": env_updates,
        },
        None,
    )


def _prepare_run_clusterkit_singlerack(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Shared parse for async ClusterKit singlerack Slurm job."""
    if os.environ.get("RACK_CLUSTERKIT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "clusterkit disabled (RACK_CLUSTERKIT_DISABLED)"}),
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
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, (jsonify({"ok": False, "error": "target must be a string"}), 400)
    try:
        target = validation_target_for_rack(rack, tgt_ov if isinstance(tgt_ov, str) else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _clusterkit_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"clusterkit script not found: {script}"}), 500)
    cmd = ["bash", str(script), target]
    display_cmd = "./" + script.name + " " + shlex.quote(target)
    timeout = max(120, _job_env_int("RACK_CLUSTERKIT_TIMEOUT", 14400))
    ws = _nvis_workspace_dir_effective()
    if not ws:
        return None, (jsonify({"ok": False, "error": _NO_WORKSPACE}), 400)
    cuda_m = (_eget("RACK_CLUSTERKIT_CUDA_MAJOR") or _eget("RACK_NVLOOM_CUDA_MAJOR") or _eget("CUDA_MAJ") or "13").strip()
    if cuda_m not in ("12", "13"):
        cuda_m = "13"
    env_updates: dict[str, str] = {
        "WORKSPACE_DIR": ws,
        "NVIS_WORKSPACE_DIR": ws,
        "TOPDIR": ws,
        "NVIS_BIN_SCRIPTS": script_tree.scripts_dir(),
        "CUDA_MAJ": cuda_m,
        "RACK_CLUSTERKIT_RACK": rk,
    }
    pl = _clusterkit_product_line_for_job(body)
    if pl:
        env_updates["RACK_CLUSTER_PRODUCT_LINE"] = pl
    for k in (
        "NVIS_HPCX_ROOT",
        "NVIS_CLUSTERKIT_HPCX_INIT",
        "RACK_CLUSTERKIT_PARTITION",
        "RACK_CLUSTERKIT_EXTRA_OPTS",
        "NVIS_RDMA_LINK_TECH",
        "RACK_CLUSTERKIT_LINK_TECH",
        "RACK_CLUSTERKIT_HCA_PROFILE",
        "RACK_CLUSTERKIT_UCX_TLS",
    ):
        v = (_eget(k) or "").strip()
        if v:
            env_updates[k] = v
    return (
        {
            "log_dir": log_dir,
            "rk": rk,
            "target": target,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "env_updates": env_updates,
        },
        None,
    )


def _prepare_run_llmb_nccl_container(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Prepare the audited containerized LLMB 24-test NCCL workflow."""
    try:
        log_raw = body.get("log_dir")
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as exc:
        return None, (jsonify({"ok": False, "error": str(exc)}), 400)
    storage = str(body.get("storage") or "local").strip().lower()
    if storage not in ("local", "nfs"):
        return None, (jsonify({"ok": False, "error": "storage must be local or nfs"}), 400)
    p2p_managed = validation_presets.node_images_p2p_managed()
    rsync = body.get("rsync_to_local", body.get("rsync", storage == "local" and not p2p_managed)) is True
    if rsync and p2p_managed:
        return None, (jsonify({"ok": False, "error": (
            "rsync staging is off on this cluster: NVIS-P2P distributes its images; "
            "run a P2P campaign for the image instead")}), 400)
    if rsync and storage != "local":
        return None, (jsonify({"ok": False, "error": "rsync requires storage=local"}), 400)
    try:
        local_dir = validation_presets.check_local_container_dir(body.get("local_container_dir"))
    except ValueError as exc:
        return None, (jsonify({"ok": False, "error": str(exc)}), 400)

    nodes_raw = body.get("nodes")
    racks_raw = body.get("racks")
    if racks_raw is None and body.get("rack"):
        racks_raw = [body.get("rack")]
    if nodes_raw is not None and (not isinstance(nodes_raw, list) or not nodes_raw):
        return None, (jsonify({"ok": False, "error": "nodes must be a non-empty array"}), 400)
    if racks_raw is not None and (not isinstance(racks_raw, list) or not racks_raw):
        return None, (jsonify({"ok": False, "error": "racks must be a non-empty array"}), 400)

    racks: list[str] = []
    nodes: list[str] = []
    try:
        for raw in racks_raw or []:
            rack = _normalize_rack_id(str(raw).strip())
            if rack not in racks:
                racks.append(rack)
    except ValueError as exc:
        return None, (jsonify({"ok": False, "error": str(exc)}), 400)
    if isinstance(nodes_raw, list):
        for raw in nodes_raw:
            node = str(raw).strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", node):
                return None, (jsonify({"ok": False, "error": f"invalid node name: {raw!r}"}), 400)
            nodes.append(node)
    target = body.get("target")
    if not nodes and isinstance(target, str) and target.strip():
        nodes = _scontrol_hostnames_preserving_case(target)
        if not nodes:
            return None, (jsonify({"ok": False, "error": "target did not resolve to any Slurm nodes"}), 400)

    campaign_id = ""
    if not nodes and racks:
        try:
            campaign = _multirack_campaign_snapshot()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            return None, (jsonify({"ok": False, "error": f"exact-node campaign unavailable: {exc}"}), 503)
        campaign_id = str(campaign.get("id") or "")
        requested = str(body.get("campaign") or "").strip()
        if requested and requested != campaign_id:
            return None, (jsonify({"ok": False, "error": "stale or unknown exact-node campaign id"}), 409)
        indexed = {str(row.get("rack") or ""): row for row in campaign.get("racks", []) if isinstance(row, dict)}
        for rack in racks:
            if campaign.get("active"):
                row = indexed.get(rack)
                if row is None or not row.get("eligible"):
                    reason = row.get("reason") if row else "outside exact-node campaign"
                    return None, (jsonify({"ok": False, "error": f"rack {rack}: {reason}"}), 409)
                nodes.extend(str(node) for node in row.get("nodes", []) if str(node).strip())
            else:
                resolved = _rack_node_hostnames_from_inventory(rack)
                if not resolved:
                    return None, (jsonify({"ok": False, "error": f"no Slurm inventory nodes found for rack {rack}"}), 409)
                nodes.extend(resolved)
    nodes = _dedupe_hostnames_preserve_spelling(nodes)
    if not nodes:
        return None, (jsonify({"ok": False, "error": "nodes, target, rack, or racks is required"}), 400)
    inventory = _all_slurm_nodenames()
    if inventory:
        known = {node.casefold() for node in inventory}
        unknown = [node for node in nodes if node.casefold() not in known]
        if unknown:
            return None, (jsonify({"ok": False, "error": f"nodes not in current Slurm inventory: {', '.join(unknown)}"}), 409)
    try:
        states, ineligible = _validation_node_eligibility(nodes)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return None, (jsonify({"ok": False, "error": f"unable to verify exact-node scheduler eligibility: {exc}"}), 503)
    if ineligible:
        return None, (jsonify({"ok": False, "error": f"exact target is not idle/eligible: {', '.join(ineligible[:12])}"}), 409)

    script = Path(_scripts_dir()) / "run-llmb-nccl-container.sh"
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"container LLMB NCCL launcher not found: {script}"}), 500)
    cmd = ["bash", str(script), "--nodelist", ",".join(nodes), "--storage", storage,
           "--local-container-dir", local_dir, "--log-dir", str(log_dir)]
    cmd.append("--rsync" if rsync else "--no-rsync")
    dry_run = bool(body.get("dry_run"))
    if dry_run:
        cmd.append("--dry-run")
    scope = "node" if not racks else ("rack" if len(racks) == 1 else "multirack")
    return ({
        "workflow": "llmb-container-24test", "log_dir": log_dir, "racks": racks,
        "nodes": nodes, "node_states": states, "target": ",".join(nodes), "scope": scope,
        "storage": storage, "rsync_to_local": rsync, "cmd": cmd,
        "container_runtime": {"nccl": "2.29.3", "cuda": "13.1"},
        "recipe_contract": "job147392-shape (24 tests, 4 ranks/GPUs per node, PMIx)",
        "image_provenance": {
            "source": "/cluster/containers/nvidia+nemo+26.04.01-arm.sqsh",
            "size": 42245001216,
            "sha256": "55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc",
            "status": (
                "fleet-staged squashfs differs from the original job 147392 image digest; "
                "embedded NCCL 2.29.3 matches the proven runtime"
            ),
        },
        "pmix_gds": {"value": "hash", "status": "requested scale workaround; not a proven result claim"},
        "display_cmd": " ".join(shlex.quote(part) for part in cmd),
        "timeout": max(120, _job_env_int("RACK_LLMB_NCCL_CONTAINER_TIMEOUT", 43200)),
        "dry_run": dry_run,
        "env_updates": {**_launch_env_from_body(body), "NVIS_PREPARED_NODE_COUNT": str(len(nodes))},
        "campaign": campaign_id,
    }, None)


def _prepare_run_llmb_nccl(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Shared parse for the LLMB NCCL microbenchmark (MOP §7.3).

    Accepts EITHER a single ``rack`` (single-rack component) OR a list ``racks``
    (multi/cross-rack component). The wrapper (``run_llmb_nccl.sh``) takes NVIS rack
    ids and resolves the real Slurm nodes via ``sinfo`` itself, so we pass the
    canonical rack id(s) — not the ``-ct[...]`` validation target.
    """
    if os.environ.get("RACK_LLMB_NCCL_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (
            jsonify({"ok": False, "error": "NCCL Microbenchmark (LLMB) disabled (RACK_LLMB_NCCL_DISABLED)"}),
            403,
        )
    workflow = str(body.get("workflow") or "").strip().lower()
    if body.get("containerized") is True or workflow in ("container", "llmb-container", "llmb-container-24test"):
        return _prepare_run_llmb_nccl_container(body)
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)

    # Accept a single rack ("rack") or a list ("racks"); normalize + de-dupe.
    racks_in: list[str] = []
    racks_raw = body.get("racks")
    if racks_raw is not None:
        if not isinstance(racks_raw, list) or len(racks_raw) < 1:
            return None, (jsonify({"ok": False, "error": "racks must be a non-empty array of rack ids"}), 400)
        racks_in = [str(x) for x in racks_raw]
    else:
        single = (body.get("rack") or "").strip()
        if not single:
            return None, (jsonify({"ok": False, "error": "rack or racks is required"}), 400)
        racks_in = [single]

    rack_ids: list[str] = []
    seen: set[str] = set()
    for item in racks_in:
        try:
            rk = _normalize_rack_id(str(item).strip())
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)
        if rk in seen:
            continue
        seen.add(rk)
        rack_ids.append(rk)
    if not rack_ids:
        return None, (jsonify({"ok": False, "error": "no valid rack ids"}), 400)

    # When an exact-node campaign is configured it is authoritative: excluded or
    # unhealthy trays must never be silently reintroduced by rack-name expansion.
    try:
        campaign_snapshot = _multirack_campaign_snapshot()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return None, (jsonify({"ok": False, "error": f"exact-node campaign unavailable: {exc}"}), 503)
    campaign_id = str(campaign_snapshot.get("id") or "")
    requested_campaign = str(body.get("campaign") or "").strip()
    if requested_campaign and requested_campaign != campaign_id:
        return None, (jsonify({"ok": False, "error": "stale or unknown exact-node campaign id"}), 409)
    exact_nodes: list[str] = []
    if campaign_snapshot.get("active"):
        indexed = {
            str(row.get("rack") or ""): row
            for row in campaign_snapshot.get("racks", [])
            if isinstance(row, dict)
        }
        for rack in rack_ids:
            row = indexed.get(rack)
            if row is None:
                return None, (jsonify({"ok": False, "error": f"rack {rack} is outside exact-node campaign {campaign_id}"}), 409)
            if not row.get("eligible"):
                return None, (jsonify({"ok": False, "error": f"rack {rack}: {row.get('reason') or 'not eligible'}"}), 409)
            exact_nodes.extend(str(node) for node in row.get("nodes", []) if str(node).strip())

    # Multi-rack NCCL now runs the DIRECT path (run_nccl_mpi.sh → nccl_allreduce.sbatch:
    # host nccl-tests + mpirun, NCCL over the IB rails). This replaces the llmb-run
    # pyxis/enroot container, whose UCX could not see the RDMA verbs devices inside the
    # container ("network devices 'mlx5_*:1' are not available" → no active transport →
    # job killed). Set RACK_LLMB_NCCL_USE_CONTAINER=1 to fall back to the old container.
    use_container = _truthy_env("RACK_LLMB_NCCL_USE_CONTAINER")
    script = _run_llmb_nccl_script_path() if use_container else _run_nccl_mpi_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"NCCL launcher not found: {script}"}), 500)

    cmd: list[str] = ["bash", str(script)]
    reservation = (body.get("reservation") or _eget("RACK_LLMB_NCCL_RESERVATION") or "").strip()
    if reservation:
        cmd += ["--reservation", reservation]
    dry_run = bool(body.get("dry_run"))
    if dry_run:
        cmd.append("--dry-run")
    # Direct path only: cap the all_reduce sweep (nccl-tests -e). Default 16G so the
    # x-rack test exercises large messages; overridable per-request or via Settings.
    size_max = ""
    nccl_version = ""
    if not use_container:
        size_max = (body.get("size_max") or _eget("RACK_NCCL_MPI_SIZE_MAX") or "16G").strip()
        if size_max:
            cmd += ["--size-max", size_max]
        # NCCL version selector (direct path only): which vetted libnccl the sbatch loads
        # via LD_LIBRARY_PATH. Shared validator with the single-rack Stage-1 path.
        _vc = _nccl_version_cli(body)
        if isinstance(_vc, tuple):
            return None, _vc
        cmd += _vc
        nccl_version = _vc[1] if _vc else ""
    # Sweep rounds: honor an explicit ``iters`` from the launch modal; else leave the sbatch
    # default (1) so existing cross-rack behavior is unchanged when the knob isn't sent.
    rounds = _nccl_rounds_from_body(body, None) if not use_container else None
    if rounds:
        cmd += ["--rounds", str(rounds)]
    if exact_nodes:
        cmd += ["--nodelist", ",".join(exact_nodes)]
    else:
        cmd += rack_ids

    display_cmd = "./" + script.name + (
        (" --reservation " + shlex.quote(reservation)) if reservation else ""
    ) + (" --dry-run" if dry_run else "") + (
        (" --size-max " + shlex.quote(size_max)) if size_max else ""
    ) + (
        (" --nccl-version " + shlex.quote(nccl_version)) if nccl_version else ""
    ) + (
        (" --rounds " + str(rounds)) if rounds else ""
    ) + ((" --nodelist " + shlex.quote(",".join(exact_nodes))) if exact_nodes else (
        " " + " ".join(shlex.quote(r) for r in rack_ids)
    ))
    timeout = max(120, _job_env_int("RACK_LLMB_NCCL_TIMEOUT", 14400))
    return (
        {
            "log_dir": log_dir,
            "racks": rack_ids,
            "campaign": campaign_id if exact_nodes else "",
            "nodes": exact_nodes,
            "cmd": cmd,
            "display_cmd": display_cmd,
            "timeout": timeout,
            "dry_run": dry_run,
            "env_updates": _launch_env_from_body(body),
        },
        None,
    )


@bp.route("/api/validation-presets", methods=["GET"])
@action(get="validation:list")
def api_validation_presets():
    return jsonify({"ok": True, "presets": validation_presets.catalogue()})


@bp.route("/api/validation-presets/prepare", methods=["POST"])
@action("validation:prepare_preset")
def api_prepare_validation_preset():
    body = request.get_json(silent=True) or {}
    compatibility_error = _validation_preset_compatibility_error(body)
    if compatibility_error is not None:
        return compatibility_error
    plan, err = _prepare_validation_preset(body)
    if err is not None:
        return err
    assert plan is not None
    public_plan = {key: value for key, value in plan.items() if key not in ("cmd", "env_updates", "timeout", "log_dir")}
    return jsonify({"ok": True, "plan": public_plan})


# The legacy counterpart of the preset `prepare` above (REQ-6). One endpoint for every legacy
# launch rather than a sibling route each: they share a prepare signature and a result shape, so
# one route per kind would be one more chance for each to drift.
_PREPARE_BY_KIND.update({
    "validation": _prepare_run_validation,
    "nemo-xrack": _prepare_run_nemo_xrack,
    "exemplar-llama": _prepare_run_exemplar_llama,
    "llama": _prepare_run_llama,
    "mnubergemm-stress": _prepare_run_mnubergemm_stress,
    "nccl-fullrack": _prepare_run_nccl_fullrack,
    "nvloom": _prepare_run_nvloom_singlerack,
    "clusterkit": _prepare_run_clusterkit_singlerack,
    "llmb-nccl": _prepare_run_llmb_nccl,
})

_CATALOG_TOOL_BY_KIND = {
    "exemplar-llama": "preset-llama405b",
    "llama": "llama",
    "nemo-xrack": "nemo-xrack",
    "validation": "validation-singlerack",
}


@bp.route("/api/launch/prepare", methods=["POST"])
@action("validation:prepare_launch")
def api_prepare_launch():
    """Resolve a legacy launch for review and return its digest, without launching anything.

    ``{"kind": "<one of _PREPARE_BY_KIND>", ...}`` — the rest of the body is the launch body the
    caller intends to send, so the digest is computed over exactly what would run.

    What comes back is the resolved command, the resolved target and a ``plan_hash``. The command is
    the honest answer to "what will this do": for these endpoints the operator previously reviewed
    only the options they had typed, never the resolved argv, so nothing detected a target that had
    changed underneath them.

    Deliberately does not launch, and deliberately does not stash the plan server-side. A stored
    plan would need an expiry, a cleanup path and a per-operator scope, and would still not answer
    the real question, which is whether the target is the same *now*. The digest costs nothing to
    recompute and cannot go stale in a way the compare misses.
    """
    body = request.get_json(silent=True) or {}
    kind = str(body.get("kind") or "").strip()
    prepare = _PREPARE_BY_KIND.get(kind)
    if prepare is None:
        return jsonify({
            "ok": False,
            "error": f"unknown launch kind {kind!r}",
            "kinds": sorted(_PREPARE_BY_KIND),
        }), 400

    launch_body = {k: v for k, v in body.items() if k != "kind"}
    tool_id = _CATALOG_TOOL_BY_KIND.get(kind)
    if tool_id:
        compatibility_error = _catalog_tool_compatibility_error(tool_id)
        if compatibility_error is not None:
            return compatibility_error
    prep, err = prepare(launch_body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    return jsonify({
        "ok": True,
        "kind": kind,
        "plan": {
            "target": str(prep.get("target") or ""),
            "rack": str(prep.get("rk") or prep.get("rack") or ""),
            "command_display": str(prep.get("display_cmd") or ""),
            "log_dir": str(prep.get("log_dir") or ""),
            "timeout_sec": prep.get("timeout"),
        },
        "plan_hash": launch_digest(prep),
    })


def _run_validation_preset_async(body: dict):
    compatibility_error = _validation_preset_compatibility_error(body)
    if compatibility_error is not None:
        return compatibility_error
    plan, err = _prepare_validation_preset(body)
    if err is not None:
        return err
    assert plan is not None
    supplied_hash = str(body.get("plan_hash") or "").strip()
    if not supplied_hash:
        return jsonify({"ok": False, "error": "plan_hash from /api/validation-presets/prepare is required"}), 409
    if not secrets.compare_digest(supplied_hash, str(plan["plan_hash"])):
        return jsonify({"ok": False, "error": "validation plan changed; prepare and review it again", "plan_hash": plan["plan_hash"]}), 409
    job_id = str(uuid.uuid4())
    operator = _operator_from_request()
    tool = {
        "nccl-2.31.2": "nccl-fullrack", "llama31-405b-fp8": "llama-exemplar",
        "gpt-oss-cpu": "external", "exemplar-system-info": "external",
        "nemo-26.04.01": "nemo-xrack", "mnubergemm-26.03-v1.17": "mnubergemm",
    }[str(plan["preset_id"])]
    racks = [str(rack) for rack in plan["racks"]]
    _record_validation_activity(tool, stage="other", scope=str(plan["scope"]),
        rack=racks[0] if racks else "", racks=racks, log_dir=str(plan["log_dir"]),
        command_display=str(plan["command_display"]), activity_id=job_id, operator=operator)
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id, "kind": "validation-preset", "preset_id": plan["preset_id"],
            "operator": operator, "racks": racks, "nodes": plan["nodes"], "node_count": plan["node_count"],
            "log_dir": plan["log_dir"], "phase": "starting", "command_display": plan["command_display"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    threading.Thread(target=_stress_worker,
        args=(job_id, plan["cmd"], plan["log_dir"], plan["timeout"], plan["command_display"]),
        kwargs={"error_label": "validation preset launcher", "env_updates": plan["env_updates"],
                "profile": "script", "persist": True}, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "preset_id": plan["preset_id"],
                    "nodes": plan["nodes"], "node_count": plan["node_count"],
                    "command_display": plan["command_display"], "timeout_sec": plan["timeout"]})


@bp.route("/api/run-validation-preset-async", methods=["POST"])
@action("validation:run_preset", variant="async")
def api_run_validation_preset_async():
    return _run_validation_preset_async(request.get_json(silent=True) or {})


@bp.route("/api/run-exemplar-sysinfo-async", methods=["POST"])
@action("validation:run_exemplar_sysinfo", variant="async")
def api_run_exemplar_sysinfo_async():
    body = request.get_json(silent=True) or {}
    body["preset_id"] = "exemplar-system-info"
    return _run_validation_preset_async(body)


@bp.route("/api/run-validation-preset-status", methods=["GET"])
@action(get="job:list")
def api_run_validation_preset_status():
    return _validation_preset_status_payload((request.args.get("job_id") or "").strip())


@bp.route("/api/run-exemplar-sysinfo-status", methods=["GET"])
@action(get="job:list")
def api_run_exemplar_sysinfo_status():
    return _validation_preset_status_payload((request.args.get("job_id") or "").strip())


@bp.route("/api/run-mnubergemm-stress-async", methods=["POST"])
@action("validation:run_mnubergemm", variant="async")
def api_run_mnubergemm_stress_async():
    """Start mnubergemm stress driver; poll GET /api/run-mnubergemm-stress-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepared(_prepare_run_mnubergemm_stress, body, kind="mnubergemm-stress")
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    rk = str(prep["rk"])
    target = str(prep["target"])
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) else None
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    _record_validation_activity(
        "mnubergemm",
        stage="other",
        scope="rack",
        rack=rk,
        racks=[rk],
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "mnubergemm",
            "operator": operator,
            "rack": rk,
            "target": target,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_stress_worker,
        args=(job_id, cmd, str(log_dir), timeout, display_cmd),
        kwargs={"env_updates": env_merged, "persist": True},
        daemon=True,
    ).start()
    client_poll_deadline_ms = min(int(timeout) * 1000 + 600000, 48 * 3600 * 1000)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rk,
            "target": target,
            "command_display": display_cmd,
            "timeout_sec": timeout,
            "client_poll_deadline_ms": client_poll_deadline_ms,
        }
    )


@bp.route("/api/run-nccl-fullrack-async", methods=["POST"])
@action("validation:run_nccl", variant="async")
def api_run_nccl_fullrack_async():
    """Start the standalone Stage-1 NCCL benchmark; poll GET /api/run-nccl-fullrack-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepared(_prepare_run_nccl_fullrack, body, kind="nccl-fullrack")
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    rk = str(prep["rk"])
    target = str(prep["target"])
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) else None
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    scope, scope_nodes = launch_scope_for_target(target)
    _record_validation_activity(
        "nccl-fullrack",
        stage="stage1",
        scope=scope,
        nodes=scope_nodes,
        rack=rk,
        racks=[rk],
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    if env_merged is None:
        env_merged = {}
    env_merged = {**env_merged, "NVIS_ACTIVITY_ID": job_id}
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nccl-fullrack",
            "operator": operator,
            "rack": rk,
            "target": target,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_stress_worker,
        args=(job_id, cmd, str(log_dir), timeout, display_cmd),
        kwargs={
            "error_label": "nccl benchmark script",
            "env_updates": env_merged,
            "profile": "script",
            "persist": True,
        },
        daemon=True,
    ).start()
    client_poll_deadline_ms = min(int(timeout) * 1000 + 600000, 48 * 3600 * 1000)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rk,
            "target": target,
            "command_display": display_cmd,
            "timeout_sec": timeout,
            "client_poll_deadline_ms": client_poll_deadline_ms,
        }
    )


@bp.route("/api/run-nvloom-async", methods=["POST"])
@action("validation:run_nvloom", variant="async")
def api_run_nvloom_async():
    """Start nvloom singlerack Slurm driver (sbatch --wait); poll GET /api/run-nvloom-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepared(_prepare_run_nvloom_singlerack, body, kind="nvloom")
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    rk = str(prep["rk"])
    target = str(prep["target"])
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) else None
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    _record_validation_activity(
        "nvloom",
        stage="other",
        scope="rack",
        rack=rk,
        racks=[rk],
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "nvloom",
            "operator": operator,
            "rack": rk,
            "target": target,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_stress_worker,
        args=(job_id, cmd, str(log_dir), timeout, display_cmd),
        kwargs={"error_label": "nvloom singlerack script", "env_updates": env_merged, "persist": True},
        daemon=True,
    ).start()
    client_poll_deadline_ms = min(int(timeout) * 1000 + 600000, 48 * 3600 * 1000)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rk,
            "target": target,
            "command_display": display_cmd,
            "timeout_sec": timeout,
            "client_poll_deadline_ms": client_poll_deadline_ms,
        }
    )


@bp.route("/api/run-clusterkit-async", methods=["POST"])
@action("validation:run_clusterkit", variant="async")
def api_run_clusterkit_async():
    """Start ClusterKit singlerack Slurm driver; poll GET /api/run-clusterkit-status?job_id=…"""
    body = request.get_json(silent=True) or {}
    prep, err = _prepared(_prepare_run_clusterkit_singlerack, body, kind="clusterkit")
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    rk = str(prep["rk"])
    target = str(prep["target"])
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) else None
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    _record_validation_activity(
        "clusterkit",
        stage="other",
        scope="rack",
        rack=rk,
        racks=[rk],
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
    )
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "clusterkit",
            "operator": operator,
            "rack": rk,
            "target": target,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_stress_worker,
        args=(job_id, cmd, str(log_dir), timeout, display_cmd),
        kwargs={"error_label": "clusterkit script", "env_updates": env_merged, "persist": True},
        daemon=True,
    ).start()
    client_poll_deadline_ms = min(int(timeout) * 1000 + 600000, 48 * 3600 * 1000)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rk,
            "target": target,
            "command_display": display_cmd,
            "timeout_sec": timeout,
            "client_poll_deadline_ms": client_poll_deadline_ms,
        }
    )


@bp.route("/api/run-llmb-nccl-async", methods=["POST"])
@action("validation:run_llmb_nccl", variant="async")
def api_run_llmb_nccl_async():
    """Start the LLMB NCCL microbenchmark (single rack via ``rack`` or cross-rack via
    ``racks``); poll GET /api/run-llmb-nccl-status?job_id=…

    Process cwd is *log_dir* (like clusterkit / nemo-xrack) so Slurm batch stdout lands
    under the UI log dir. Runs on the privileged ``script`` profile (Slurm + llmb-run on
    the head node).
    """
    body = request.get_json(silent=True) or {}
    prep, err = _prepared(_prepare_run_llmb_nccl, body, kind="llmb-nccl")
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    log_dir = prep["log_dir"]
    racks = [str(r) for r in prep["racks"]]
    cmd = prep["cmd"]
    display_cmd = str(prep["display_cmd"])
    timeout = int(prep["timeout"])
    dry_run = bool(prep["dry_run"])
    workflow = str(prep.get("workflow") or "direct-host")
    nodes = [str(node) for node in prep.get("nodes", [])]
    _eu = prep.get("env_updates")
    env_merged = {str(k): str(v) for k, v in _eu.items()} if isinstance(_eu, dict) and _eu else None
    rack_label = racks[0] if len(racks) == 1 else (f"{len(racks)} racks" if racks else f"{len(nodes)} nodes")
    cwd = _host_job_cwd(log_dir)
    job_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    operator = _operator_from_request()
    scope = str(prep.get("scope") or ("multirack" if len(racks) > 1 else "rack"))
    _record_validation_activity(
        "llmb-nccl",
        stage="stage1",
        scope=scope,
        rack=racks[0] if racks else rack_label,
        racks=racks,
        log_dir=str(log_dir),
        command_display=display_cmd,
        activity_id=job_id,
        operator=operator,
        dry_run=dry_run,
    )
    if env_merged is None:
        env_merged = {}
    env_merged = {**env_merged, "NVIS_ACTIVITY_ID": job_id}
    with _stress_jobs_lock:
        _stress_jobs[job_id] = {
            "job_id": job_id,
            "kind": "llmb-nccl",
            "operator": operator,
            "rack": rack_label,
            "racks": racks,
            "nodes": nodes,
            "workflow": workflow,
            "dry_run": dry_run,
            "log_dir": str(log_dir),
            "phase": "starting",
            "command_display": display_cmd,
            "created_at": created,
        }
    threading.Thread(
        target=_stress_worker,
        args=(job_id, cmd, cwd, timeout, display_cmd),
        kwargs={
            "error_label": "run_llmb_nccl script",
            "profile": "script",
            "persist": True,
            "env_updates": env_merged,
        },
        daemon=True,
    ).start()
    client_poll_deadline_ms = min(int(timeout) * 1000 + 600000, 48 * 3600 * 1000)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "rack": rack_label,
            "racks": racks,
            "nodes": nodes,
            "workflow": workflow,
            "command_display": display_cmd,
            "timeout_sec": timeout,
            "client_poll_deadline_ms": client_poll_deadline_ms,
        }
    )
