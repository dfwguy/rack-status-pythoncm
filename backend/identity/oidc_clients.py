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
"""Registered OIDC clients (FR-15, FR-15a).

**Configured, never self-registered.** Dynamic client registration (RFC 7591) is an explicit
non-goal: it is an unauthenticated way to create trusted redirect targets, and the consumer list for
this deployment is short and known. A client is added by an operator editing a file.

THE REDIRECT URI IS MATCHED BY EXACT STRING, AND THIS IS THE IMPORTANT PART
---------------------------------------------------------------------------
No prefix matching, no wildcards, no subdomain matching, no ignoring a trailing slash. RFC 6749
§3.1.2.2 requires exact comparison and every well-known instance of this bug is somebody relaxing
it "just for a path suffix".

The consequence of getting it wrong is not a broken redirect: the authorization **code** is handed
to whatever origin the attacker nominated, and a code is exchangeable for tokens. That is a full
account takeover from a single permissive comparison, which is why the matcher here is a literal
``in`` against a frozen set rather than anything that could grow a special case.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CLIENTS_FILE = REPO_ROOT / "var" / "secrets" / "oidc" / "clients.json"

PUBLIC = "public"
CONFIDENTIAL = "confidential"


class ClientError(Exception):
    """An unusable client registration, or a request naming one that does not exist."""


@dataclass(frozen=True)
class Client:
    """One registered relying party."""

    client_id: str
    redirect_uris: frozenset[str]
    client_type: str = PUBLIC
    #: Only for confidential clients. A public client (a browser SPA, a CLI) cannot keep one, which
    #: is exactly why PKCE is mandatory rather than optional here.
    client_secret: str | None = None
    name: str = ""
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"openid"}))

    def permits_redirect(self, candidate: str) -> bool:
        """Exact string comparison. Deliberately not clever.

        A candidate differing by a trailing slash, a query string, a path suffix or a subdomain is a
        different URI and is refused. If a client needs two URIs it registers two.
        """
        return candidate in self.redirect_uris

    @property
    def is_confidential(self) -> bool:
        return self.client_type == CONFIDENTIAL


def _validate_redirect(uri: str, client_id: str) -> str:
    """Reject a registration that could not be matched safely later.

    Checked at load rather than at use: a malformed entry should fail when an operator edits the
    file, not during somebody's login. Fragments are refused because RFC 6749 §3.1.2 forbids them,
    and a wildcard is refused explicitly so the error names the actual problem rather than silently
    never matching.
    """
    if "*" in uri:
        raise ClientError(
            f"client {client_id!r} registers a wildcard redirect_uri {uri!r}. Wildcards are not "
            f"supported: an over-broad match hands the authorization code to an attacker-chosen "
            f"origin, and a code is exchangeable for tokens. Register each URI in full."
        )
    parsed = urlparse(uri)
    if not parsed.scheme or not parsed.netloc:
        raise ClientError(f"client {client_id!r} has redirect_uri {uri!r} with no scheme or host")
    if parsed.fragment:
        raise ClientError(f"client {client_id!r} has redirect_uri {uri!r} with a fragment")
    loopback = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not loopback:
        raise ClientError(
            f"client {client_id!r} has non-https redirect_uri {uri!r} (http is permitted only for "
            f"loopback, which is how a CLI receives a code)"
        )
    return uri


def _client_from_dict(raw: dict) -> Client:
    client_id = str(raw.get("client_id") or "").strip()
    if not client_id:
        raise ClientError("a client registration has no client_id")

    uris = raw.get("redirect_uris") or []
    if not isinstance(uris, list) or not uris:
        raise ClientError(f"client {client_id!r} registers no redirect_uris")

    client_type = str(raw.get("client_type") or PUBLIC).strip().lower()
    if client_type not in (PUBLIC, CONFIDENTIAL):
        raise ClientError(f"client {client_id!r} has unknown client_type {client_type!r}")

    secret = raw.get("client_secret")
    if client_type == CONFIDENTIAL and not secret:
        raise ClientError(f"confidential client {client_id!r} has no client_secret")
    if client_type == PUBLIC and secret:
        # Refused rather than ignored: a secret on a public client is a misunderstanding about where
        # the client runs, and a browser or CLI cannot keep one. Silently dropping it would leave
        # the operator believing the client is authenticated when it is not.
        raise ClientError(
            f"public client {client_id!r} carries a client_secret. A public client cannot keep a "
            f"secret; mark it confidential or remove the secret."
        )

    return Client(
        client_id=client_id,
        redirect_uris=frozenset(_validate_redirect(str(u), client_id) for u in uris),
        client_type=client_type,
        client_secret=str(secret) if secret else None,
        name=str(raw.get("name") or client_id),
        scopes=frozenset(raw.get("scopes") or ["openid"]),
    )


_registry: dict[str, Client] | None = None
_registry_key: tuple[str, float] | None = None
_lock = threading.RLock()


def _clients_file() -> Path:
    raw = (os.environ.get("RACK_WEB_OIDC_CLIENTS_FILE") or "").strip()
    return Path(raw) if raw else DEFAULT_CLIENTS_FILE


def registry() -> dict[str, Client]:
    """All registered clients, reloaded when the file changes.

    Keyed on (path, mtime) so an operator editing the file does not need a restart -- and so the
    common case is a dict lookup rather than a parse. Reading performs **no write**, which matters
    because a write here would change the mtime that is part of this very cache key.
    """
    global _registry, _registry_key
    path = _clients_file()
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        # No file is a valid state: a deployment with no relying parties still serves the SPA.
        with _lock:
            _registry, _registry_key = {}, None
        return {}

    with _lock:
        if _registry is not None and _registry_key == key:
            return _registry
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ClientError(f"{path} is unreadable: {exc}") from exc

        entries = raw.get("clients") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            raise ClientError(f"{path} must hold a list of clients, or an object with 'clients'")

        loaded: dict[str, Client] = {}
        for entry in entries:
            client = _client_from_dict(entry)
            if client.client_id in loaded:
                raise ClientError(f"{path} registers {client.client_id!r} twice")
            loaded[client.client_id] = client

        _registry, _registry_key = loaded, key
        _logger.info("oidc: loaded %d registered client(s) from %s", len(loaded), path)
        return loaded


def get(client_id: str) -> Client:
    """The named client, or raise. Never returns a default."""
    client = registry().get((client_id or "").strip())
    if client is None:
        raise ClientError(f"unknown client_id {client_id!r}")
    return client


def reset_for_tests() -> None:
    global _registry, _registry_key
    with _lock:
        _registry, _registry_key = None, None
