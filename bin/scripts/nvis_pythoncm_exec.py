#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Out-of-process PythonCM parallel execution bridge for Rack Status.

This script runs on the BCM head with BCM's bundled interpreter.  Its stdout is
a one-line JSON protocol; PythonCM logs and diagnostics belong on stderr.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import shlex
import sys
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_MODULE_PATHS = (
    "/cm/local/apps/cmd/pythoncm",
    "/cm/local/apps/python3/lib/python3*/site-packages",
)
BUSY_STATES = {0, 1, 2, 13, 16}
FAILED_STATES = {3, 4, 5, 6, 7, 8, 10, 12, 14, 15}
KILLED_STATE = 9
_CONNECTION_OPTIONS: dict[str, Any] = {}


def _response(ok: bool, **fields: Any) -> int:
    print(json.dumps({"ok": ok, **fields}, separators=(",", ":"), sort_keys=True))
    return 0 if ok else 2


def _resolve_pythoncm() -> str | None:
    candidates = list(_CONNECTION_OPTIONS.get("module_path") or [])
    configured = os.environ.get("NVIS_PYTHONCM_PATH", "").strip()
    if configured:
        candidates.append(configured)
    candidates.extend(DEFAULT_MODULE_PATHS)
    for raw in candidates:
        for base in sorted(glob.glob(os.path.expanduser(raw)), reverse=True):
            choices = (base, *sorted(glob.glob(f"{base}/lib/python3*/site-packages"), reverse=True))
            for path in choices:
                if os.path.isdir(os.path.join(path, "pythoncm")):
                    sys.path.insert(0, path)
                    return path
    return None


def _cluster():
    if not _resolve_pythoncm():
        raise RuntimeError("pythoncm not found on BCM head")
    from pythoncm.cluster import Cluster
    from pythoncm.settings import Settings

    cookie = _secret("cookie_file", "NVIS_BCM_COOKIE")
    username = str(
        _CONNECTION_OPTIONS.get("username") or os.environ.get("NVIS_BCM_USERNAME", "")
    ).strip()
    password = _secret("password_file", "NVIS_BCM_PASSWORD")
    if not cookie and (not username or not password):
        raise PermissionError(
            "PythonCM execution needs --cookie-file/NVIS_BCM_COOKIE or a username "
            "plus --password-file/NVIS_BCM_PASSWORD"
        )
    settings = Settings(
        host=_CONNECTION_OPTIONS.get("host") or os.environ.get("NVIS_BCM_HOST", "127.0.0.1"),
        port=int(_CONNECTION_OPTIONS.get("port") or os.environ.get("NVIS_BCM_PORT", "8081")),
        cert_file=os.path.expanduser(
            _CONNECTION_OPTIONS.get("cert") or os.environ.get("NVIS_BCM_CERT", "~/.cm/cert.pem")
        ),
        key_file=os.path.expanduser(
            _CONNECTION_OPTIONS.get("key") or os.environ.get("NVIS_BCM_KEY", "~/.cm/cert.key")
        ),
        ca_file=os.path.expanduser(
            _CONNECTION_OPTIONS.get("ca")
            or os.environ.get("NVIS_BCM_CA", "/cm/local/apps/cmd/etc/cacert.pem")
        ),
        check_hostname=False,
    )
    if cookie:
        settings.cookie = cookie
    else:
        if not settings.get_cookie(username, password):
            raise PermissionError("PythonCM login failed for configured NVIS_BCM_USERNAME")
    return Cluster(settings)


def _secret(option: str, env_name: str) -> str:
    """Read a secret file, with the remote process environment as compatibility fallback."""
    path = str(_CONNECTION_OPTIONS.get(option) or "").strip()
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            label = option.replace("_", " ")
            raise PermissionError(f"cannot read PythonCM {label}: {exc}") from exc
    return os.environ.get(env_name, "").strip()


def _decode_payload(encoded: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid submit payload: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError("submit payload must be a JSON object")
    return value


def _manifest_path(jobs_dir: str, task_id: str) -> Path:
    return Path(jobs_dir) / task_id / "job.json"


def _load(jobs_dir: str, task_id: str) -> dict[str, Any]:
    path = _manifest_path(jobs_dir, task_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        # Public handles are ProgramRunner UUIDs.  The private task directory id
        # is generated just before submit because its path is embedded in the
        # remote command; resolve either form after a process restart.
        value = None
        for candidate in Path(jobs_dir).glob("*/job.json"):
            try:
                row = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(row, dict) and str(row.get("pythoncm_id") or "") == task_id:
                value = row
                break
        if value is None:
            raise LookupError(f"unknown PythonCM task {task_id}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"invalid task manifest {path}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _expand_nodelist(spec: str) -> list[str]:
    """Expand the single-bracket hostlists emitted by supported BCM clusters."""
    import re

    parts, current, depth = [], [], 0
    for char in spec or "":
        depth += char == "["
        depth -= char == "]"
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    result = []
    for token in (part.strip() for part in parts if part.strip()):
        match = re.match(r"^([^\[\]]+)\[([^\]]+)\]([^\[\]]*)$", token)
        if not match:
            result.append(token)
            continue
        prefix, inner, suffix = match.groups()
        for piece in inner.split(","):
            rng = re.match(r"^(\d+)-(\d+)$", piece.strip())
            if not rng:
                result.append(f"{prefix}{piece.strip()}{suffix}")
                continue
            low, high = rng.groups()
            if int(high) < int(low) or int(high) - int(low) > 10000:
                raise ValueError(f"invalid or excessive node range {piece}")
            result.extend(
                f"{prefix}{value:0{len(low)}d}{suffix}"
                for value in range(int(low), int(high) + 1)
            )
    return result


def _resolve_nodes(cluster, nodelist: str) -> tuple[list[Any], list[str]]:
    names = _expand_nodelist(nodelist)
    if not names:
        head = cluster.active_head_node()
        return ([head] if head is not None else []), []
    found, missing = [], []
    for name in names:
        node = cluster.get_by_name(name)
        (missing if node is None else found).append(name if node is None else node)
    return found, missing


def _wrapped_command(command: str, task_dir: Path) -> str:
    # The task directory must be shared between head and compute nodes (the BCM
    # default).  Per-node files avoid interleaved writes from a full rack.
    qdir = shlex.quote(str(task_dir))
    return (
        "_nvis_node=$(hostname -s); "
        f"_nvis_dir={qdir}; "
        "_nvis_out=\"${_nvis_dir}/${_nvis_node}.stdout\"; "
        "_nvis_err=\"${_nvis_dir}/${_nvis_node}.stderr\"; "
        "_nvis_status=\"${_nvis_dir}/${_nvis_node}.status\"; "
        "{ set +e; ( " + command + " ); _nvis_rc=$?; "
        "printf '%s\\n' \"$_nvis_rc\" > \"${_nvis_status}.tmp.$$\"; "
        "mv -f \"${_nvis_status}.tmp.$$\" \"$_nvis_status\"; "
        "exit \"$_nvis_rc\"; "
        "} >\"$_nvis_out\" 2>\"$_nvis_err\""
    )


def _submit(payload: dict[str, Any], cluster=None) -> dict[str, Any]:
    command = str(payload.get("command") or "").strip()
    script = str(payload.get("script") or "").strip()
    if not command and script:
        command = f"bash {shlex.quote(script)}"
    if not command:
        raise ValueError("submit needs a script or command")
    jobs_dir = str(payload.get("jobs_dir") or "/var/tmp/nvis-pythoncm-jobs")
    task_id = str(uuid.uuid4())
    task_dir = Path(jobs_dir) / task_id
    owns_cluster = cluster is None
    cluster = cluster or _cluster()
    try:
        nodes, missing = _resolve_nodes(cluster, str(payload.get("nodelist") or ""))
        if missing:
            raise ValueError("unknown BCM node(s): " + ", ".join(missing[:20]))
        if not nodes:
            raise ValueError("no BCM target nodes resolved")
        launch_mode = str(payload.get("launch_mode") or "fanout").strip().lower()
        if launch_mode not in {"fanout", "leader", "head"}:
            raise ValueError("launch_mode must be fanout, leader, or head")
        if launch_mode == "head":
            head = cluster.active_head_node()
            if head is None:
                raise RuntimeError("BCM returned no active head node for head launch")
            execution_nodes = [head]
        else:
            execution_nodes = nodes[:1] if launch_mode == "leader" else nodes
        task_dir.mkdir(parents=True, exist_ok=False)
        env = {str(k): str(v) for k, v in (payload.get("env") or {}).items() if v is not None}
        env.update({
            "NVIS_TASK_ID": task_id,
            "NVIS_NODELIST": ",".join(str(getattr(n, "hostname", "")) for n in nodes),
            "NVIS_NODE_COUNT": str(len(nodes)),
        })
        if payload.get("ntasks_per_node"):
            env["NVIS_NTASKS_PER_NODE"] = str(payload["ntasks_per_node"])
        execution_user = str(
            payload.get("execution_user")
            or os.environ.get("RACK_PYTHONCM_USER", "root")
        ).strip()
        if not execution_user:
            raise ValueError("PythonCM execution user is empty")
        _success, status = cluster.parallel.execute(
            nodes=execution_nodes,
            command=_wrapped_command(command, task_dir),
            env=env,
            user=execution_user,
            max_run_time=int(payload.get("timeout") or 1800),
            wait=False,
            info=f"Rack Status: {payload.get('name') or task_id}",
        )
        manifest = {
            "task_id": task_id,
            "pythoncm_id": str(status.uuid),
            "nodes": [str(getattr(node, "hostname", "")) for node in nodes],
            "execution_nodes": [str(getattr(node, "hostname", "")) for node in execution_nodes],
            "node_uuids": [str(node.uuid) for node in execution_nodes],
            "launch_mode": launch_mode,
            "execution_user": execution_user,
            "name": str(payload.get("name") or "nvis-job"),
            "output": str(payload.get("output") or ""),
            "cancelled": False,
            "created_at": time.time(),
        }
        _atomic_json(_manifest_path(jobs_dir, task_id), manifest)
        return {"task_id": task_id, "pythoncm_id": str(status.uuid), "nodes": manifest["nodes"]}
    finally:
        if owns_cluster:
            cluster.disconnect()


def _node_results(manifest: dict[str, Any], jobs_dir: str) -> list[dict[str, Any]]:
    task_dir = Path(jobs_dir) / str(manifest["task_id"])
    rows = []
    for name in manifest.get("execution_nodes") or manifest.get("nodes") or []:
        status_path = task_dir / f"{name}.status"
        try:
            rc = int(status_path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError):
            rc = None
        rows.append({"node": name, "returncode": rc})
    return rows


def _active_status(manifest: dict[str, Any], cluster=None):
    owns_cluster = cluster is None
    cluster = cluster or _cluster()
    try:
        from uuid import UUID

        statuses = cluster.parallel.active_command_status(ids=[UUID(manifest["pythoncm_id"])])
        if not statuses:
            return None
        status = statuses[0]
        states = [
            status.get_state(UUID(str(node)))[0]
            for node in manifest.get("node_uuids") or []
        ]
        if any(state == KILLED_STATE for state in states):
            return "CANCELLED"
        if any(state in FAILED_STATES for state in states):
            return "FAILED"
        return "RUNNING" if any(state in BUSY_STATES for state in states) else "PENDING"
    finally:
        if owns_cluster:
            cluster.disconnect()


def _state(
    manifest: dict[str, Any], jobs_dir: str, cluster=None
) -> tuple[str, list[dict[str, Any]]]:
    rows = _node_results(manifest, jobs_dir)
    if manifest.get("cancelled"):
        return "CANCELLED", rows
    if rows and all(row["returncode"] is not None for row in rows):
        return ("COMPLETED" if all(row["returncode"] == 0 for row in rows) else "FAILED"), rows
    active = _active_status(manifest, cluster=cluster)
    return (active or "UNKNOWN"), rows


def _cancel(manifest: dict[str, Any], jobs_dir: str, cluster=None) -> None:
    owns_cluster = cluster is None
    cluster = cluster or _cluster()
    try:
        from uuid import UUID

        statuses = cluster.parallel.active_command_status(ids=[UUID(manifest["pythoncm_id"])])
        for status in statuses:
            status.kill()
    finally:
        if owns_cluster:
            cluster.disconnect()
    manifest["cancelled"] = True
    _atomic_json(_manifest_path(jobs_dir, str(manifest["task_id"])), manifest)


def _collect(manifest: dict[str, Any], jobs_dir: str) -> tuple[str, str]:
    task_dir = Path(jobs_dir) / str(manifest["task_id"])
    stdout, stderr = [], []
    for node in manifest.get("execution_nodes") or manifest.get("nodes") or []:
        for suffix, target in (("stdout", stdout), ("stderr", stderr)):
            path = task_dir / f"{node}.{suffix}"
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue
            if text:
                target.append(f"===== {node} =====\n{text.rstrip()}\n")
    return "".join(stdout), "".join(stderr)


def build_parser() -> argparse.ArgumentParser:
    """Return the execution parser shared by the CLI and persistent broker."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("submit", "status", "cancel", "result", "probe"))
    parser.add_argument("--payload", default="")
    parser.add_argument("--jobs-dir", default="/var/tmp/nvis-pythoncm-jobs")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--module-path", action="append", default=[])
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int)
    parser.add_argument("--cert", default="")
    parser.add_argument("--key", default="")
    parser.add_argument("--ca", default="")
    parser.add_argument("--username", default="")
    parser.add_argument("--password-file", default="")
    parser.add_argument("--cookie-file", default="")
    return parser


def execute(args: argparse.Namespace, cluster=None) -> dict[str, Any]:
    """Execute one helper action, optionally through an already-connected cluster."""
    global _CONNECTION_OPTIONS
    _CONNECTION_OPTIONS = vars(args)
    owned_cluster = None
    try:
        if args.action == "probe":
            if cluster is None:
                owned_cluster = _cluster()
                cluster = owned_cluster
            # This call is intentionally part of readiness: cert-only sessions
            # can query entities but fail cmproc with "No valid cookie".
            cluster.parallel.active_command_status()
            return {"ok": True, "ready": True}
        if args.action == "submit":
            return {"ok": True, **_submit(_decode_payload(args.payload), cluster=cluster)}
        manifest = _load(args.jobs_dir, args.task_id)
        if args.action == "cancel":
            _cancel(manifest, args.jobs_dir, cluster=cluster)
            return {"ok": True, "task_id": args.task_id, "state": "CANCELLED"}
        state, rows = _state(manifest, args.jobs_dir, cluster=cluster)
        if args.action == "status":
            return {"ok": True, "task_id": args.task_id, "state": state, "nodes": rows}
        stdout, stderr = _collect(manifest, args.jobs_dir)
        returncode = 0 if state == "COMPLETED" else 1
        return {
            "ok": True,
            "task_id": args.task_id,
            "state": state,
            "returncode": returncode,
            "stdout_b64": base64.b64encode(stdout.encode()).decode("ascii"),
            "stderr_b64": base64.b64encode(stderr.encode()).decode("ascii"),
            "output": manifest.get("output", ""),
            "nodes": rows,
        }
    except PermissionError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "hint": "configure a BCM login/cookie with cmproc permission",
        }
    except Exception as exc:  # noqa: BLE001 - preserve JSON protocol for every PythonCM failure
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if owned_cluster is not None:
            try:
                owned_cluster.disconnect()
            except Exception:  # noqa: BLE001,S110 - cannot mask the protocol response
                pass


def main(argv: list[str] | None = None) -> int:
    result = execute(build_parser().parse_args(argv))
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
