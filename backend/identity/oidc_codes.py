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
"""Authorization codes, with PKCE (FR-11, FR-11a, NFR-5).

A code is a 60-second bearer credential exchangeable for tokens, so nearly everything here is about
making sure it is used exactly once, by the party it was issued to.

THE REPLAY RULE IS THE PART WORTH READING
------------------------------------------
Presenting a code twice does not merely fail the second attempt -- it **revokes the tokens the first
attempt produced** (FR-11a, and OAuth 2.1 §4.1.3).

The reasoning: a code is single-use and travels through a browser redirect. If it arrives twice, the
most likely explanation is that it leaked -- from a referrer header, a proxy log, browser history --
and somebody else got there first or second. Declining the duplicate politely leaves the attacker
holding whatever the first exchange minted. Withdrawing those tokens costs a legitimate user one
re-login in the rare case of a double-submit, and costs an attacker the whole session in the case
that matters.

PKCE IS MANDATORY AND ONLY S256
-------------------------------
``plain`` offers no protection against an intercepted code -- the verifier *is* the challenge -- and
exists in the RFC only for clients that cannot compute a SHA-256. We have no such client, so
accepting it would be a weakness with no beneficiary.

THE STORE IS IN-PROCESS, WHICH IS A CONSTRAINT AND NOT AN OVERSIGHT
--------------------------------------------------------------------
This is the **second** piece of in-process authentication state, after the SRP challenge table, and
it inherits the same single-worker dependency. With two workers a code issued by one and redeemed
against the other fails roughly half the time, which presents as an intermittent client bug rather
than a configuration error -- so `assert_single_worker_safe()` covers it, and the table is bounded
so an unauthenticated caller cannot grow it without limit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

_logger = logging.getLogger(__name__)

#: RFC 6749 §4.1.2 recommends a maximum of ten minutes and "as short as possible". Sixty seconds is
#: the time between a redirect landing and a back-channel exchange, which is a network round trip.
CODE_TTL_SECONDS = 60

#: Bounded so an unauthenticated caller cannot exhaust memory. Eviction is oldest-first; at 60-second
#: lifetimes this ceiling is far above any real burst.
MAX_OUTSTANDING = 2048

METHOD_S256 = "S256"


class CodeError(Exception):
    """Any reason a code must not be exchanged."""


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass
class AuthorizationCode:
    """One issued code and everything the exchange must check it against."""

    code: str
    client_id: str
    principal: str
    redirect_uri: str
    code_challenge: str
    nonce: str | None
    auth_time: float
    scope: str
    issued_at: float
    expires_at: float
    #: Claims of the tokens minted from this code, so a replay can withdraw them (FR-11a).
    #: Stored as `{jti, exp}` rather than bare ids because `revocation.revoke_token` needs the
    #: expiry to prune its own row once the token would have lapsed anyway.
    minted_tokens: list[dict] = field(default_factory=list)
    redeemed: bool = False


class CodeStore:
    """The outstanding authorization codes for this process."""

    def __init__(self, *, ttl_seconds: int = CODE_TTL_SECONDS, capacity: int = MAX_OUTSTANDING):
        self.ttl_seconds = ttl_seconds
        self.capacity = capacity
        self._lock = threading.RLock()
        self._codes: dict[str, AuthorizationCode] = {}

    def issue(
        self,
        *,
        client_id: str,
        principal: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        nonce: str | None = None,
        auth_time: float | None = None,
        scope: str = "openid",
        now: float | None = None,
    ) -> AuthorizationCode:
        """Mint a code. Refuses anything but S256 PKCE."""
        if code_challenge_method != METHOD_S256:
            raise CodeError(
                f"code_challenge_method must be {METHOD_S256!r}; got {code_challenge_method!r}. "
                f"'plain' offers no protection against an intercepted code and is not accepted."
            )
        if not code_challenge or len(code_challenge) < 43:
            # 43 characters is the base64url length of a SHA-256 digest; anything shorter cannot be
            # one, so it is a malformed challenge rather than an unusual one.
            raise CodeError("code_challenge is missing or too short to be a SHA-256 digest")

        now = time.time() if now is None else now
        with self._lock:
            self._evict_locked(now)
            entry = AuthorizationCode(
                code=secrets.token_urlsafe(32),
                client_id=client_id,
                principal=principal,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                nonce=nonce,
                auth_time=now if auth_time is None else auth_time,
                scope=scope,
                issued_at=now,
                expires_at=now + self.ttl_seconds,
            )
            self._codes[entry.code] = entry
            return entry

    def redeem(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
        now: float | None = None,
    ) -> AuthorizationCode:
        """Exchange a code exactly once, or raise.

        On a replay this raises **and** revokes whatever the first exchange minted, so the caller
        does not need to remember to. Doing it here rather than at the call site is deliberate: the
        store is the only place that knows a code has been seen before.
        """
        now = time.time() if now is None else now
        with self._lock:
            entry = self._codes.get(code)
            if entry is None:
                raise CodeError("unknown or expired authorization code")

            if entry.redeemed:
                self._revoke_issued_locked(entry)
                self._codes.pop(code, None)
                raise CodeError(
                    "authorization code has already been redeemed; the tokens issued from it have "
                    "been revoked because a repeated code most likely means it leaked"
                )

            if now >= entry.expires_at:
                self._codes.pop(code, None)
                raise CodeError("authorization code has expired")

            # Bound to the client and the redirect it was issued for. Without the first check, a
            # code intercepted from one client could be exchanged by another; without the second, a
            # code issued for one registered URI could be redeemed as though it came from a
            # different one.
            if not hmac.compare_digest(entry.client_id, client_id):
                raise CodeError("authorization code was not issued to this client")
            if not hmac.compare_digest(entry.redirect_uri, redirect_uri):
                raise CodeError("redirect_uri does not match the one the code was issued for")

            expected = _b64u(hashlib.sha256(code_verifier.encode("ascii")).digest())
            if not hmac.compare_digest(expected, entry.code_challenge):
                raise CodeError("PKCE verification failed")

            entry.redeemed = True
            return entry

    def record_minted(self, code: str, claims: list[dict]) -> None:
        """Remember what a redemption produced, so a later replay can withdraw it.

        Called after minting rather than during, so a failure to mint leaves nothing recorded.
        """
        with self._lock:
            entry = self._codes.get(code)
            if entry is not None:
                entry.minted_tokens.extend(
                    {"jti": c.get("jti"), "exp": c.get("exp")} for c in claims if c.get("jti")
                )

    def _revoke_issued_locked(self, entry: AuthorizationCode) -> None:
        from identity import revocation

        for claims in entry.minted_tokens:
            try:
                revocation.revoke_token(claims, reason="authorization code replayed")
            except Exception:
                # Best effort per token: one failure must not stop the others being withdrawn.
                _logger.exception("oidc: could not revoke %s after code replay", claims.get("jti"))
        if entry.minted_tokens:
            _logger.warning(
                "oidc: authorization code replayed for client %s; revoked %d token(s)",
                entry.client_id,
                len(entry.minted_tokens),
            )

    def _evict_locked(self, now: float) -> None:
        expired = [c for c, e in self._codes.items() if now >= e.expires_at]
        for code in expired:
            self._codes.pop(code, None)
        while len(self._codes) >= self.capacity:
            oldest = min(self._codes.values(), key=lambda e: e.issued_at)
            self._codes.pop(oldest.code, None)

    def outstanding(self) -> int:
        with self._lock:
            return len(self._codes)

    def reset(self) -> None:
        with self._lock:
            self._codes.clear()


_store = CodeStore()


def store() -> CodeStore:
    return _store


def verifier_for(challenge_seed: str) -> tuple[str, str]:
    """Helper for tests and clients: a (verifier, S256 challenge) pair."""
    verifier = _b64u(hashlib.sha256(challenge_seed.encode()).digest())
    challenge = _b64u(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge
