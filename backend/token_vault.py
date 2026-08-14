# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Encrypted NVFTP (and future) API token vault.

Tokens are stored on disk as JSON with a random salt and Fernet ciphertext derived from
``NVIS_VAULT_MASTER_KEY`` or ``RACK_WEB_SECRET_KEY`` via PBKDF2-HMAC-SHA256.

Other backend modules should call :func:`get_nvftp_token` for outbound NVFTP API auth.

Secrets live under ``var/secrets/`` (gitignored). Never commit ``*.vault.json``, flock files,
or ``.vault_master_key`` — CI runs ``scripts/check_no_vault_in_git.sh``.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

_logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
# Secrets vault lives under repo-root var/ (parent of backend/).
REPO_ROOT = APP_DIR.parent
VAULT_DIR = Path(os.environ.get("NVIS_TOKEN_VAULT_DIR", "") or str(REPO_ROOT / "var" / "secrets"))
NVFTP_VAULT_FILE = VAULT_DIR / "nvftp_token.vault.json"
FLOCK_FILE = VAULT_DIR / "nvftp_token.vault.flock"

_PBKDF2_ITERATIONS = 600_000
_VAULT_VERSION = 1


@contextmanager
def _vault_flock(shared: bool = True):
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    with open(FLOCK_FILE, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _master_key_bytes() -> bytes:
    """Master secret used to derive per-file encryption keys."""
    raw = (os.environ.get("NVIS_VAULT_MASTER_KEY") or os.environ.get("RACK_WEB_SECRET_KEY") or "").strip()
    if raw:
        return raw.encode("utf-8")
    raise RuntimeError(
        "Token vault requires RACK_WEB_SECRET_KEY or NVIS_VAULT_MASTER_KEY "
        "(the dashboard creates RACK_WEB_SECRET_KEY on first start under var/secrets/)"
    )


def _derive_fernet_key(master: bytes, salt: bytes) -> bytes:
    dk = hashlib.pbkdf2_hmac("sha256", master, salt, _PBKDF2_ITERATIONS, dklen=32)
    return base64.urlsafe_b64encode(dk)


def _encrypt(plaintext: str) -> dict[str, str]:
    salt = os.urandom(16)
    fernet = Fernet(_derive_fernet_key(_master_key_bytes(), salt))
    token = fernet.encrypt(plaintext.encode("utf-8"))
    return {
        "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "ciphertext": token.decode("ascii"),
    }


def _decrypt(blob: dict[str, Any]) -> str:
    salt_b = blob.get("salt")
    ct = blob.get("ciphertext")
    if not isinstance(salt_b, str) or not isinstance(ct, str):
        raise ValueError("vault file missing salt or ciphertext")  # noqa: TRY004 - routes catch ValueError to answer 400; TypeError would 500
    salt = base64.urlsafe_b64decode(salt_b.encode("ascii"))
    fernet = Fernet(_derive_fernet_key(_master_key_bytes(), salt))
    try:
        return fernet.decrypt(ct.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("vault decrypt failed (wrong master key or corrupted file)") from e


def nvftp_token_status() -> dict[str, object]:
    """Return whether a token is stored (never returns the raw token)."""
    with _vault_flock(shared=True):
        if not NVFTP_VAULT_FILE.is_file():
            return {"configured": False}
        try:
            with open(NVFTP_VAULT_FILE, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError):
            return {"configured": False}
    if not isinstance(doc, dict) or int(doc.get("version") or 0) != _VAULT_VERSION:
        return {"configured": False}
    updated = doc.get("updated_at")
    try:
        plain = _decrypt(doc)
    except (ValueError, RuntimeError):
        return {"configured": False, "error": "stored token cannot be decrypted"}
    tail = plain[-4:] if len(plain) >= 4 else "****"
    return {
        "configured": True,
        "masked": f"••••{tail}",
        "updated_at": updated if isinstance(updated, str) else None,
    }


def store_nvftp_token(plaintext: str) -> dict[str, object]:
    """Encrypt and persist the NVFTP API token."""
    token = (plaintext or "").strip()
    if not token:
        raise ValueError("token is empty")
    if len(token) > 8192:
        raise ValueError("token is too long")
    enc = _encrypt(token)
    doc: dict[str, object] = {
        "version": _VAULT_VERSION,
        "kind": "nvftp",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **enc,
    }
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = NVFTP_VAULT_FILE.with_suffix(".tmp")
    with _vault_flock(shared=False):
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        os.replace(tmp, NVFTP_VAULT_FILE)
        try:
            os.chmod(NVFTP_VAULT_FILE, 0o600)
        except OSError:
            pass
    return nvftp_token_status()


def clear_nvftp_token() -> None:
    """Remove stored NVFTP token."""
    with _vault_flock(shared=False):
        if NVFTP_VAULT_FILE.is_file():
            NVFTP_VAULT_FILE.unlink()


def get_nvftp_token() -> str | None:
    """Return decrypted NVFTP token for internal API calls, or ``None`` if unset."""
    with _vault_flock(shared=True):
        if not NVFTP_VAULT_FILE.is_file():
            return None
        try:
            with open(NVFTP_VAULT_FILE, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError):
            return None
    if not isinstance(doc, dict) or int(doc.get("version") or 0) != _VAULT_VERSION:
        return None
    try:
        return _decrypt(doc)
    except (ValueError, RuntimeError):
        _logger.warning("NVFTP vault present but decrypt failed")
        return None
