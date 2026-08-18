# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Dashboard authentication mechanism: modes, credential verification, session state.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The *mechanism*, deliberately separate from both the endpoints that use it (``api/auth.py``) and the
gate that enforces it (``_require_dashboard_auth`` in ``app.py``). One module answers "how does this
app decide who you are", which is the question the coming authorization work has to build on.

Two modes, chosen by :func:`_auth_mode`. PAM authenticates against *this machine's* accounts, so it
is only meaningful when the app runs on the target host; a container talking to a remote head node has
no such mapping, which is why a remote target defaults to a shared token instead.

PAM verification prefers in-process ctypes and falls back to a subprocess helper under a Python whose
ctypes actually works -- a module-loaded Python on a cluster login node frequently cannot load it, and
the error that produces is opaque, so the fallback raises RuntimeError naming
``RACK_WEB_PAM_HELPER_PYTHON``.

Not an endpoint module and not under ``api/``: it imports Flask only for ``session``, and nothing here
builds a response.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from datetime import timedelta

import host_exec
from config_env import _env_int, _subprocess_env
from core import truthy_env as _truthy_env
from flask import session
from paths import APP_DIR


def _auth_enabled() -> bool:
    """Dashboard login via PAM or token unless RACK_WEB_AUTH_DISABLED is set."""
    return not _truthy_env("RACK_WEB_AUTH_DISABLED")


def _auth_mode() -> str:
    """Return ``pam`` or ``token``.  Defaults to ``token`` when inside a container (SSH mode)."""
    explicit = (os.environ.get("RACK_WEB_AUTH_MODE") or "").strip().lower()
    if explicit in ("pam", "token"):
        return explicit
    # PAM authenticates against *this* machine's accounts, so it is only meaningful
    # when the app runs on the target host; a remote target has no such mapping.
    return "pam" if host_exec.exec_kind() == "local" else "token"


def _auth_token() -> str | None:
    """Token string when RACK_WEB_AUTH_MODE=token."""
    t = (os.environ.get("RACK_WEB_AUTH_TOKEN") or "").strip()
    return t or None


def _auth_user() -> str:
    return (os.environ.get("RACK_WEB_AUTH_USER") or "root").strip() or "root"


def _pam_service() -> str:
    return (os.environ.get("RACK_WEB_PAM_SERVICE") or "login").strip() or "login"


def _session_lifetime() -> timedelta:
    h = _env_int("RACK_WEB_SESSION_HOURS", 8)
    h = max(h, 1)
    h = min(h, 24 * 30)
    return timedelta(hours=h)


def _session_ok() -> bool:
    return session.get("rack_web_auth") == "1"


def _safe_next(raw: str | None) -> str:
    s = (raw or "").strip().split("#", 1)[0]
    if not s.startswith("/") or s.startswith("//"):
        return ""
    return s


def _pam_helper_python_exe() -> str:
    return (os.environ.get("RACK_WEB_PAM_HELPER_PYTHON") or "/usr/bin/python3").strip() or "/usr/bin/python3"


def _pam_verify_subprocess(username: str, password: str, service: str) -> bool:
    """Authenticate via pam_helper.py under a Python that can load ctypes (e.g. /usr/bin/python3)."""
    exe = _pam_helper_python_exe()
    helper = APP_DIR / "rack_web" / "pam_helper.py"
    if not helper.is_file():
        raise RuntimeError(f"Internal error: missing {helper}")
    payload = json.dumps({"user": username, "password": password, "service": service})
    try:
        proc = subprocess.run(
            [exe, str(helper)],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=90,
            env=_subprocess_env(),
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"PAM helper interpreter not found: {exe!r}. Set RACK_WEB_PAM_HELPER_PYTHON to a "
            "Python whose ctypes module works (often /usr/bin/python3), or recreate the venv with "
            "that interpreter instead of a mismatched module-loaded Python."
        ) from None
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("PAM authentication subprocess timed out") from e
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    err_tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(
            f"PAM helper process failed (exit {proc.returncode}). "
            f"Try RACK_WEB_PAM_HELPER_PYTHON=/usr/bin/python3. Stderr: {err_tail[-1200:]}"
        )
    if not out:
        raise RuntimeError(
            "PAM helper produced no output. "
            f"Try RACK_WEB_PAM_HELPER_PYTHON=/usr/bin/python3. Stderr: {err_tail[-1200:]}"
        )
    line = out.splitlines()[-1].strip()
    try:
        data = json.loads(line)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"PAM helper returned invalid JSON ({line[:200]!r}): {e}") from e
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return bool(data.get("ok"))


def _token_verify(password: str) -> bool:
    """Verify password against RACK_WEB_AUTH_TOKEN (simple shared-secret)."""
    token = _auth_token()
    if not token:
        return False
    return secrets.compare_digest(password, token)


def _pam_verify(username: str, password: str, service: str) -> bool:
    if password is None:
        return False
    if _auth_mode() == "token":
        return _token_verify(password)
    try:
        from rack_web.pam_ctypes import authenticate as pam_authenticate
    except (ImportError, OSError):
        return _pam_verify_subprocess(username, password, service)
    return bool(pam_authenticate(username, password, service=service))
