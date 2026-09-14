# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Turning a pasted or uploaded private key into a file SSH will accept.

Author: Rafael M. Koike & Cursor Agent ("Ada")

``Cluster.connection.ssh_key`` is a **path**, because that is what ``ssh -i`` takes and what
``host_exec`` forwards. The setup wizard collects key *material* -- an operator pastes it or
uploads the file -- so something has to write it down, and writing a private key is the one
operation in this feature where getting the details slightly wrong is a security problem rather
than a bug.

Three details, each with a reason:

**The file is created with mode 0600, not chmoded afterwards.** ``os.open(..., 0o600)`` rather
than ``open()`` then ``os.chmod``: a plain ``open()`` takes the umask, so the file exists
world-readable for the moment between the two calls, and OpenSSH refuses a key whose mode is too
open anyway. This is trap 3 in ``identity-auth-traps.mdc`` -- the same mistake made the identity
store world-readable, and the lesson recorded there is to create with the mode you want rather
than correct it later.

**The material never reaches a log.** Not at debug, not in an exception. The functions here take
the body and return a path; the body is never formatted into a message, and callers are given a
fingerprint to report instead.

**The path is checked before it is written.** The name is derived from ids the catalog already
validated, and then confirmed to sit under the key directory, because a key written outside it
would be a file this deployment cannot account for.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

_logger = logging.getLogger(__name__)

#: Owner-only, and the mode OpenSSH requires of a private key. Passed to `os.open`, so it is the
#: mode the file is *created* with.
_KEY_MODE = 0o600

#: Owner-only directory, for the same reason.
_DIR_MODE = 0o700

#: Same character set the catalog allows in an id, so a name derived from ids cannot escape.
_SAFE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

#: What an OpenSSH or PEM private key starts with. Checked so a pasted *public* key -- the
#: commonest paste mistake, and one that fails much later with a confusing SSH error -- is
#: refused here, where the message can say which half was pasted.
_PRIVATE_HEADERS = (
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
)

_PUBLIC_PREFIXES = ("ssh-rsa ", "ssh-ed25519 ", "ecdsa-sha2-", "ssh-dss ")


class KeyRejected(ValueError):
    """The material is not a usable private key. The message is safe to show a user."""


def key_dir() -> Path:
    """Where keys collected through the UI live: ``var/secrets/ssh``.

    Beside the other secrets this deployment writes (the signing key, the vault), which is the
    directory already excluded from images and backups.
    """
    from paths import REPO_ROOT

    override = (os.environ.get("NVIS_SSH_KEY_DIR") or "").strip()
    return Path(override) if override else REPO_ROOT / "var" / "secrets" / "ssh"


def fingerprint(material: str) -> str:
    """A short SHA-256 of the material, for logs and for the UI to echo back.

    Not the OpenSSH fingerprint: computing that means parsing the key, and this only has to let
    a human confirm that the thing stored is the thing they pasted.
    """
    digest = hashlib.sha256(material.strip().encode("utf-8", "replace")).hexdigest()
    return f"sha256:{digest[:16]}"


def looks_like_private_key(material: str) -> None:
    """Raise `KeyRejected` unless *material* is plausibly an SSH private key.

    Deliberately shallow -- it does not parse the key, because a key this process cannot parse
    may still be one the head node's SSH accepts. It catches the two mistakes that are worth a
    clear message: an empty box, and a public key pasted where the private one belongs.
    """
    body = (material or "").strip()
    if not body:
        raise KeyRejected("no key material: paste the private key or choose a key file")
    if body.startswith(_PUBLIC_PREFIXES):
        raise KeyRejected(
            "that is a public key (it starts with a key type). Paste the matching private key "
            "instead -- the file without the .pub extension."
        )
    if not body.startswith(_PRIVATE_HEADERS):
        raise KeyRejected(
            "that does not look like an SSH private key. It should begin with a line such as "
            "-----BEGIN OPENSSH PRIVATE KEY-----."
        )


def key_path(project_id: str, cluster_id: str) -> Path:
    """The file this project/cluster's key is written to."""
    for value, label in ((project_id, "project"), (cluster_id, "cluster")):
        if not _SAFE.match((value or "").strip()):
            raise KeyRejected(f"invalid {label} id for a key filename: {value!r}")
    directory = key_dir()
    path = directory / f"{project_id.strip()}-{cluster_id.strip()}.key"
    # Belt and braces after the character check: a name built from validated ids cannot escape,
    # and this is the assertion that keeps that true if the naming ever changes.
    if directory.resolve() not in path.resolve().parents:
        raise KeyRejected("refusing to write a key outside the key directory")
    return path


def write_key(project_id: str, cluster_id: str, material: str) -> tuple[str, str]:
    """Store *material* and return ``(path, fingerprint)``.

    The material is not logged, not echoed, and not returned.
    """
    looks_like_private_key(material)
    path = key_path(project_id, cluster_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The directory too: a 0600 file inside a world-readable directory still leaks its name and
    # its existence, and `ssh` will read a key out of a loose directory without complaining.
    os.chmod(path.parent, _DIR_MODE)

    body = material.strip() + "\n"
    # O_TRUNC rather than unlink-and-replace: an atomic replace would install the *temp* file's
    # mode, which is the trap this module's docstring names. Created with 0600 from the start.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _KEY_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
    except Exception:
        # No material in the message: the exception text reaches a log.
        _logger.error("ssh key: could not write %s", path)
        raise
    # Re-assert on the rewrite path: O_CREAT does not change the mode of a file that already
    # exists, so a key written once by hand at 0644 would stay 0644 forever.
    os.chmod(path, _KEY_MODE)

    fp = fingerprint(body)
    _logger.info("ssh key stored: path=%s fingerprint=%s bytes=%d", path, fp, len(body))
    return str(path), fp


def describe(path: str) -> dict[str, object]:
    """What the UI may know about a stored key: never its contents."""
    p = Path(path) if path else None
    if p is None or not p.exists():
        return {"path": str(path or ""), "exists": False}
    return {
        "path": str(p),
        "exists": True,
        "mode": oct(p.stat().st_mode & 0o777),
        "bytes": p.stat().st_size,
    }
