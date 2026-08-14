# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Where the scripts and the logs live -- on whichever host actually owns them.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Every path here names a file on the *execution host*, which is usually not this machine. They are
``PurePosixPath``/str throughout and are never resolved against the local filesystem: existence is
asked of the connection (``host_exec.is_dir`` / ``is_file``), because only it knows which host the
path belongs to. ``PurePosixPath`` has no ``.resolve()``, so a call site that forgets this raises
loudly instead of quietly answering about the wrong machine.

Role roots come first (``host_exec.role_root``) so a deployment can put the script tree on a
different host from the logs; ``RACK_REMOTE_BASE_PATH`` is the single-host fallback.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import host_exec
from core import rpath as _rpath

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent

# One env var places the whole tree on the head node; role roots can override per role.
_REMOTE_BASE = os.environ.get("RACK_REMOTE_BASE_PATH", "").strip()
if _REMOTE_BASE:
    SCRIPTS_DIR = Path(_REMOTE_BASE) / "bin" / "scripts"
    DEFAULT_LOG_DIR = Path(_REMOTE_BASE) / "var" / "logs"
else:
    SCRIPTS_DIR = REPO_ROOT / "bin" / "scripts"
    DEFAULT_LOG_DIR = REPO_ROOT / "var" / "logs"

DEFAULT_SCRIPT = SCRIPTS_DIR / "rack_status.sh"


def _scripts_dir() -> str:
    """Directory holding ``bin/scripts`` on the host that actually runs them.

    Prefers the ``scripts`` role's declared root, so a deployment can keep the
    script tree on a different machine from the app (or from the logs). Falls back
    to the module-level ``SCRIPTS_DIR``, which is the single-host answer derived
    from ``RACK_REMOTE_BASE_PATH``.
    """
    return host_exec.role_root("scripts") or str(SCRIPTS_DIR)


def _logs_dir_default() -> str:
    """Default log root on the host that owns the ``logs`` role."""
    return host_exec.role_root("logs") or str(DEFAULT_LOG_DIR)


def _script_exists(path: Path) -> bool:
    """Check whether a script exists on the execution host.

    Previously this returned True unconditionally for a remote target, because the
    local check could only ever be wrong there. The connection can answer for both.
    """
    return host_exec.is_file(path)


def _default_log_dir() -> PurePosixPath:
    return _rpath(os.environ.get("RACK_STATUS_LOG_DIR", str(DEFAULT_LOG_DIR)))


def _allowed_roots() -> list[PurePosixPath]:
    raw = os.environ.get("RACK_STATUS_ALLOWED_PREFIXES", "")
    if not raw.strip():
        return []
    return [_rpath(x.strip()) for x in raw.split(",") if x.strip()]


def _resolve_log_dir(raw: str | None) -> PurePosixPath:
    # No macOS firmlink fixup any more: that existed to undo a local .resolve()
    # applied to a head-node path, and the connection never resolves a remote path
    # against this filesystem in the first place.
    log_dir = _rpath(raw or _default_log_dir())
    roots = _allowed_roots()
    if roots:
        ok = any(str(log_dir).startswith(str(r) + os.sep) or log_dir == r for r in roots)
        if not ok:
            raise ValueError(f"log_dir must be under one of: {[str(r) for r in roots]}")
    if not host_exec.is_dir(log_dir):
        raise ValueError(f"not a directory: {log_dir}")
    return log_dir
