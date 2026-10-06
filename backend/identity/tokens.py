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
"""Minting and verifying our own JWTs (FR-5, FR-5b, FR-4c, FR-4d, FR-6).

Three token types, because conflating them is an attack rather than an inconvenience: an **id**
token says who the subject is, an **access** token authorises a request, and a **refresh** token buys
a new access token without re-entering a password (FR-5b). Each carries `typ`, and
:func:`verify` demands the type the *caller* expects — PyJWT has no notion of this, so a library that
happily validates any of our tokens would let a long-lived refresh token be presented as an access
token and never expire in practice.

**PyJWT's defaults leave two holes, both silent.** Measured on 2.13.0 rather than assumed:

- ``iss`` is **not checked at all** unless ``issuer=`` is passed. The claim can name anyone.
- A token with **no ``exp`` claim is accepted** and never expires.

Neither raises, warns, or looks wrong at the call site, which is why verification here passes
``issuer=``/``audience=`` *and* an explicit ``require`` list, and why the tests assert the negative
cases. (``aud`` is the one PyJWT does check unprompted, and ``algorithms=[...]`` does correctly
reject ``alg: none`` and an HS512→HS256 substitution — so the pin works, it just has to be written.)

**What separates deployments is the key, not the claims.** ``iss`` and ``aud`` are constants for the
product, so they do not distinguish two Rack Status installs from each other; the derived signing key
does, because each deployment has its own secret. Validating ``iss``/``aud`` buys something
different and still worth having: it stops a token minted for another *audience* — most concretely a
federated provider's ID token, which is also a JWT — being presented as one of ours (FR-4d).

**The signing key is derived, not borrowed.** HKDF over ``RACK_WEB_SECRET_KEY`` with a labelled
``info``, so leaking the JWT key does not hand over Flask session forgery, and vice versa. Domain
separation is the goal; the input is already high-entropy, so this is not key stretching.

**Revocation is fail-closed** (FR-6). :func:`verify` refuses if no revocation checker has been
registered, rather than defaulting to "nothing is revoked". Phase 1 taught this one directly: a hook
that is silently absent produces wrong behaviour on some deployments and an error on none
(`test_config_materialize_hook.py` exists for exactly that). A missing registration here is a loud
total refusal, which is the correct failure direction for a security control and is caught by the
first request rather than by an incident.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from identity.principals import PrincipalError, parse_principal

#: Pinned. Passed to every ``jwt.decode`` as the *only* accepted algorithm, which is what makes
#: ``alg: none`` and an RS256→HS256 substitution fail (FR-4c). Symmetric is right here: there is one
#: party minting and verifying, so an asymmetric key buys nothing and costs key management.
ALGORITHM = "HS256"

#: 32 bytes: the SHA-256 output size, which RFC 7518 §3.2 gives as the minimum HMAC key length for
#: HS256. (PyJWT emits ``InsecureKeyLengthWarning`` below the hash size — at HS512 this length would
#: warn, which is a useful reminder that key length must track the algorithm.)
KEY_BYTES = 32

_logger = logging.getLogger(__name__)

TOKEN_ID = "id"
TOKEN_ACCESS = "access"
TOKEN_REFRESH = "refresh"
TOKEN_TYPES = (TOKEN_ID, TOKEN_ACCESS, TOKEN_REFRESH)

# --- Lifetimes ------------------------------------------------------------------------------------
#
# **What an access TTL does and does not bound here.** The original 15 minutes was chosen because
# FR-6 says "a withdrawn *grant* may lag by up to the access-token lifetime". That sentence describes
# a design where grants ride inside the token — and this implementation does not do that. `mint`
# puts only `sub`, `typ` and timing in the payload: no groups, no roles, no scopes. Membership and
# policy are read from the store on **every** request, and the revocation record is consulted on
# every verify.
#
# So lengthening these does not slow a revocation and does not staleness-bound an authorisation
# decision. What it lengthens is the window in which a **stolen token nobody has revoked** still
# works. That is a real cost and it is the one to weigh: 1 hour is four times 15 minutes for that
# case, and 7 days is a long-lived bearer credential.
#
# What keeps that tolerable is the revocation fast path, which is why it is load-bearing rather than
# a nicety: `revocation.revoke_subject` and `revoke_all_for` cut a session immediately regardless of
# how much life the token had left, and disabling, deleting or resetting a user all call one of them.
#
# Defaults raised 2026-09-01 on feature-lead direction: 15 min forced a re-authentication mid-task
# often enough to be the product's most-felt friction, and the auto-refresh in `apiFetch` did not
# exist yet.

#: Access token. Default one hour; a deployment may ask for up to a week.
ACCESS_TTL_SECONDS = 60 * 60
MAX_ACCESS_TTL_SECONDS = 7 * 24 * 60 * 60

#: The id token is a claims carrier for the UI, not a credential for the gate, so it tracks the
#: access lifetime rather than having a knob of its own.
ID_TTL_SECONDS = ACCESS_TTL_SECONDS

#: Refresh token. Default one week; extensible to a month. Revocable by ``jti``, which is the
#: combination FR-5b asks for — a renewal path that can itself be withdrawn.
#:
#: **This value *is* the session length, by design.** The refresh token is the session's source of
#: truth: `apiFetch` renews the short-lived access token from it silently (proactively before expiry
#: and reactively on a 401), and a refresh never rotates the refresh token itself — so a session
#: lives exactly from login until this lifetime elapses, no sliding window. To make operators stay
#: signed in longer or shorter, change *this* (or `RACK_WEB_REFRESH_TTL_SECONDS`); nothing else gates
#: it. Raised from one day to one week 2026-10-06: a day forced a re-login mid-work-week for no
#: security gain the access TTL and the revocation fast path were not already providing.
#:
#: A month means a browser left open for a month is still a live session, so the ceiling is
#: deliberately not "unbounded".
REFRESH_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60


def _configured_ttl(name: str, default: int, ceiling: int) -> int:
    """A lifetime from the environment, clamped to its ceiling and floored at a minute.

    Clamped with a warning rather than refused: taking startup down over a too-generous TTL would
    turn a configuration opinion into an outage. Warned rather than silent because an operator who
    asked for 90 days and got 30 needs to know which one is in force — a security parameter quietly
    disagreeing with its configuration is worse than either value.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        seconds = int(raw)
    except ValueError:
        _logger.warning("%s=%r is not an integer; using the default %ds", name, raw, default)
        return default
    if seconds < 60:
        _logger.warning("%s=%d is below the 60s floor; using 60s", name, seconds)
        return 60
    if seconds > ceiling:
        _logger.warning(
            "%s=%d exceeds the %ds ceiling; using %ds", name, seconds, ceiling, ceiling
        )
        return ceiling
    return seconds


def access_ttl() -> int:
    """Effective access-token lifetime. Read per call, so a restart is not needed to change it."""
    return _configured_ttl("RACK_WEB_ACCESS_TTL_SECONDS", ACCESS_TTL_SECONDS, MAX_ACCESS_TTL_SECONDS)


def refresh_ttl() -> int:
    """Effective refresh-token lifetime."""
    return _configured_ttl(
        "RACK_WEB_REFRESH_TTL_SECONDS", REFRESH_TTL_SECONDS, MAX_REFRESH_TTL_SECONDS
    )


#: Demanded present, not merely validated if present. ``exp`` is the one that matters most: PyJWT
#: accepts a token without it and treats it as eternal.
REQUIRED_CLAIMS = ("iss", "aud", "sub", "exp", "iat", "jti", "typ")

# --- The OIDC provider fork (Phase 2.5, FR-19) -----------------------------------------------
#
# Four things differ when `RACK_WEB_OIDC_ENABLED` is on, and **nothing else does**. They are
# collected here rather than scattered through `mint` and `verify` so the difference between the two
# modes can be read in one place — and so "off is identical to today" is inspectable rather than
# hoped for. `test_identity_tokens.py` passing unchanged with the flag off is the real assertion.
#
#   1. ES256 with a per-key `kid` header, instead of HS256 with none.
#   2. `token_use` instead of the custom `typ` claim. `typ` is a *header* parameter in JOSE with a
#      different meaning (RFC 9068 uses it for `at+jwt`), so a claim of the same name invites a
#      verifier to check the wrong one.
#   3. `iss` is the deployment's URL, not the opaque string `nvis-rack-status`. OIDC Discovery
#      requires it and conforming clients enforce it.
#   4. `auth_time` and `scope` appear.
#
# There is no third mode and no partial state: the same flag governs signing and the published
# endpoints, because HS256 tokens beside a published JWKS is a key that verifies nothing.

#: The claim naming the token's kind. Renamed rather than duplicated — carrying both would let a
#: verifier check whichever it happened to know about, which is how a type confusion survives.
#: Retired 2026-09-08 with the HS256 path. Kept as a name because the *string* still appears in
#: tokens minted before the cutover, and a reader of an old audit line needs to recognise it.
TYPE_CLAIM_LEGACY = "typ"

#: The token-type claim. `token_use` is the name AWS Cognito uses and the one a reader of an OIDC
#: token expects; `typ` is a JOSE *header* parameter, so putting a payload claim there was the kind
#: of near-miss that interoperates with nothing.
TYPE_CLAIM_OIDC = "token_use"
TYPE_CLAIM = TYPE_CLAIM_OIDC


def _oidc_mode() -> bool:
    """Always true. There is one token shape.

    Kept because nine call sites read it and the honest reading of those sites is "we are an OIDC
    provider". `RACK_WEB_OIDC_ENABLED` was retired on 2026-09-08 — see `identity.oidc.enabled`.
    """
    return True


def _type_claim() -> str:
    return TYPE_CLAIM


def _required_claims() -> list[str]:
    return [c for c in REQUIRED_CLAIMS if c != TYPE_CLAIM_LEGACY] + [TYPE_CLAIM]


def _effective_issuer() -> str:
    from identity import oidc

    return oidc.issuer_url()


#: No clock-skew allowance. One process mints and verifies against one clock, so leeway would only
#: extend the life of an expired token and loosen the FR-6 bound for nothing.
LEEWAY_SECONDS = 0

#: The pre-cutover audience. Retired 2026-09-08 and kept only so the string is recognisable in an
#: old token or audit line; nothing mints or accepts it.
AUDIENCE_LEGACY = "nvis-rack-status"

#: The audience this deployment mints and demands.
AUDIENCE_OIDC = "nvis-greenlight"


def audience() -> str:
    """The `aud` this deployment mints and demands.

    **This is where the product rename actually took effect**, and it was deliberately bundled with
    the ES256 cutover rather than shipped on its own. Changing `aud` refuses every token carrying the
    old value, so doing it separately would have cost a second mass logout for a change no operator
    can see. Making the provider unconditional ended every outstanding session once; the rename rode
    along in that same moment.
    """
    return AUDIENCE


#: Kept as a module constant because a lot of existing code and tests read `tokens.AUDIENCE`.
#:
#: It resolved to the LEGACY value while the provider was a flag, which was correct then and became
#: a live defect the moment the provider went unconditional: `audience()` returns this, so a stale
#: binding here means every token is minted with an audience the verifier no longer expects. Caught
#: by `test_the_audience_is_the_launch_name` — the mint and the verify disagreed and the error was
#: `InvalidAudienceError`, which reads like a forged token rather than a constant left behind.
AUDIENCE = AUDIENCE_OIDC


class TokenError(Exception):
    """Any token that must not be honoured. Callers map this to a single uniform failure."""


class TokenRevoked(TokenError):
    """Subject or token was revoked (FR-6). Separate because it is worth auditing distinctly from a
    malformed or expired token — it means someone *was* valid and is not any more."""


_lock = threading.RLock()
_revocation_checker: Callable[[dict[str, Any]], None] | None = None


def set_revocation_checker(checker: Callable[[dict[str, Any]], None]) -> None:
    """Register the FR-6 check. The callable receives verified claims and raises to refuse.

    Passing verified *claims* rather than a subject string means a checker can revoke by ``jti`` (one
    refresh token), by ``sub`` (a user), or by issue time (everything minted before a password
    change) without changing this signature.
    """
    global _revocation_checker
    with _lock:
        _revocation_checker = checker


def revocation_checker_registered() -> bool:
    return _revocation_checker is not None


def allow_all_for_tests() -> None:
    """Register a checker that revokes nothing.

    Named so it can never be mistaken for a default. The point of fail-closed is that *no* code path
    reaches a permissive state implicitly; a test that wants one asks for it in a line a reviewer
    will notice.
    """
    set_revocation_checker(lambda _claims: None)


def reset_for_tests() -> None:
    global _revocation_checker
    with _lock:
        _revocation_checker = None


def issuer() -> str:
    """Who minted the token. Overridable so an operator running two deployments can tell their audit
    trails apart; the *security* boundary between deployments is the key, not this string."""
    return (os.environ.get("RACK_WEB_TOKEN_ISSUER") or "").strip() or "nvis-rack-status"


def signing_key() -> bytes:
    """HKDF-SHA256 over the deployment secret, with a labelled ``info``.

    Read from the environment rather than by importing ``app``: ``app.py`` bootstraps
    ``RACK_WEB_SECRET_KEY`` from ``var/secrets/`` into ``os.environ`` at startup, and an ``identity``
    module importing ``app`` would recreate the cycle ``backend/api/`` exists to remove.

    Not cached. The derivation is microseconds, and caching would mean a rotated secret kept minting
    against the old key until a restart — the kind of staleness that is invisible until it matters.
    """
    secret = (os.environ.get("RACK_WEB_SECRET_KEY") or "").strip()
    if not secret:
        raise TokenError(
            "RACK_WEB_SECRET_KEY is not set, so no signing key can be derived. app.py bootstraps it "
            "at startup; a bare import outside the app must set it explicitly."
        )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=None,
        # Domain separation: change this string and every existing token stops verifying, which is
        # the intended blast radius of a deliberate invalidation.
        info=b"nvis-rack-status/jwt/v1",
    ).derive(secret.encode())


def mint(
    principal: str,
    token_type: str,
    ttl_seconds: int | None = None,
    claims: dict[str, Any] | None = None,
    auth_time: float | None = None,
    scope: str | None = None,
) -> str:
    """Sign a token for ``principal`` (a ``user:<login>`` string).

    The principal is parsed rather than trusted: :func:`identity.principals.parse_principal` is the
    single grammar (FR-15a), so a caller cannot slip an unparseable ``sub`` into a token that later
    code will try to split by hand.

    ``scope`` is an explicit parameter rather than something passed through ``claims`` because it is
    load-bearing: `/userinfo` decides what to release from it, so a token whose scope disagreed with
    what was granted would hand out attributes nobody authorised. It is also a reserved claim name
    for that reason -- two ways to set one security-relevant value is one way too many.
    """
    if token_type not in TOKEN_TYPES:
        raise TokenError(f"unknown token type {token_type!r}; expected one of {TOKEN_TYPES}")
    _parsed_or_refused(principal)

    if ttl_seconds is None:
        access = access_ttl()
        ttl_seconds = {
            TOKEN_ID: access,
            TOKEN_ACCESS: access,
            TOKEN_REFRESH: refresh_ttl(),
        }[token_type]
    if ttl_seconds <= 0:
        raise TokenError("ttl_seconds must be positive")

    # The explicit override is capped too. Without this, the ceiling would only constrain the
    # *configured* default and any caller passing `ttl_seconds` could mint a decade-long token —
    # a ceiling that the shortest path around it ignores is not a ceiling. Tests legitimately mint
    # far-future tokens, so this refuses rather than clamps: a test asserting on an unbounded
    # lifetime should fail loudly, not silently receive a 7-day one.
    ceiling = MAX_REFRESH_TTL_SECONDS if token_type == TOKEN_REFRESH else MAX_ACCESS_TTL_SECONDS
    if ttl_seconds > ceiling:
        raise TokenError(
            f"ttl_seconds={ttl_seconds} exceeds the {ceiling}s ceiling for a {token_type} token"
        )

    # Fractional seconds, which RFC 7519 §2 explicitly permits for NumericDate. Integer `iat` cannot
    # distinguish a token minted just *before* a revocation cutoff from one minted just after, and
    # `identity.revocation` compares exactly that. The first version of this used whole seconds and
    # forced the cutoff a second into the future to stay safe, which locked a user out of a *fresh*
    # login for up to a second after their own password change. Sub-second `iat` removes the
    # ambiguity rather than trading one error for the other.
    now = time.time()
    payload: dict[str, Any] = {
        "iss": _effective_issuer(),
        "aud": audience(),
        "sub": principal,
        "iat": now,
        "exp": now + float(ttl_seconds),
        # Per-token id, so a single refresh token can be revoked without revoking the user.
        "jti": uuid.uuid4().hex,
        _type_claim(): token_type,
    }

    if _oidc_mode():
        # When the user actually authenticated, which is NOT when this token was minted.
        #
        # The distinction only shows up on refresh, and it is invisible when wrong: a renewed token
        # whose `auth_time` is "now" claims the user authenticated a moment ago, when they may have
        # signed in weeks earlier. Every `max_age` check a relying party makes is then answered with
        # a plausible, well-formed lie. Callers renewing a session must pass the original value
        # through; `mint` cannot recover it.
        payload["auth_time"] = float(auth_time) if auth_time is not None else now
        if token_type in (TOKEN_ACCESS, TOKEN_REFRESH):
            # Both, not just the access token: the refresh token is what a renewal reads the
            # original grant from, and a refresh that cannot recall its scope can only either
            # guess or widen. RFC 6749 §6 forbids widening, so it has to be recorded here.
            payload["scope"] = scope or "openid"

    for reserved in (*REQUIRED_CLAIMS, TYPE_CLAIM_OIDC, "scope"):
        if claims and reserved in claims:
            raise TokenError(f"caller may not override the {reserved!r} claim")
    payload.update(claims or {})

    if _oidc_mode():
        from identity import keys as keys_mod

        key = keys_mod.key_set().ensure()
        return jwt.encode(
            payload,
            key.private,
            algorithm=keys_mod.ALGORITHM,
            # The `kid` is what lets a verifier pick the right key out of the JWKS without
            # trial-verifying against every one of them, and it is what makes rotation
            # non-disruptive rather than a flag day.
            headers={"kid": key.kid},
        )
    return jwt.encode(payload, signing_key(), algorithm=ALGORITHM)


def _verification_material(token: str) -> tuple[Any, str]:
    """The key and algorithm to verify this token with.

    In legacy mode there is one of each. In OIDC mode the key is chosen by the token's own ``kid``,
    which means reading an **unverified** header — unavoidable and standard, because you cannot
    check a signature before you know which key to check it against. Two rules make that safe:
    nothing from the header is used for anything except *looking up* a key we already hold, and the
    algorithm is pinned from our own configuration rather than taken from the header.

    That second rule is what defeats the attack the published key creates. With a JWKS available, an
    attacker can take our **public** key, sign a token with it as an HMAC secret, and set
    ``alg: HS256``. A verifier that trusts the header's algorithm then validates it against a value
    we handed out. Passing ``algorithms=[ES256]`` refuses it — but only because it is written.
    """
    # The HS256 branch that stood here is gone with the flag (2026-09-08). It mattered: while it
    # existed, `RACK_WEB_SECRET_KEY` signed JWTs, which is what made INV-17 a JWT hazard — repoint
    # the secret and every outstanding token stops verifying. **That no longer applies to JWTs.**
    # The signing key is now the ES256 key set under `RACK_WEB_SIGNING_KEY_DIR`, and the secret
    # still protects the Flask session and the token vault, so it is not free to change either.
    from identity import keys as keys_mod

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise TokenError(f"token rejected: {type(exc).__name__}") from exc

    kid = header.get("kid")
    if not kid:
        # No fallback to "try the current key". A missing `kid` must not be a way to have a token
        # verified against whichever key happens to be active, which would re-admit exactly the
        # tokens a rotation is meant to retire.
        raise TokenError("token has no `kid`; it cannot be matched to a signing key")

    key = keys_mod.key_set().verifier(str(kid))
    if key is None:
        raise TokenError(f"token names an unknown or retired signing key ({kid!r})")
    return key.private.public_key(), keys_mod.ALGORITHM


def verify(token: str, expect_type: str) -> dict[str, Any]:
    """Validate a token and return its claims, or raise :class:`TokenError`.

    ``expect_type`` is mandatory and has no default, so token-type confusion needs a deliberate
    mistake rather than an omission: a 12-hour refresh token presented as an access token would
    otherwise authorise requests for 12 hours.
    """
    if expect_type not in TOKEN_TYPES:
        raise TokenError(f"unknown expected type {expect_type!r}")
    if _revocation_checker is None:
        # FR-6 fail-closed. Loud, because the alternative is honouring revoked sessions silently.
        raise TokenError(
            "no revocation checker registered, so FR-6 cannot be satisfied and no token may be "
            "honoured. Call identity.tokens.set_revocation_checker() during startup."
        )
    verifying_key, algorithm = _verification_material(token)
    try:
        claims = jwt.decode(
            token,
            verifying_key,
            algorithms=[algorithm],  # pinned: rejects alg:none and any substitution (FR-4c)
            audience=audience(),
            issuer=_effective_issuer(),  # without this kwarg PyJWT does not check `iss` at all
            leeway=LEEWAY_SECONDS,
            options={
                "require": _required_claims(),  # without this, a token lacking `exp` is eternal
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except jwt.PyJWTError as exc:
        # Uniform on purpose: the type of failure is useful in a log, not in a response.
        raise TokenError(f"token rejected: {type(exc).__name__}") from exc

    actual = claims.get(_type_claim())
    if actual != expect_type:
        raise TokenError(f"token type is {actual!r}, expected {expect_type!r}")

    _parsed_or_refused(str(claims.get("sub", "")))  # the grammar again, on the way out
    _revocation_checker(claims)
    return claims


def _parsed_or_refused(principal: str) -> tuple[str, str]:
    """Apply the principal grammar, reporting a failure as a :class:`TokenError`.

    Exists so this module keeps one exception type. ``parse_principal`` raises ``PrincipalError``,
    and letting that escape :func:`verify` would mean a caller correctly catching ``TokenError``
    still gets an unhandled exception — a malformed ``sub`` becoming a 500 instead of a clean
    refusal. Found by a test that expected the documented contract and did not get it.
    """
    try:
        return parse_principal(principal)
    except PrincipalError as exc:
        raise TokenError(f"subject is not a valid principal: {exc}") from exc
