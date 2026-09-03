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
"""Reading validation logs: download, fetch content, and tail a live one.

Downloads and content reads are bounded and path-checked -- a log path arrives from the browser, so it
is resolved against the allowed roots before anything opens it, and the file lives on the execution
host rather than locally.

The tail endpoints stream. A NEMO run writes for hours, so the operator watches it arrive rather than
waiting for a complete file; the generator holds the connection open and yields as lines appear.
"""
from __future__ import annotations

import base64
import logging
import os
import queue
import shlex
import subprocess
import threading
from pathlib import Path, PurePosixPath

import host_exec
from audit.actions import action
from config_env import _eget, _env_int, _subprocess_env
from core import rpath as _rpath
from flask import Blueprint, Response, jsonify, request, send_file, stream_with_context
from log_discovery import (
    _resolve_nemo_log_basename_in_dir,
    pick_log_for_rack,
)
from naming import _normalize_rack_id, multirack_display_rack_from_basename
from paths import _resolve_log_dir
from slurm_query import _resolve_slurm_job_stdout_path
from stream_exec import _translate_cwd_for_remote

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("logs", __name__, url_prefix="")


def _rack_log_resolve(
    log_dir_raw: str | None,
    rack: str,
    job_id_raw: str | None,
    log_file_raw: str | None = None,
) -> tuple[Path, Path, str] | tuple[Response, int]:
    """Resolve rack log path for API handlers. Returns (log_dir, log_path, rack_key) or (jsonify, status)."""
    try:
        log_dir = _resolve_log_dir(log_dir_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    lf = (log_file_raw or "").strip()
    if lf:
        lp = _resolve_nemo_log_basename_in_dir(log_dir, lf)
        if lp is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"log file not found or basename must contain NEMO/NCCL: {Path(lf).name!r}"
                        ),
                    }
                ),
                404,
            )
        rk = multirack_display_rack_from_basename(lp.name)
        return log_dir, lp, rk
    rack_s = (rack or "").strip()
    try:
        rk = _normalize_rack_id(rack_s)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    job_raw = (job_id_raw or "").strip()
    job_id = job_raw if job_raw and job_raw != "-" else None
    log_path = pick_log_for_rack(log_dir, rack_s, job_id)
    if log_path is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": (
                        f"no matching *.txt for rack {rk!r} under {log_dir} or {log_dir / 'failed'} "
                        f"(NEMO/NCCL basename, or ClusterKit root-CK-/clusterkit with one rack id)"
                    ),
                }
            ),
            404,
        )
    # In ssh mode these are REMOTE paths (PurePosixPath): .resolve()/.is_file() touch the local
    # filesystem and raise AttributeError, which Flask turns into a 500 -> an HTML error page ->
    # "Unexpected token '<'" in the SPA. Only do filesystem checks for genuinely local paths.
    if isinstance(log_dir, Path) and isinstance(log_path, Path):
        root = _rpath(log_dir)
        lp = _rpath(log_path)
        try:
            lp.relative_to(root)
        except ValueError:
            return jsonify({"ok": False, "error": "invalid log path"}), 400
        if not lp.is_file():
            return jsonify({"ok": False, "error": "log file not found"}), 404
    else:
        # PurePath.relative_to does not collapse "..", so normalise textually before the
        # containment check or "<dir>/../../etc/passwd" would pass it.
        root = PurePosixPath(os.path.normpath(str(log_dir)))
        lp = PurePosixPath(os.path.normpath(str(log_path)))
        try:
            lp.relative_to(root)
        except ValueError:
            return jsonify({"ok": False, "error": "invalid log path"}), 400
        # Existence is not checkable from here; a missing remote file surfaces as a read error.
    return log_dir, lp, rk


@bp.route("/api/rack-log-download")
@action(get="logs:read")
def api_rack_log_download():
    """Download the full NEMO rack log file (same pick as parser / tail-log)."""
    out = _rack_log_resolve(
        request.args.get("log_dir"),
        request.args.get("rack") or "",
        request.args.get("job_id"),
        request.args.get("log_file"),
    )
    # Arity, not isinstance -- see the note in api_rack_log_content(); log_dir is a PurePosixPath
    # in ssh mode and would otherwise be returned to Flask as if it were an error tuple.
    if len(out) != 3:
        return out
    _log_dir, lp, _rk = out
    if not isinstance(lp, Path):
        # Remote (ssh mode): the file is on the head node, so send_file() cannot reach it.
        # Stream it over the same transport that produced the path.
        q = shlex.quote(str(lp))
        cp = host_exec.run_on_host(f"base64 -w0 {q}")
        if cp.returncode != 0 or not (cp.stdout or "").strip():
            return jsonify(
                {"ok": False, "error": f"cannot read remote log: {lp} {(cp.stderr or '').strip()}"}
            ), 404
        try:
            blob = base64.b64decode((cp.stdout or "").strip())
        except ValueError as e:  # binascii.Error subclasses ValueError
            return jsonify({"ok": False, "error": f"decode failed: {e}"}), 500
        resp = Response(blob, mimetype="text/plain; charset=utf-8")
        resp.headers["Content-Disposition"] = f'attachment; filename="{lp.name}"'
        resp.headers["Cache-Control"] = "no-store"
        return resp
    return send_file(
        str(lp),
        as_attachment=True,
        download_name=lp.name,
        mimetype="text/plain; charset=utf-8",
        max_age=0,
    )


@bp.route("/api/rack-log-content")
@action(get="logs:read")
def api_rack_log_content():
    """Return full log text as JSON if under size limit; otherwise too_large + metadata."""
    max_inline = max(0, _env_int("RACK_LOG_INLINE_MAX_BYTES", 10 * 1024 * 1024))
    out = _rack_log_resolve(
        request.args.get("log_dir"),
        request.args.get("rack") or "",
        request.args.get("job_id"),
        request.args.get("log_file"),
    )
    # _rack_log_resolve returns (jsonify, code) on error and (log_dir, lp, rk) on success.
    # Do NOT discriminate with isinstance(out[0], Path): in ssh mode log_dir is a PurePosixPath,
    # which is NOT a Path subclass, so every successful remote resolve was mistaken for an error
    # and returned raw to Flask ->
    #   TypeError: The view function did not return a valid response ... it was a PurePosixPath
    # i.e. a 500 on EVERY log view in ssh mode. Discriminate on arity instead.
    if len(out) != 3:
        return out
    _log_dir, lp, _rk = out
    # The log lives on the head node in ssh mode, so lp is a PurePosixPath and .stat()/.read_bytes()
    # raise AttributeError -> 500 -> an HTML error page -> "Unexpected token '<'" in the SPA.
    # Read it over the same transport that produced the path.
    remote = not isinstance(lp, Path)
    if remote:
        q = shlex.quote(str(lp))
        cp = host_exec.run_on_host(f"stat -c %s {q} 2>/dev/null || echo -1")
        rc, so, se = cp.returncode, (cp.stdout or ""), (cp.stderr or "")
        try:
            sz = int(so.strip().splitlines()[-1])
        except (ValueError, IndexError):
            sz = -1
        if rc != 0 or sz < 0:
            return jsonify({"ok": False, "error": f"log file not found: {lp} {(se or '').strip()}"}), 404
    else:
        try:
            sz = lp.stat().st_size
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    if sz > max_inline:
        return jsonify(
            {
                "ok": True,
                "too_large": True,
                "size_bytes": sz,
                "filename": lp.name,
                "max_inline_bytes": max_inline,
            }
        )
    if remote:
        cp = host_exec.run_on_host(f"cat {shlex.quote(str(lp))}")
        if cp.returncode != 0:
            return jsonify({"ok": False, "error": (cp.stderr or "remote read failed").strip()}), 500
        text = cp.stdout or ""
    else:
        try:
            raw = lp.read_bytes()
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        text = raw.decode("utf-8", errors="replace")
    return jsonify(
        {
            "ok": True,
            "too_large": False,
            "size_bytes": sz,
            "filename": lp.name,
            "content": text,
        }
    )


def _parse_tail_log_request(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Validate tail-log POST body; prep has log_path, log_dir, display_cmd, rack."""
    if os.environ.get("RACK_TAIL_LOG_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "tail-log disabled (RACK_TAIL_LOG_DISABLED)"}), 403)
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    lf_raw = body.get("log_basename") or body.get("log_file")
    lf = str(lf_raw).strip() if lf_raw else ""
    if lf:
        log_path = _resolve_nemo_log_basename_in_dir(log_dir, lf)
        if log_path is None:
            return None, (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"log file not found or basename must contain NEMO/NCCL: {Path(lf).name!r}"
                        ),
                    }
                ),
                404,
            )
        rk = multirack_display_rack_from_basename(log_path.name)
    else:
        rack = (body.get("rack") or "").strip()
        try:
            rk = _normalize_rack_id(rack)
        except ValueError as e:
            return None, (jsonify({"ok": False, "error": str(e)}), 400)
        job_raw = (body.get("job_id") or "").strip()
        job_id = job_raw if job_raw and job_raw != "-" else None
        log_path = pick_log_for_rack(log_dir, rack, job_id)
        if log_path is None:
            return None, (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"no matching *.txt for rack {rk!r} under {log_dir} or {log_dir / 'failed'} "
                        f"(NEMO/NCCL basename, or ClusterKit root-CK-/clusterkit with one rack id)"
                        ),
                    }
                ),
                404,
            )
    # ssh mode: the log lives on the head node, so these are PurePosixPath and .resolve() raises
    # AttributeError -> 500 -> an HTML error page -> "Unexpected token '<'" / "500 Internal Server
    # Error" in the SPA. Fifth site of this same bug class in this file; branch on EXEC MODE, not
    # on the object type (_resolve_log_dir always returns a local Path object either way).
    root = _rpath(log_dir)
    lp = _rpath(log_path)
    try:
        lp.relative_to(root)
    except ValueError:
        return None, (jsonify({"ok": False, "error": "invalid log path"}), 400)
    rel = str(lp.relative_to(root))
    n0 = max(0, _env_int("RACK_TAIL_INITIAL_LINES", 400))
    display_cmd = f"tail -n {n0} -f {shlex.quote(rel)}"
    return (
        {
            "log_dir": log_dir,
            "log_path": lp,
            "rack": rk,
            "rel_path": rel,
            "display_cmd": display_cmd,
        },
        None,
    )


def _tail_log_stream_generator(prep: dict) -> object:
    """Stream bytes from `tail -n N -f logfile` until client disconnects or error."""
    lp = prep["log_path"]
    n_init = max(0, _env_int("RACK_TAIL_INITIAL_LINES", 400))
    cmd = ["tail", "-n", str(n_init), "-f", str(lp)]
    # SSH mode: the log lives on the cluster head — tail must run there, not in this
    # container. Wrap via host_exec (ssh 'cd <log_dir> && tail …'); local stdbuf and
    # local cwd only apply to local execution. Mirrors _merged_stdout_stream_generator.
    # See _merged_stdout_stream_generator: hand-built streaming Popen, so the
    # mechanism matters here in a way it does not for ordinary callers.
    remote = host_exec.exec_kind() == "ssh"
    if (_eget("RACK_TAIL_LINEBUFFER", "1") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        # `tail -f` writing to a pipe (no TTY) is block-buffered, so a slow live log
        # never fills the buffer and the viewer hangs on "connecting". Resolve stdbuf
        # on the *execution* host — the local path may not exist there.
        stdbuf = host_exec.which("stdbuf")
        if stdbuf:
            cmd = [stdbuf, "-oL", "-eL", *cmd]
    cwd_local = str(prep["log_dir"])
    remote_cwd = _translate_cwd_for_remote(cwd_local) if remote else None
    cmd = host_exec.wrap_for_host(cmd, cwd=remote_cwd)
    proc = subprocess.Popen(
        cmd,
        cwd=None if remote else cwd_local,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=None if remote else _subprocess_env(),
        bufsize=0,
    )
    outq: queue.Queue[bytes | None] = queue.Queue()

    def feeder() -> None:
        try:
            stdout = proc.stdout
            if stdout is None:
                return
            while True:
                block = stdout.read(8192)
                if not block:
                    break
                outq.put(block)
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass
            outq.put(None)

    threading.Thread(target=feeder, daemon=True).start()
    header = (
        f"# {prep['display_cmd']}\n# cwd: {prep['log_dir']}\n# file: {lp}\n\n"
    )
    yield header.encode("utf-8")
    try:
        while True:
            try:
                chunk = outq.get(timeout=0.5)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if chunk is None:
                break
            yield chunk
        rc = proc.poll()
        if rc is not None and rc != 0:
            yield f"\n--- tail exited {rc} ---\n".encode()
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=15)
            except (subprocess.TimeoutExpired, OSError):
                pass


def _parse_tail_job_stdout_stream_request(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """POST JSON ``{log_dir, path}`` — resolve Slurm ``StdOut`` path and tail it (same generator as NEMO tail)."""
    if os.environ.get("RACK_TAIL_LOG_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "tail-log disabled (RACK_TAIL_LOG_DISABLED)"}), 403)
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    path_raw = body.get("path")
    if not isinstance(path_raw, str) or not path_raw.strip():
        return None, (jsonify({"ok": False, "error": "path is required"}), 400)
    try:
        lp = _resolve_slurm_job_stdout_path(path_raw.strip(), str(log_dir))
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    n0 = max(0, _env_int("RACK_TAIL_INITIAL_LINES", 400))
    display_cmd = f"tail -n {n0} -f {shlex.quote(str(lp))}"
    return (
        {
            "log_dir": log_dir,
            "log_path": lp,
            "rack": "",
            "rel_path": lp.name,
            "display_cmd": display_cmd,
        },
        None,
    )


@bp.route("/api/tail-job-stdout-stream", methods=["POST"])
@action("logs:tail_job", variant="stream")
def api_tail_job_stdout_stream():
    """Stream ``tail -f`` for a Slurm job stdout file (``path`` from ``scontrol show job`` ``StdOut=``)."""
    body = request.get_json(silent=True) or {}
    prep, err = _parse_tail_job_stdout_stream_request(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None
    return Response(
        stream_with_context(_tail_log_stream_generator(prep)),
        mimetype="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@bp.route("/api/tail-log-stream", methods=["POST"])
@action("logs:tail", variant="stream")
def api_tail_log_stream():
    """Stream live `tail -f` of the NEMO rack log (same file pick as parser)."""
    body = request.get_json(silent=True) or {}
    prep, err = _parse_tail_log_request(body)
    if err is not None:
        return err[0], err[1]
    assert prep is not None

    return Response(
        stream_with_context(_tail_log_stream_generator(prep)),
        mimetype="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
