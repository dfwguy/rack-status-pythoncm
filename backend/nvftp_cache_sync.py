# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Mirror all of ``var/cache/`` to NVFTP (SFTP) under ``/nvis-project-view/{RACK_PROJECT_TOKEN}/``.

Uses the NVFTP token from :mod:`token_vault` (or a token passed to Test) as the SFTP password with
``RACK_NVFTP_SFTP_USER`` on ``transfer.nvftp.nvidia.com``. Upload runs only when:

- ``RACK_WEB_SECRET_KEY`` is configured (auto-generated on first start or set in env)
- ``RACK_PROJECT_TOKEN`` is set in Settings
- An NVFTP token is stored in the vault
- ``RACK_NVFTP_SFTP_USER`` is set (SFTP username from your NVFTP workspace)

Remote layout (under ``cache/`` on the project view path)::

    /nvis-project-view/{RACK_PROJECT_TOKEN}/cache/<file-under-var-cache>
    /nvis-project-view/{RACK_PROJECT_TOKEN}/cache/rack_comments.json

Optional extra prefix when ``RACK_NVFTP_REMOTE_BASE`` is set::

    {RACK_NVFTP_REMOTE_BASE}/nvis-project-view/{RACK_PROJECT_TOKEN}/cache/…
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import config_env
import token_vault
from json_db import atomic_write_json

_logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
# Cache lives under repo-root var/ (parent of backend/).
REPO_ROOT = APP_DIR.parent
CACHE_DIR = (REPO_ROOT / "var" / "cache").resolve()
SYNC_STATE_FILE = CACHE_DIR / "nvftp_sync_state.json"

_DEFAULT_HOST = "transfer.nvftp.nvidia.com"
_DEFAULT_PORT = 22
_DEFAULT_REMOTE_BASE = ""
_NVIS_PROJECT_VIEW_DIR = "nvis-project-view"
_REMOTE_CACHE_DIR = "cache"
_DEFAULT_INTERVAL_SEC = 7200
_CHUNK_BYTES = 8 * 1024 * 1024

_PROJECT_TOKEN_RE = re.compile(r"[^\w.\-]+")
_sync_lock = threading.Lock()
_bg_thread: threading.Thread | None = None
_bg_thread_lock = threading.Lock()


def _eget(name: str, default: str = "") -> str:
    """Config read through the one ladder in ``config_env``.

    This module used to re-implement the ladder: process env, then a hand-rolled read of
    ``job_env_overrides.json``, then ``app._eget`` fetched out of ``sys.modules``. That predated
    ``config_env.py`` and got the PRECEDENCE wrong -- it let the process environment beat an
    operator's Settings override, while every other reader has the override win. The same knob
    therefore resolved differently depending on which module asked, which is the failure
    ``core-designs.mdc`` §5 exists to prevent.
    """
    v = config_env._eget(name, default)
    return (v or default or "").strip()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _interval_sec() -> int:
    raw = _eget("RACK_NVFTP_SYNC_INTERVAL_SEC", str(_DEFAULT_INTERVAL_SEC))
    try:
        return max(300, int(raw))
    except ValueError:
        return _DEFAULT_INTERVAL_SEC


def _sanitize_project_token(raw: str) -> str:
    safe = _PROJECT_TOKEN_RE.sub("_", (raw or "").strip())
    if not safe:
        raise ValueError("RACK_PROJECT_TOKEN is empty or invalid")
    return safe


def rack_web_secret_configured() -> bool:
    return bool((os.environ.get("RACK_WEB_SECRET_KEY") or "").strip())


def sync_enabled() -> bool:
    """True when a periodic upload should run."""
    if _truthy("RACK_NVFTP_SYNC_DISABLED"):
        return False
    if not rack_web_secret_configured():
        return False
    if not _eget("RACK_PROJECT_TOKEN"):
        return False
    if not token_vault.get_nvftp_token():
        return False
    # bool(): _eget returns a str, and this function is declared -> bool.
    return bool(_eget("RACK_NVFTP_SFTP_USER"))


def skip_reason() -> str | None:
    if not rack_web_secret_configured():
        return "RACK_WEB_SECRET_KEY is not set"
    if not _eget("RACK_PROJECT_TOKEN"):
        return "RACK_PROJECT_TOKEN is not set (Settings → NVFTP cache sync)"
    if not token_vault.get_nvftp_token():
        return "NVFTP token is not configured (Overview → Set Project View)"
    if not _eget("RACK_NVFTP_SFTP_USER"):
        return "RACK_NVFTP_SFTP_USER is not set"
    if _truthy("RACK_NVFTP_SYNC_DISABLED"):
        return "RACK_NVFTP_SYNC_DISABLED=1"
    return None


def _nvftp_remote_base() -> str:
    """Optional path prefix before ``/nvis-project-view/{project}``."""
    return (_eget("RACK_NVFTP_REMOTE_BASE", _DEFAULT_REMOTE_BASE) or "").strip()


def _project_remote_dir(project: str) -> str:
    base = _nvftp_remote_base()
    if base:
        return _posix_join(base, _NVIS_PROJECT_VIEW_DIR, project)
    return _posix_join(_NVIS_PROJECT_VIEW_DIR, project)


def _sync_remote_root(project: str) -> str:
    """NVFTP ``cache/`` folder under the project view path (mirrors local ``var/cache/`` + extras)."""
    return _posix_join(_project_remote_dir(project), _REMOTE_CACHE_DIR)


# One definition, in comments_store: this module had a second one that resolved the same
# RACK_COMMENTS_FILE setting differently, so the two could point at different files. Kept here
# rather than at the top of the file because comments_store imports this module's siblings; hoisting
# it re-creates the cycle the split removed.
from comments_store import _comments_file  # noqa: E402


def _is_syncable_cache_file(name: str) -> bool:
    """True for regular cache artifacts; skips lock/temp files only."""
    return not name.endswith((".tmp", ".flock"))


def _iter_cache_files() -> list[tuple[Path, str]]:
    """All uploadable files under ``var/cache/`` (includes subdirectories)."""
    if not CACHE_DIR.is_dir():
        return []
    out: list[tuple[Path, str]] = []
    for local in sorted(CACHE_DIR.rglob("*")):
        if not local.is_file():
            continue
        if not _is_syncable_cache_file(local.name):
            continue
        out.append((local, local.relative_to(CACHE_DIR).as_posix()))
    return out


def _iter_extra_sync_files() -> list[tuple[Path, str]]:
    """Additional files mirrored into the remote ``cache/`` folder (not under ``var/cache/``)."""
    out: list[tuple[Path, str]] = []
    comments = _comments_file()
    if comments.is_file() and _is_syncable_cache_file(comments.name):
        out.append((comments, comments.name))
    return out


def _iter_sync_files() -> list[tuple[Path, str]]:
    """All local files to mirror into the remote ``cache/`` directory."""
    seen: set[str] = set()
    merged: list[tuple[Path, str]] = []
    for local, rel in _iter_cache_files() + _iter_extra_sync_files():
        if rel in seen:
            continue
        seen.add(rel)
        merged.append((local, rel))
    return sorted(merged, key=lambda pair: pair[1])


def _posix_join(*parts: str) -> str:
    out = "/"
    for p in parts:
        if not p:
            continue
        if p.startswith("/"):
            out = p
        else:
            out = out.rstrip("/") + "/" + p.lstrip("/")
    return out


def _ensure_remote_dir(sftp, posix_path: str) -> None:
    parts = PurePosixPath(posix_path).parts
    cur = ""
    for part in parts:
        if part == "/":
            cur = "/"
            continue
        cur = cur.rstrip("/") + "/" + part if cur else part
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


def _format_connect_error(exc: Exception, *, user: str, host: str) -> str:
    msg = str(exc).strip() or type(exc).__name__
    if "authentication failed" in msg.lower():
        return (
            f"SFTP authentication failed for {user}@{host}. "
            "Check RACK_NVFTP_SFTP_USER and the NVFTP token (save in Set Project View or paste for Test)."
        )
    return msg


def _connect_sftp(*, username: str | None = None, password: str | None = None):
    import paramiko

    host = _eget("RACK_NVFTP_SFTP_HOST", _DEFAULT_HOST) or _DEFAULT_HOST
    port_raw = _eget("RACK_NVFTP_SFTP_PORT", str(_DEFAULT_PORT)) or str(_DEFAULT_PORT)
    try:
        port = int(port_raw)
    except ValueError:
        port = _DEFAULT_PORT
    user = (username or _eget("RACK_NVFTP_SFTP_USER")).strip()
    password = (password or token_vault.get_nvftp_token() or "").strip()
    if not user or not password:
        raise RuntimeError("RACK_NVFTP_SFTP_USER and NVFTP token are required")

    ssh = paramiko.SSHClient()
    if _truthy("RACK_NVFTP_SFTP_STRICT_HOST_KEY"):
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    _logger.info("NVFTP cache sync: connecting to %s@%s:%s", user, host, port)
    try:
        ssh.connect(
            hostname=host,
            port=port,
            username=user,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=30,
        )
    except Exception as e:
        raise RuntimeError(_format_connect_error(e, user=user, host=host)) from e
    transport = ssh.get_transport()
    if transport is not None:
        transport.set_keepalive(30)
    return ssh


def _load_state() -> dict:
    try:
        if SYNC_STATE_FILE.is_file():
            with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
                doc = json.load(f)
            if isinstance(doc, dict):
                return doc
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {"files": {}}


def _save_state(state: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SYNC_STATE_FILE, state, sort_keys=True)


def _should_upload(local: Path, rel: str, state: dict) -> bool:
    try:
        st = local.stat()
    except OSError:
        return False
    sig = f"{st.st_size}:{int(st.st_mtime_ns)}"
    prev = (state.get("files") or {}).get(rel)
    return prev != sig


def _upload_file(sftp, local: Path, remote_path: str) -> None:
    _ensure_remote_dir(sftp, str(PurePosixPath(remote_path).parent))
    with local.open("rb") as src, sftp.open(remote_path, "wb") as dst:
        while True:
            chunk = src.read(_CHUNK_BYTES)
            if not chunk:
                break
            dst.write(chunk)


def test_project_directory(
    *,
    project_token: str,
    sftp_user: str | None = None,
    token: str | None = None,
) -> dict:
    """Connect via SFTP and write a small JSON probe under ``{remote_base}/{project_token}/``."""
    try:
        project = _sanitize_project_token(project_token)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    project_dir = _project_remote_dir(project)
    test_filename = "nvis-rack-status-connectivity-test.json"
    remote_path = _posix_join(project_dir, test_filename)
    payload = {
        "ok": True,
        "source": "nvis-rack-status",
        "test": "nvftp_project_directory",
        "project_token": project,
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    body = (json.dumps(payload, indent=2) + "\n").encode("utf-8")

    try:
        ssh = _connect_sftp(username=sftp_user, password=token)
    except Exception as e:
        _logger.exception("NVFTP test connect failed")
        host = _eget("RACK_NVFTP_SFTP_HOST", _DEFAULT_HOST) or _DEFAULT_HOST
        user = (sftp_user or _eget("RACK_NVFTP_SFTP_USER")).strip()
        return {"ok": False, "error": _format_connect_error(e, user=user or "?", host=host)}

    try:
        sftp = ssh.open_sftp()
        try:
            _ensure_remote_dir(sftp, project_dir)
            with sftp.open(remote_path, "wb") as dst:
                dst.write(body)
            st = sftp.stat(remote_path)
            remote_size = int(st.st_size or 0)
        finally:
            sftp.close()
    except Exception as e:
        _logger.exception("NVFTP test upload failed")
        return {"ok": False, "error": str(e), "remote_dir": project_dir}
    finally:
        ssh.close()

    return {
        "ok": True,
        "remote_dir": project_dir,
        "remote_file": remote_path,
        "bytes_written": len(body),
        "remote_size": remote_size,
        "message": f"Wrote {test_filename} to {project_dir}",
    }


def run_sync(*, force: bool = False) -> dict:
    """Upload ``var/cache/`` and extras (e.g. rack_comments.json) incrementally unless *force*."""
    reason = skip_reason()
    if reason:
        return {"ok": True, "skipped": True, "reason": reason}

    sync_files = _iter_sync_files()
    if not sync_files:
        return {"ok": True, "skipped": True, "reason": "no files to sync (var/cache empty and rack_comments.json missing)"}

    project = _sanitize_project_token(_eget("RACK_PROJECT_TOKEN"))
    remote_root = _sync_remote_root(project)
    force = force or _truthy("RACK_NVFTP_SYNC_FORCE")

    with _sync_lock:
        state = _load_state()
        files_state: dict[str, str] = dict(state.get("files") or {})
        if (state.get("remote_root") or "").strip() and state.get("remote_root") != remote_root:
            _logger.info(
                "NVFTP cache sync: remote_root changed (%s -> %s); re-uploading all cache files",
                state.get("remote_root"),
                remote_root,
            )
            files_state = {}
        uploaded: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []

        try:
            ssh = _connect_sftp()
        except Exception as e:
            _logger.exception("NVFTP cache sync connect failed")
            host = _eget("RACK_NVFTP_SFTP_HOST", _DEFAULT_HOST) or _DEFAULT_HOST
            user = _eget("RACK_NVFTP_SFTP_USER")
            return {"ok": False, "error": _format_connect_error(e, user=user or "?", host=host)}

        try:
            sftp = ssh.open_sftp()
            try:
                for local, rel in sync_files:
                    if not force and not _should_upload(local, rel, {"files": files_state}):
                        skipped.append(rel)
                        continue
                    remote_path = _posix_join(remote_root, rel)
                    try:
                        _upload_file(sftp, local, remote_path)
                        st = local.stat()
                        files_state[rel] = f"{st.st_size}:{int(st.st_mtime_ns)}"
                        uploaded.append(rel)
                    except Exception as e:
                        errors.append(f"{rel}: {e}")
                        _logger.warning("NVFTP upload failed for %s: %s", rel, e, exc_info=True)
            finally:
                sftp.close()
        finally:
            ssh.close()

        state = {
            "files": files_state,
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "remote_root": remote_root,
            "uploaded_count": len(uploaded),
            "skipped_count": len(skipped),
            "error_count": len(errors),
        }
        try:
            _save_state(state)
        except OSError as e:
            errors.append(f"state write: {e}")

    result = {
        "ok": len(errors) == 0,
        "skipped": False,
        "forced": force,
        "remote_root": remote_root,
        "local_file_count": len(sync_files),
        "uploaded": uploaded,
        "skipped_unchanged": skipped,
        "errors": errors,
        "last_run_at": state.get("last_run_at"),
    }
    if uploaded:
        _logger.info(
            "NVFTP cache sync: uploaded %d file(s) to %s",
            len(uploaded),
            remote_root,
        )
    return result


def sync_status() -> dict:
    """Public status for API (no secrets)."""
    st = _load_state()
    sync_files = _iter_sync_files()
    tracked = st.get("files") or {}
    pending = 0
    for local, rel in sync_files:
        if _should_upload(local, rel, {"files": tracked}):
            pending += 1
    return {
        "enabled": sync_enabled(),
        "skip_reason": skip_reason(),
        "interval_sec": _interval_sec(),
        "project_token_set": bool(_eget("RACK_PROJECT_TOKEN")),
        "nvftp_token_configured": bool(token_vault.nvftp_token_status().get("configured")),
        "local_file_count": len(sync_files),
        "pending_upload_count": pending,
        "last_run_at": st.get("last_run_at"),
        "last_remote_root": st.get("remote_root"),
        "last_uploaded_count": st.get("uploaded_count"),
        "last_skipped_count": st.get("skipped_count"),
        "last_error_count": st.get("error_count"),
    }


def _log_sync_result(out: dict) -> None:
    """Log background sync outcomes (failures are easy to miss when no exception is raised)."""
    if out.get("skipped"):
        reason = out.get("reason") or "unknown"
        _logger.debug("NVFTP cache sync skipped: %s", reason)
        return
    if not out.get("ok"):
        err = out.get("error")
        if not err and out.get("errors"):
            err = "; ".join(str(e) for e in (out.get("errors") or [])[:5])
        _logger.warning("NVFTP cache sync failed: %s", err or "unknown error")
        return
    uploaded = out.get("uploaded") or []
    if uploaded:
        _logger.info(
            "NVFTP cache sync: uploaded %d file(s) to %s",
            len(uploaded),
            out.get("remote_root") or "?",
        )
    else:
        _logger.debug("NVFTP cache sync: no cache file changes")


def _background_loop() -> None:
    time.sleep(30)
    while True:
        interval = float(_interval_sec())
        if sync_enabled():
            try:
                _log_sync_result(run_sync())
            except Exception:
                _logger.exception("NVFTP cache sync loop error")
        else:
            reason = skip_reason()
            if reason:
                _logger.debug("NVFTP cache sync idle (will retry): %s", reason)
        time.sleep(interval)


def ensure_background_thread() -> None:
    """Start or restart the NVFTP cache sync daemon thread (runs even when sync is not yet configured)."""
    global _bg_thread
    with _bg_thread_lock:
        if _bg_thread is not None and _bg_thread.is_alive():
            return
        if _bg_thread is not None:
            _logger.warning("NVFTP cache sync background thread died; restarting")
        _bg_thread = threading.Thread(
            target=_background_loop,
            name="nvftp-cache-sync",
            daemon=True,
        )
        _bg_thread.start()
        _logger.info(
            "NVFTP cache sync thread started (interval %ss, enabled=%s)",
            _interval_sec(),
            sync_enabled(),
        )
