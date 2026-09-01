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
"""The deployment's token-signing key set (Phase 2.5, FR-1..FR-4f).

**This is one half of the deployment's cryptographic instance identity.** The other is the TLS key
and certificate that `bin/gen-tls-cert.py` mints on first boot. They answer two different questions
about the same instance — *who am I talking to* and *who issued this token* — and they bootstrap
together, in `var/secrets/`, on the same curve.

**They are deliberately not the same key.** The TLS key is presented in every handshake and lives in
gunicorn; this one never leaves the app process. Compromising the TLS key means impersonating the
server; compromising this one means minting a token for **any user**, which is a total
authentication bypass. Reusing one key for both would also couple their lifetimes, so a routine
certificate re-issue would invalidate every outstanding token.

THE LIFETIME MODEL — one configured value, one derived cutoff
-------------------------------------------------------------
A key has a single configured validity, ``not_after`` (default 18 months). It stops *signing*
earlier than that::

    signing_cutoff = not_after − T_max        # T_max = the configured refresh-token ceiling

so that the last token it signs has fully expired by the time the key stops *verifying*. The
alternative — configuring the signing period and the verification tail separately — permits a
combination in which tokens outlive their key, and then needs a guard to catch it. Deriving the
cutoff makes that state unreachable instead.

Getting this wrong does not look like a misconfiguration. It looks like operators being logged out
at random, on a schedule nobody connects to a key setting, because tokens stop verifying *before the
``exp`` they advertise*.

``T_max`` is **read** from the configured ceiling, never hard-coded: the ceiling is itself
configurable, so a constant here would mean raising it later silently reintroduces the exact failure
this design exists to prevent.

ROTATION APPENDS, IT DOES NOT REPLACE
-------------------------------------
One key is active (signs); every unexpired key verifies and appears in the JWKS. Rotation adds a new
active key and demotes the incumbent to verify-only. Rotating by *replacement* would invalidate
every outstanding token at once — the same event as the FR-18 cutover — and a rotation that logs
everyone out is one nobody will ever schedule, which means the key is never rotated at all.

Steady state is therefore **two** keys, briefly three if an emergency rotation lands while a demoted
key is still inside its tail. Nothing should depend on that count; Microsoft explicitly refuses to
promise one for Entra, and for the same reason we do not document one.

WHY ``kid`` IS A THUMBPRINT AND NOT A COUNTER
---------------------------------------------
RFC 7638 thumbprint of the public JWK. A verifier can *derive* it from the key it was handed, rather
than trusting a label we chose. It is stable across restarts with no extra persisted state — and a
counter needs state, which if it ever resets produces two different keys both claiming ``kid: 1``,
where the second silently fails to verify the first's tokens.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

_logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Beside the TLS material and the deployment secret, on the ``rack-data`` volume, already gitignored.
DEFAULT_KEY_DIR = REPO_ROOT / "var" / "secrets" / "oidc"

#: P-256 / ES256. Same curve `bin/gen-tls-cert.py` already uses for TLS, so the deployment has one
#: curve rather than two. Chosen over RS256 on token size (359 bytes against 615) after measuring;
#: verification latency did not discriminate — both are ~100x inside the NFR-1 budget.
CURVE = ec.SECP256R1
ALGORITHM = "ES256"

_SECONDS_PER_MONTH = 30 * 24 * 60 * 60
DEFAULT_VALIDITY_SECONDS = 18 * _SECONDS_PER_MONTH

#: A key must outlive the longest token it can sign, or the cutoff lands in the past and it can
#: never sign at all. Startup refuses rather than serving a deployment that cannot mint a session.
MIN_VALIDITY_MARGIN_SECONDS = 60

#: The intended floor on how long a key actually *signs*, as distinct from how long it stays valid.
#:
#: Feature-lead direction: "at least 1 year + extra time to support the last issued token with the
#: longest TTL". At the 18-month default and today's 1-month ceiling the derived signing period is
#: 17 months, so the floor is met with five months to spare — but it is met *by the default*, not by
#: anything in the code, and a lowered validity would quietly erode it.
#:
#: **Warned, not refused**, and the distinction is deliberate. A short signing period is not unsafe:
#: every token still verifies for its full life, because the cutoff derivation guarantees that
#: independently. It is *operationally* poor — it churns the JWKS, which is the exact concern that
#: motivated a long-lived key in the first place, and a client that mishandles a rollover fails
#: intermittently. Refusing to start over a working-but-churny configuration would be the wrong
#: severity; saying nothing would let the intent evaporate silently.
MIN_INTENDED_SIGNING_PERIOD_SECONDS = 365 * 24 * 60 * 60


class KeyError_(Exception):
    """Anything that makes the key set unusable. Named to avoid shadowing the builtin."""


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def thumbprint(public_key: ec.EllipticCurvePublicKey) -> str:
    """RFC 7638 JWK thumbprint, base64url, used as the ``kid``.

    The member set and their order are **fixed by the RFC** (``crv``, ``kty``, ``x``, ``y`` for EC,
    lexicographic, no whitespace). That is the whole point: two implementations hashing the same key
    must produce the same string, so a client can confirm the ``kid`` names the key it was given
    rather than taking our word for it. Deviating here — adding a member, pretty-printing the JSON —
    produces a plausible-looking identifier that no other implementation agrees with.
    """
    numbers = public_key.public_numbers()
    size = (public_key.curve.key_size + 7) // 8
    canonical = {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64u(numbers.x.to_bytes(size, "big")),
        "y": _b64u(numbers.y.to_bytes(size, "big")),
    }
    payload = json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()
    return _b64u(hashlib.sha256(payload).digest())


@dataclass(frozen=True)
class SigningKey:
    """One key in the set, with the two dates that govern it."""

    kid: str
    private: ec.EllipticCurvePrivateKey
    created_at: float
    not_after: float

    def signing_cutoff(self, t_max_seconds: int) -> float:
        """When this key stops signing, so its last token expires before it stops verifying."""
        return self.not_after - t_max_seconds

    def can_sign(self, t_max_seconds: int, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.signing_cutoff(t_max_seconds)

    def can_verify(self, *, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.not_after

    def public_jwk(self) -> dict[str, str]:
        """The public half, as a JWK.

        Only ever the public half — there is no code path here that can serialise ``d``. That is
        deliberate rather than incidental: publishing the private key to an unauthenticated endpoint
        is the one mistake in this phase that cannot be walked back, and the endpoint would still
        look correct in a browser.
        """
        numbers = self.private.public_key().public_numbers()
        size = (self.private.curve.key_size + 7) // 8
        return {
            "kty": "EC",
            "crv": "P-256",
            "alg": ALGORITHM,
            "use": "sig",
            "kid": self.kid,
            "x": _b64u(numbers.x.to_bytes(size, "big")),
            "y": _b64u(numbers.y.to_bytes(size, "big")),
        }


def _write_private(path: Path, data: bytes) -> None:
    """Atomically install a private file that is owner-only at every observable moment.

    Both properties at once, which is the part worth getting right. Creating the temp with
    ``os.open(..., 0o600)`` and then replacing gives atomicity **and** the mode, because
    ``os.replace`` installs the *source* inode — the 0644 in the classic version of this bug comes
    from creating the temp with a plain ``open()``, which takes the umask.

    Measured, because `identity-auth-traps.mdc` stated the cause imprecisely and the imprecision
    led to the wrong fix being specified: ``open()`` + replace → 0644, ``os.open(…, 0o600)`` +
    replace → 0600. Correcting the mode *after* the write is a correction applied once a
    world-readable inode already exists — a narrower window, not no window.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-key-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class KeySet:
    """The deployment's signing keys: load, bootstrap, rotate, publish.

    Not a singleton by construction — tests build their own against a temp directory — but the
    process uses one, resolved through :func:`key_set`.
    """

    def __init__(self, directory: Path | None = None, *, validity_seconds: int | None = None):
        self.directory = Path(directory) if directory is not None else _configured_dir()
        self.validity_seconds = (
            validity_seconds if validity_seconds is not None else _configured_validity()
        )
        self._lock = threading.RLock()
        self._keys: dict[str, SigningKey] = {}
        self._loaded = False

    # -- persistence -------------------------------------------------------------------------

    def _meta_path(self) -> Path:
        return self.directory / "keys.json"

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._keys = {}
        meta_path = self._meta_path()
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise KeyError_(f"{meta_path} is unreadable: {exc}") from exc
            for entry in meta.get("keys", []):
                kid = str(entry.get("kid") or "")
                pem_path = self.directory / f"{kid}.pem"
                if not kid or not pem_path.is_file():
                    # A metadata row whose key file is gone: skip rather than fail. The tokens it
                    # signed cannot be verified either way, and refusing to start would turn a
                    # partial loss into a total outage.
                    _logger.warning("signing key %r listed in metadata but %s is absent", kid, pem_path)
                    continue
                private = serialization.load_pem_private_key(pem_path.read_bytes(), password=None)
                if not isinstance(private, ec.EllipticCurvePrivateKey):
                    raise KeyError_(f"{pem_path} is not an EC private key")
                loaded = SigningKey(
                    kid=kid,
                    private=private,
                    created_at=float(entry.get("created_at") or 0.0),
                    not_after=float(entry.get("not_after") or 0.0),
                )
                # The kid is derived, so a metadata row claiming a different one is either
                # corruption or an edit. Trusting the file would publish a JWKS whose kid does not
                # match the key, and every external verifier would then fail to find it.
                derived = thumbprint(private.public_key())
                if derived != kid:
                    raise KeyError_(
                        f"{pem_path} has thumbprint {derived!r} but metadata says {kid!r}; "
                        "the key set has been edited or corrupted"
                    )
                self._keys[kid] = loaded
        self._loaded = True

    def _save_locked(self) -> None:
        meta = {
            "version": 1,
            "keys": [
                {"kid": k.kid, "created_at": k.created_at, "not_after": k.not_after}
                for k in sorted(self._keys.values(), key=lambda k: k.created_at)
            ],
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        path = self._meta_path()
        fd, tmp = tempfile.mkstemp(dir=str(self.directory), prefix=".tmp-meta-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(meta, fh, indent=2)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- lifecycle ---------------------------------------------------------------------------

    def _prune_locked(self, now: float) -> bool:
        """Drop keys past ``not_after``. Returns whether anything changed.

        Pruning happens on *write* paths and on explicit maintenance, never inside a read: a read
        that writes changes the file mtime, which is part of any cache key over it, so the cache
        invalidates itself and never hits — measured in Phase 2, where every functional test passed
        while the cache was serving nothing (trap 2).
        """
        dead = [kid for kid, key in self._keys.items() if not key.can_verify(now=now)]
        for kid in dead:
            _logger.info("retiring signing key %s (past not_after)", kid)
            self._keys.pop(kid, None)
            (self.directory / f"{kid}.pem").unlink(missing_ok=True)
        return bool(dead)

    def _mint_locked(self, now: float) -> SigningKey:
        private = ec.generate_private_key(CURVE())
        kid = thumbprint(private.public_key())
        key = SigningKey(
            kid=kid, private=private, created_at=now, not_after=now + self.validity_seconds
        )
        _write_private(self.directory / f"{kid}.pem", _pem(private))
        self._keys[kid] = key
        _logger.info(
            "minted signing key %s (valid %.0f days, signs for %.0f days)",
            kid,
            self.validity_seconds / 86400,
            key.signing_cutoff(_t_max()) - now if key.signing_cutoff(_t_max()) > now else 0,
        )
        return key

    def ensure(self, *, now: float | None = None) -> SigningKey:
        """The active signing key, bootstrapping or rotating as required.

        Idempotent: a restart with a usable key does nothing. This is the only path that mints, so
        "generate on first boot" and "rotate at the cutoff" are one code path rather than two that
        can disagree.
        """
        now = time.time() if now is None else now
        t_max = _t_max()
        self._assert_validity_exceeds(t_max)
        with self._lock:
            self._load_locked()
            changed = self._prune_locked(now)
            signers = [k for k in self._keys.values() if k.can_sign(t_max, now=now)]
            if signers:
                if changed:
                    self._save_locked()
                # Newest, so a rotation takes effect immediately for new tokens.
                return max(signers, key=lambda k: k.created_at)
            key = self._mint_locked(now)
            self._save_locked()
            return key

    def rotate(self, *, now: float | None = None) -> SigningKey:
        """Mint a new active key on demand, keeping the incumbent as a verifier (FR-4f).

        Separate from :meth:`ensure` because the reason to call it is different: this is the
        response to a suspected compromise, and it must work at any moment, not only at the cutoff.
        With an 18-month validity the automatic path would otherwise run once and a half per key,
        and the moment it is needed is the worst moment to find out it does not work.
        """
        now = time.time() if now is None else now
        self._assert_validity_exceeds(_t_max())
        with self._lock:
            self._load_locked()
            self._prune_locked(now)
            key = self._mint_locked(now)
            self._save_locked()
            return key

    def _assert_validity_exceeds(self, t_max: int) -> None:
        """Two checks at different severities, because they are different problems.

        FR-4d **refuses**: a key that cannot outlive its own tokens produces a cutoff in the past
        and can never sign anything, so the deployment cannot mint a session at all.

        The signing-period floor only **warns**: that configuration works correctly — every token
        still verifies for its full life — it just rotates far more often than intended and churns
        the JWKS for every consumer.
        """
        if self.validity_seconds <= t_max + MIN_VALIDITY_MARGIN_SECONDS:
            raise KeyError_(
                f"signing-key validity ({self.validity_seconds}s) must exceed the longest token "
                f"lifetime ({t_max}s) by more than {MIN_VALIDITY_MARGIN_SECONDS}s, or the signing "
                f"cutoff falls in the past and no token can ever be minted. Raise "
                f"RACK_WEB_SIGNING_KEY_VALIDITY_SECONDS or lower the refresh-token ceiling."
            )
        signing_period = self.validity_seconds - t_max
        if signing_period < MIN_INTENDED_SIGNING_PERIOD_SECONDS:
            _logger.warning(
                "signing key will rotate every %.1f months (validity %.1f months minus a %.1f-month "
                "token ceiling), below the intended %.0f-month floor. This is safe — tokens still "
                "verify for their full life — but it republishes the JWKS that often, and consumers "
                "that cache keys will see more rollovers than intended.",
                signing_period / (30 * 86400),
                self.validity_seconds / (30 * 86400),
                t_max / (30 * 86400),
                MIN_INTENDED_SIGNING_PERIOD_SECONDS / (30 * 86400),
            )

    # -- reads -------------------------------------------------------------------------------

    def verifier(self, kid: str, *, now: float | None = None) -> SigningKey | None:
        """The key a ``kid`` names, if it is still a valid verifier. Never mints."""
        now = time.time() if now is None else now
        with self._lock:
            self._load_locked()
            key = self._keys.get(kid)
            return key if key is not None and key.can_verify(now=now) else None

    def public_jwks(self, *, now: float | None = None) -> dict[str, list[dict[str, str]]]:
        """The JWK Set: every currently valid verification key, public halves only."""
        now = time.time() if now is None else now
        with self._lock:
            self._load_locked()
            return {
                "keys": [
                    k.public_jwk()
                    for k in sorted(self._keys.values(), key=lambda k: k.created_at)
                    if k.can_verify(now=now)
                ]
            }


# -- configuration -----------------------------------------------------------------------------


def _configured_dir() -> Path:
    raw = (os.environ.get("RACK_WEB_SIGNING_KEY_DIR") or "").strip()
    return Path(raw) if raw else DEFAULT_KEY_DIR


def _configured_validity() -> int:
    raw = (os.environ.get("RACK_WEB_SIGNING_KEY_VALIDITY_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_VALIDITY_SECONDS
    try:
        value = int(raw)
    except ValueError:
        _logger.warning(
            "RACK_WEB_SIGNING_KEY_VALIDITY_SECONDS=%r is not an integer; using %ds",
            raw,
            DEFAULT_VALIDITY_SECONDS,
        )
        return DEFAULT_VALIDITY_SECONDS
    return value


def _t_max() -> int:
    """The longest token lifetime the deployment permits, read from the live configuration.

    Imported lazily to keep this module importable on its own, and read per call rather than
    captured: a hard-coded value here means raising the refresh ceiling later silently starts
    issuing tokens that outlive their signing key, which is the failure the whole cutoff derivation
    exists to prevent (FR-4d1).
    """
    from identity import tokens

    return tokens.refresh_ttl()


_SET: KeySet | None = None
_SET_LOCK = threading.Lock()


def key_set() -> KeySet:
    """The process's key set."""
    global _SET
    with _SET_LOCK:
        if _SET is None or _SET.directory != _configured_dir():
            _SET = KeySet()
        return _SET


def reset_for_tests() -> None:
    global _SET
    with _SET_LOCK:
        _SET = None
