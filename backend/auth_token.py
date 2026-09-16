"""File-backed dashboard authentication token loading.

The file form keeps the token out of the container environment and therefore
out of ``docker inspect`` output.  The legacy environment value remains a
compatibility fallback while deployments migrate.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

_MAX_TOKEN_FILE_BYTES = 64 * 1024


def auth_token(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the configured dashboard token without logging secret material.

    ``RACK_WEB_AUTH_TOKEN_FILE`` takes precedence when it contains a non-empty
    token.  A missing or empty file falls back to ``RACK_WEB_AUTH_TOKEN`` for
    rolling-upgrade compatibility.  Invalid, oversized, or undecodable files
    fail closed rather than using the environment fallback.
    """

    env = os.environ if environ is None else environ
    token_file = (env.get("RACK_WEB_AUTH_TOKEN_FILE") or "").strip()
    if token_file:
        path = Path(token_file)
        try:
            if path.is_symlink():
                return None
            stat = path.stat()
            if not path.is_file() or stat.st_size > _MAX_TOKEN_FILE_BYTES:
                return None
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            # A deployment may intentionally point at an absent placeholder
            # while retaining the legacy environment variable during rollout.
            value = ""
        except (OSError, UnicodeError):
            return None
        if value:
            return value

    legacy = (env.get("RACK_WEB_AUTH_TOKEN") or "").strip()
    return legacy or None
