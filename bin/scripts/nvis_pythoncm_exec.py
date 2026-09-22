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
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""nvis_pythoncm_exec.py — run work through BCM's ProgramRunner, on the head node.

This is the other half of `backend/schedulers/pythoncm.py`, and the **only** code in the
product that imports `pythoncm`. It runs on the BCM head under the head's own interpreter,
because that library is generated against a specific CMDaemon release and Greenlight serves
several clusters at once.

Contract, in both directions:

- **In:** one action (`submit`, `status`, `cancel`, `result`, `probe`), plus a base64url JSON
  payload for `submit`.
- **Out:** exactly one JSON object on the last line of stdout, always with `ok`. Exit 0 when
  `ok` is true, 2 otherwise. Never anything else on stdout -- the caller reads the last JSON
  line specifically so a login banner cannot corrupt the answer.

**It keeps its own durable state, and that is not optional.** CMDaemon drops a parallel
command from its active status once it finishes, so a result read a minute later would find
nothing at all. Each task therefore owns a directory holding a manifest and one
stdout/stderr/status triple per node, written by the command itself. That directory is what
`sacct` is for Slurm: the answer to "how did it go" after the fact.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import shlex
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

#: Where BCM installs its Python client, newest first. Searched only when the caller did not
#: say; a site that puts it elsewhere passes --module-path.
DEFAULT_MODULE_PATHS = (
    "/cm/local/apps/cmd/lib/python3*/site-packages",
    "/cm/local/apps/python3*/lib/python3*/site-packages",
    "/cm/shared/apps/cmd/lib/python3*/site-packages",
)

DEFAULT_JOBS_DIR = "/var/tmp/nvis-pythoncm-jobs"

#: CMDaemon's vocabulary, mapped onto the scheduler's. The right-hand side must stay inside
#: `schedulers.base.TERMINAL_STATES` or a finished job polls forever.
_STATE_MAP = {
    "PENDING": "PENDING",
    "RUNNING": "RUNNING",
    "COMPLETED": "COMPLETED",
    "FINISHED": "COMPLETED",
    "FAILED": "FAILED",
    "ERROR": "FAILED",
    "CANCELLED": "CANCELLED",
    "KILLED": "CANCELLED",
    "TIMEOUT": "TIMEOUT",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------------------
# pythoncm access
# ---------------------------------------------------------------------------------------

def _resolve_pythoncm(extra_path: str = "") -> None:
    """Put `pythoncm` on `sys.path`, searching BCM's usual locations."""
    candidates = [extra_path] if extra_path else []
    for pattern in DEFAULT_MODULE_PATHS:
        candidates.extend(sorted(glob.glob(pattern), reverse=True))
    for candidate in candidates:
        if candidate and os.path.isdir(os.path.join(candidate, "pythoncm")):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return
    # Not fatal here: the import below may still succeed from a site-packages we did not
    # guess, and its ImportError is a better error message than ours would be.


def _secret(path: str, env_name: str) -> str:
    """A credential from a file, falling back to this process's environment.

    File first on purpose. A password passed on argv is visible to every user on the head in
    `ps`, so the scheduler sends only the *path*; the environment fallback exists for an
    operator running this by hand.
    """
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read credential file {path}: {exc}") from exc
    return (os.environ.get(env_name) or "").strip()


def _cluster(args):
    """Connect to CMDaemon and hand back a live `Cluster`."""
    _resolve_pythoncm(args.module_path)
    try:
        from pythoncm.cluster import Cluster
        from pythoncm.settings import Settings
    except ImportError as exc:  # pragma: no cover - depends on the head's install
        raise RuntimeError(
            f"pythoncm is not importable on this host ({exc}); "
            "pass --module-path or install BCM's Python client"
        ) from exc

    home = os.path.expanduser("~")
    settings = Settings(
        host=args.host or os.environ.get("NVIS_BCM_HOST") or "localhost",
        port=int(args.port or os.environ.get("NVIS_BCM_PORT") or 8081),
        cert_file=args.cert or f"{home}/.cm/cert.pem",
        key_file=args.key or f"{home}/.cm/cert.key",
        ca_file=args.ca or "/cm/local/apps/cmd/etc/cacert.pem",
    )
    cookie = _secret(args.cookie_file, "NVIS_BCM_COOKIE")
    if cookie:
        settings.cookie = cookie
    elif args.username:
        settings.username = args.username
        settings.password = _secret(args.password_file, "NVIS_BCM_PASSWORD")
    return Cluster(settings)


# ---------------------------------------------------------------------------------------
# node lists
# ---------------------------------------------------------------------------------------

_BRACKET = re.compile(r"^(?P<prefix>.*?)\[(?P<body>[0-9,\-]+)\](?P<suffix>.*)$")


def expand_nodelist(nodelist: str) -> list[str]:
    """Expand Slurm-style bracket notation: `n[01-03],n07` -> `n01 n02 n03 n07`.

    Written out rather than shelled to `scontrol show hostnames`: that binary is exactly what
    a PythonCM cluster does not have. Zero padding is taken from the literal the site wrote,
    because `n01` and `n1` are different hostnames.
    """
    out: list[str] = []
    for chunk in _split_top_level(nodelist):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = _BRACKET.match(chunk)
        if not match:
            out.append(chunk)
            continue
        prefix, body, suffix = match.group("prefix", "body", "suffix")
        for part in body.split(","):
            part = part.strip()
            if "-" in part:
                lo_s, hi_s = part.split("-", 1)
                width = len(lo_s)
                for n in range(int(lo_s), int(hi_s) + 1):
                    out.append(f"{prefix}{n:0{width}d}{suffix}")
            elif part:
                out.append(f"{prefix}{part}{suffix}")
    # Order is the site's, deduplicated: a repeated node would run the command twice.
    return list(dict.fromkeys(out))


def _split_top_level(nodelist: str) -> list[str]:
    """Split on commas that are not inside brackets."""
    parts, depth, current = [], 0, []
    for ch in nodelist or "":
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return parts


# ---------------------------------------------------------------------------------------
# durable task state
# ---------------------------------------------------------------------------------------

def _task_dir(jobs_dir: str, task_id: str) -> Path:
    return Path(jobs_dir) / task_id


def _manifest_path(jobs_dir: str, task_id: str) -> Path:
    return _task_dir(jobs_dir, task_id) / "job.json"


def _write_manifest(jobs_dir: str, task_id: str, manifest: dict) -> None:
    path = _manifest_path(jobs_dir, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load(jobs_dir: str, task_id: str) -> dict:
    """Find a task by our id or by the ProgramRunner's.

    The caller holds the ProgramRunner uuid, because that is the one an operator can look up
    with BCM's own tooling. Our task id is private, so a lookup by the public handle has to
    fall back to scanning the manifests.
    """
    direct = _manifest_path(jobs_dir, task_id)
    if direct.is_file():
        return json.loads(direct.read_text(encoding="utf-8"))
    for candidate in sorted(Path(jobs_dir).glob("*/job.json")):
        try:
            manifest = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(manifest.get("pythoncm_id") or "") == task_id:
            return manifest
    raise RuntimeError(f"no PythonCM task matching {task_id!r} under {jobs_dir}")


def _wrapped_command(command: str, task_path: str) -> str:
    """Wrap the command so each node leaves its own stdout, stderr and exit status.

    ProgramRunner returns one aggregate stream for the whole fan-out, which is unusable when
    one node in eighteen fails. Each node writes `<host>.stdout`, `<host>.stderr` and
    `<host>.status` into the shared task directory instead, so the result is per-node.

    The status file is written **last and by rename**, which is what makes a partially
    written directory readable: a `.status` that exists is a node that finished.

    The command runs in a **subshell**, not a brace group. A brace group runs in the current
    shell, so a command ending in `exit` -- which scripts routinely do -- would terminate
    the wrapper before it recorded anything, and the missing status would be read as a
    failure no matter how the work actually went.
    """
    body = (
        f'h="$(hostname -s)"; d={shlex.quote(task_path)}; mkdir -p "$d"; '
        f'( {command} ) >"$d/$h.stdout" 2>"$d/$h.stderr"; rc=$?; '
        f'printf "%s" "$rc" >"$d/$h.status.tmp"; mv "$d/$h.status.tmp" "$d/$h.status"; '
        f"exit $rc"
    )
    return f"bash -lc {shlex.quote(body)}"


# ---------------------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------------------

def _submit(cluster, payload: dict) -> dict:
    command = payload.get("command") or ""
    script = payload.get("script") or ""
    if script and not command:
        command = f"bash {shlex.quote(script)}"
    if not command:
        raise RuntimeError("submit payload has neither command nor script")

    nodelist = payload.get("nodelist") or ""
    nodes = expand_nodelist(nodelist) if nodelist else []
    launch_mode = payload.get("launch_mode") or "fanout"
    if launch_mode == "head":
        targets = []  # the active head, chosen by CMDaemon
    elif launch_mode == "leader":
        targets = nodes[:1]
    else:
        targets = nodes
    if launch_mode != "head" and not targets:
        raise RuntimeError(f"launch_mode {launch_mode!r} needs a nodelist")

    jobs_dir = payload.get("jobs_dir") or DEFAULT_JOBS_DIR
    task_id = str(uuid.uuid4())
    task_path = str(_task_dir(jobs_dir, task_id))
    Path(task_path).mkdir(parents=True, exist_ok=True)

    env = {str(k): str(v) for k, v in (payload.get("env") or {}).items()}
    user = payload.get("execution_user") or os.environ.get("RACK_PYTHONCM_USER") or "root"
    status = cluster.parallel.execute(
        nodes=targets,
        command=_wrapped_command(command, task_path),
        env=env or None,
        user=user,
        max_run_time=int(payload.get("timeout") or 1800),
        wait=False,
    )
    pythoncm_id = str(getattr(status, "unique_key", "") or getattr(status, "id", "") or "")
    if not pythoncm_id:
        raise RuntimeError("ProgramRunner accepted the command but returned no id")

    _write_manifest(jobs_dir, task_id, {
        "task_id": task_id,
        "pythoncm_id": pythoncm_id,
        "name": payload.get("name") or "",
        "nodes": targets,
        "launch_mode": launch_mode,
        "execution_user": user,
        "output": payload.get("output") or "",
        "jobs_dir": jobs_dir,
        "submitted_at": _now(),
    })
    return {"ok": True, "task_id": task_id, "pythoncm_id": pythoncm_id, "nodes": targets}


def _active_status(cluster, pythoncm_id: str):
    """The live ProgramRunner status, or None once CMDaemon has dropped it."""
    try:
        key = uuid.UUID(pythoncm_id)
    except (ValueError, AttributeError, TypeError):
        key = pythoncm_id
    for entry in cluster.parallel.active_command_status(ids=[key]) or []:
        found = getattr(entry, "unique_key", None) or getattr(entry, "id", None)
        if str(found) == str(pythoncm_id):
            return entry
    return None


def _node_results(manifest: dict) -> list[dict]:
    """Per-node exit status, read from what the wrapped command left behind."""
    task_path = _task_dir(manifest["jobs_dir"], manifest["task_id"])
    out = []
    for node in manifest.get("nodes") or []:
        status_file = task_path / f"{node}.status"
        rc = None
        if status_file.is_file():
            try:
                rc = int(status_file.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                rc = None
        out.append({"node": node, "returncode": rc, "finished": rc is not None})
    return out


def _state_from_disk(manifest: dict) -> str:
    """The state the durable files prove, or "" when they cannot prove one.

    Cheap, and cheap is the point: constructing a `pythoncm` client costs about ten
    CPU-seconds on a large controller, and a poll that pays that every five seconds spends
    more of the head node's CPU watching a job than the job's own launch cost. Most polls
    can be answered from the files the work itself wrote.

    Only two situations are *provable* from disk. A recorded cancel is terminal. Every node
    having written a status is terminal, because that file is written last and by rename, so
    its existence means that node finished. Anything else is genuinely unknown here: a job
    still running and a job whose ranks died look identical on disk, and only CMDaemon can
    tell them apart.
    """
    if manifest.get("cancelled_at"):
        return "CANCELLED"
    results = _node_results(manifest)
    if not results:
        # A head-mode run writes no node files, so disk can never conclude for it.
        return ""
    if not all(r["finished"] for r in results):
        return ""
    return "COMPLETED" if all((r["returncode"] or 0) == 0 for r in results) else "FAILED"


def _derive_state(connect, manifest: dict) -> tuple[str, bool]:
    """Return ``(state, authoritative)``.

    *connect* is a callable returning a live cluster, or None to stay offline. It is a
    callable rather than a cluster so the expensive client is built only if the disk cannot
    answer -- passing a connected cluster would pay the cost before discovering it was
    unnecessary, which is the whole saving.

    ``authoritative`` is false only for the offline guess below. A caller polling cheaply
    needs to know the difference between "the files say it finished" and "the files cannot
    say, so assume it is still going".
    """
    proven = _state_from_disk(manifest)
    if proven:
        return proven, True

    if connect is None:
        # Offline and inconclusive. Report RUNNING so a caller keeps waiting rather than
        # treating an unknown as a failure -- but say it is a guess, so it verifies before
        # acting on it.
        return "RUNNING", False

    entry = _active_status(connect(), manifest["pythoncm_id"])
    if entry is not None:
        raw = str(getattr(entry, "status", "") or getattr(entry, "state", "") or "").upper()
        return _STATE_MAP.get(raw, "RUNNING"), True
    if not (manifest.get("nodes") or []):
        # A head-mode run CMDaemon has forgotten is finished; there are no files to check.
        return "COMPLETED", True
    # Gone from CMDaemon with nodes still unfinished: the command died without completing.
    return "FAILED", True


def _read_logs(manifest: dict) -> tuple[str, str]:
    """Concatenate per-node logs, labelled, for the aggregate stdout/stderr."""
    task_path = _task_dir(manifest["jobs_dir"], manifest["task_id"])
    stdout, stderr = [], []
    for node in manifest.get("nodes") or []:
        for suffix, sink in ((".stdout", stdout), (".stderr", stderr)):
            path = task_path / f"{node}{suffix}"
            if path.is_file():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if text.strip():
                    sink.append(f"===== {node} =====\n{text}")
    return "\n".join(stdout), "\n".join(stderr)


def _result(connect, manifest: dict) -> dict:
    state, authoritative = _derive_state(connect, manifest)
    nodes = _node_results(manifest)
    stdout, stderr = _read_logs(manifest)
    codes = [r["returncode"] or 0 for r in nodes if r["finished"]]
    returncode = max(codes) if codes else (0 if state == "COMPLETED" else 1)
    return {
        "ok": True,
        "task_id": manifest["task_id"],
        "state": state,
        "authoritative": authoritative,
        "returncode": returncode,
        "stdout_b64": base64.b64encode(stdout.encode("utf-8")).decode("ascii"),
        "stderr_b64": base64.b64encode(stderr.encode("utf-8")).decode("ascii"),
        "output": manifest.get("output") or "",
        "nodes": nodes,
    }


def _cancel(cluster, manifest: dict) -> dict:
    entry = _active_status(cluster, manifest["pythoncm_id"])
    if entry is not None and hasattr(entry, "kill"):
        entry.kill()
    manifest["cancelled_at"] = _now()
    _write_manifest(manifest["jobs_dir"], manifest["task_id"], manifest)
    return {"ok": True, "task_id": manifest["task_id"], "state": "CANCELLED"}


# ---------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run work through BCM's ProgramRunner.")
    p.add_argument("action", choices=("submit", "status", "cancel", "result", "probe"))
    p.add_argument("--payload", default="", help="base64url JSON job spec (submit)")
    p.add_argument("--jobs-dir", default=DEFAULT_JOBS_DIR)
    p.add_argument("--task-id", default="", help="our task id or the ProgramRunner uuid")
    p.add_argument("--module-path", default="", help="directory containing pythoncm/")
    p.add_argument("--host", default="")
    p.add_argument("--port", default="")
    p.add_argument("--cert", default="")
    p.add_argument("--key", default="")
    p.add_argument("--ca", default="")
    p.add_argument("--username", default="")
    p.add_argument("--password-file", default="", help="path; never the password itself")
    p.add_argument("--cookie-file", default="", help="path; never the cookie itself")
    p.add_argument(
        "--offline",
        action="store_true",
        help=(
            "answer status/result from the durable task files without connecting to "
            "CMDaemon. Terminal states are still exact; an unfinished job reports RUNNING "
            "with authoritative=false"
        ),
    )
    return p


def execute(args) -> dict:
    # Built at most once, and only when something actually needs CMDaemon. `submit`,
    # `cancel` and `probe` always do; `status` and `result` usually do not, and that is
    # where the ten CPU-seconds would otherwise be spent on every poll.
    cached: list = []

    def connect():
        if not cached:
            cached.append(_cluster(args))
        return cached[0]

    if args.action == "probe":
        connect().parallel.active_command_status()
        return {"ok": True, "ready": True}
    if args.action == "submit":
        if not args.payload:
            raise RuntimeError("submit needs --payload")
        payload = json.loads(base64.urlsafe_b64decode(args.payload).decode("utf-8"))
        return _submit(connect(), payload)
    if not args.task_id:
        raise RuntimeError(f"{args.action} needs --task-id")
    manifest = _load(args.jobs_dir, args.task_id)
    if args.action == "cancel":
        # Never offline: a cancel that did not reach CMDaemon has not cancelled anything,
        # and leftover ranks are what drain the next job's rack.
        return _cancel(connect(), manifest)

    reader = None if args.offline else connect
    if args.action == "status":
        state, authoritative = _derive_state(reader, manifest)
        return {
            "ok": True,
            "task_id": manifest["task_id"],
            "state": state,
            "authoritative": authoritative,
            "nodes": _node_results(manifest),
        }
    return _result(reader, manifest)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = execute(args)
    except PermissionError as exc:
        result = {
            "ok": False,
            "error": f"PermissionError: {exc}",
            # Named because this is the failure operators hit first, and the certificate is
            # never the thing they think to check.
            "hint": "CMDaemon rejected the credential; check the cert/key pair or cookie file",
        }
    except Exception as exc:  # noqa: BLE001 - the contract is one JSON object, always
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
