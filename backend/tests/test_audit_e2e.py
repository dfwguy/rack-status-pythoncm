# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""TC-31 of the Phase 1 test plan: the whole feature end to end, under default configuration.

Traces to NFR-8, FR-10, FR-10b, AC-14, AC-14d, AC-26.

**This case exists because every other one is scoped too narrowly to answer NFR-8.** The rest of the
suite verifies pieces: that the schema is right, that rotation is bounded, that a refusal carries a
decision. NFR-8 makes a claim about the *product as shipped* — that a deployment which enables no
hardening option, configures no syslog, and turns on no encryption still produces a readable audit
trail. Nothing that runs in-process through the Flask test client can establish that, because the
test client shares this interpreter's logging configuration; it is the one thing a real deployment
does not.

So a real server is started as a subprocess with **token-mode authentication actually on**, and three
requests are driven over a real socket:

1. a permitted mutating request, carrying a session established through `/api/login`;
2. the same request with no credential, which the gate refuses;
3. an unauthenticated `/ws/terminal` upgrade.

Rows 2 and 3 are the point. A trail that records only successes is an activity log, not an audit
trail — the entries with evidentiary value are the refusals, and `/ws/terminal` is answered by WSGI
middleware **above Flask's router**, so it is the one surface no `after_request` hook can reach. If
the terminal refusal is missing from the trail, the product's single highest-privilege endpoint is
the one whose denials go unrecorded.

Gunicorn is the server here, not Werkzeug, because it is what the Dockerfile's ENTRYPOINT runs and
because the terminal middleware only claims a path when the environ carries a real socket
(`gunicorn.socket` / `werkzeug.socket`). TC-35 owns the gunicorn-versus-dev-server question; this
case just needs a server that behaves like production while the audit configuration stays default.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The same audited endpoint the rest of the phase's cases use, so a break here is a break there.
CANONICAL_ENDPOINT = "/api/rack-uptime"
CANONICAL_ACTION = "rack:uptime"

#: Shared secret for token-mode auth. A literal in a test fixture, never a real credential.
#: The bootstrap administrator, re-enrolled with a password this test knows. The child server
#: mints its own random one and keeps only the verifier, so the only way to drive a real login
#: is to write a known credential into the store before the server starts.
ADMIN_LOGIN = "admin"
ADMIN_PASSWORD = "tc31-e2e-admin-password"

STARTUP_TIMEOUT_SEC = 45.0
EVENT_TIMEOUT_SEC = 20.0

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / "gunicorn.conf.py").is_file(),
    reason="gunicorn.conf.py absent; TC-31 drives the server the ENTRYPOINT runs",
)


def _seed_admin(identity_file: Path, policy_file: Path) -> None:
    """Create the administrator with a password this test knows, and the policy that grants them.

    The policy half arrived when first boot began writing one. Without it the child authenticates
    and is then refused everything by default-deny, so the trail fills with 403s and the NFR-8
    claim this case exists to prove — that a deployment enabling nothing still produces a readable
    trail — fails for a reason that has nothing to do with auditing.

    Written here rather than left to the child's own bootstrap because the store is pre-seeded with
    a known credential, so `ensure_admin` correctly finds an administrator already present and the
    seeding beside it does not fire.
    """
    import json as _json

    from identity import bootstrap
    from identity import srp as srp_mod
    from identity.store import IdentityStore

    identity_file.parent.mkdir(parents=True, exist_ok=True)
    store = IdentityStore(str(identity_file))
    try:
        bootstrap.ensure_admin(store, emit=False)
        user = store.get_user(ADMIN_LOGIN)
        salt, verifier = srp_mod.enroll(ADMIN_LOGIN, ADMIN_PASSWORD)
        # `must_change_password=False` matters here, and leaving it True was a real defect once the
        # bootstrap began re-issuing an *unclaimed* credential (2026-09-08): the child server booted,
        # found an administrator nobody had claimed, issued a fresh password, and the known one this
        # test logs in with stopped working — a 401 on the permitted request. Choosing a password IS
        # claiming the account, so saying so is also the honest record.
        store.update_user(
            user.edited(srp_salt=salt, srp_verifier=verifier, must_change_password=False)
        )
        store.attach_policy(f"user:{ADMIN_LOGIN}", bootstrap.ADMIN_POLICY_ID)
    finally:
        store.close()

    # The child gets its own policy set, so this case stays a self-contained deployment rather than
    # borrowing the session's. The document is the product's own, not a hand-written stand-in.
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        _json.dumps({"version": 1, "policies": [bootstrap.admin_policy_document()]}) + "\n"
    )


def _set_cookies(headers: list[tuple[str, str]]) -> list[str]:
    """Every Set-Cookie value. The login sets two and both must be carried."""
    return [v for k, v in headers if k.lower() == "set-cookie"]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _LiveServer:
    """A gunicorn child under default audit configuration, plus the audit file it writes."""

    def __init__(self, port: int, audit_file: Path, proc: subprocess.Popen, tls_cert: Path):
        self.port = port
        self.audit_file = audit_file
        self.proc = proc
        self.session_cookie: str | None = None
        # The server is HTTPS-only; verify against the certificate it generated, as a browser
        # through the tunnel would (its SAN carries 127.0.0.1).
        self.tls = ssl.create_default_context(cafile=str(tls_cert))

    # -- transport ------------------------------------------------------------------------------
    def _request(
        self, method: str, path: str, body: dict | None = None, *, with_session: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=20, context=self.tls)
        headers = {"Content-Type": "application/json"}
        if with_session and self.session_cookie:
            headers["Cookie"] = self.session_cookie
        if extra_headers:
            headers.update(extra_headers)
        payload = json.dumps(body).encode() if body is not None else None
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            # The RAW header list, not a dict: a dict keeps one Set-Cookie and the login
            # exchange sets two, so the access cookie would be silently dropped.
            return int(resp.status), list(resp.getheaders()), data
        finally:
            conn.close()

    def login(self) -> int:
        """Authenticate through the real SRP exchange, so the permitted request is really permitted.

        This drove `/api/login` with the shared token until FR-18 removed that path. It is now the
        two-leg SRP exchange against the bootstrap administrator, which is worth more than a fixture
        convenience: the end-to-end case now exercises the credential a deployment actually uses.
        """
        import srp as srp_lib
        from identity import srp as srp_mod

        srp_lib.rfc5054_enable(srp_mod.RFC5054_COMPAT)
        user = srp_lib.User(
            ADMIN_LOGIN, ADMIN_PASSWORD, hash_alg=srp_mod.HASH_ALG, ng_type=srp_mod.NG_TYPE
        )
        _uname, a_value = user.start_authentication()

        status, _headers, body = self._request(
            "POST", "/api/auth/srp/begin", {"login": ADMIN_LOGIN, "A": a_value.hex()}
        )
        if status != 200:
            return status
        begun = json.loads(body)
        proof = user.process_challenge(
            bytes.fromhex(begun["salt"]), bytes.fromhex(begun["B"])
        )
        assert proof is not None, "the client refused the server's challenge"

        status, headers, _ = self._request(
            "POST", "/api/auth/srp/finish", {"token": begun["token"], "M": proof.hex()}
        )
        # Carry every cookie the exchange set; the access cookie is what the gate reads.
        jar = [c.split(";", 1)[0] for c in _set_cookies(headers)]
        if jar:
            self.session_cookie = "; ".join(jar)
        return status

    def upgrade_terminal(self) -> int:
        """Attempt a WebSocket upgrade on /ws/terminal with no credential.

        The middleware only claims the path when `Upgrade: websocket` is present AND the environ
        carries a real socket, so the headers matter: without them this falls through to Flask's
        router and exercises a different refusal entirely.
        """
        status, _, _ = self._request(
            "GET", "/ws/terminal?mode=headnode",
            extra_headers={
                "Upgrade": "websocket",
                "Connection": "Upgrade",
                "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
            },
        )
        return status

    # -- observation ----------------------------------------------------------------------------
    def audit_events(self) -> list[dict]:
        if not self.audit_file.is_file():
            return []
        out = []
        for line in self.audit_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                # A line that does not parse is itself a finding; surface it rather than hide it.
                pytest.fail(f"unparseable line in the audit trail: {line[:200]!r}")
            if isinstance(parsed, dict):
                out.append(parsed)
        return out

    def wait_for(self, predicate, timeout: float = EVENT_TIMEOUT_SEC) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.2)
        return False

    def find(self, **match) -> dict | None:
        for event in self.audit_events():
            if all(event.get(k) == v for k, v in match.items()):
                return event
        return None


@pytest.fixture
def live_server(tmp_path):
    """A real server with authentication ON and every audit hardening option left at its default."""
    port = _free_port()
    audit_file = tmp_path / "audit" / "audit.jsonl"
    identity_file = tmp_path / "identity" / "identity.json"
    policy_file = tmp_path / "store" / "policies.json"

    # Enrol a known credential BEFORE the child starts. The child's own bootstrap would mint a
    # random password and keep only the verifier, so there would be no way to log in; doing it here
    # means the test drives the same SRP exchange a real operator does.
    _seed_admin(identity_file, policy_file)

    env = dict(os.environ)
    env.update(
        {
            "RACK_STATUS_DEMO": "1",
            # The subject of this case: the gate is ON. Patching cannot cross a process boundary,
            # so the environment is the only way to configure the child. There is no longer a
            # credential *variable* to set — FR-18 removed them — so the credential is written into
            # the identity store below instead, which is the real deployment shape.
            "RACK_WEB_SECRET_KEY": "tc31-token-signing-key",
            "NVIS_IDENTITY_FILE": str(identity_file),
            "RACK_WEB_POLICY_FILE": str(policy_file),
            "RACK_EXEC_MODE": "local",
            # Only the audit *path* is overridden, so the test can read it. Sinks, format, level,
            # rotation, retention and the flood guard are all left at their defaults -- that is
            # what makes this NFR-8 evidence rather than a configured happy path.
            "NVIS_AUDIT_FILE": str(audit_file),
            "RACK_UPTIME_DRY_RUN": "1",
            "NVIS_CATALOG_FILE": str(tmp_path / "catalog.json"),
            # Where the server generates its self-signed pair, and a config dir with no tls/ in
            # it -- so the developer's own config/ cannot change which certificate is served.
            "RACK_WEB_TLS_DIR": str(tmp_path / "tls"),
            "NVIS_RACKSTATUS_CONFIG": str(tmp_path / "config" / "rackstatus.toml"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    # Explicitly clear anything the developer's own shell may have set, or "default configuration"
    # would mean "whatever this laptop has".
    for leaked in (
        "NVIS_AUDIT_ENABLED", "NVIS_AUDIT_LONG_RETENTION", "NVIS_LOG_SINKS", "NVIS_LOG_FORMAT",
        "NVIS_LOG_LEVEL", "NVIS_LOG_SYSLOG_HOST", "NVIS_LOG_FILE",
        "RACK_WEB_TLS_CERT", "RACK_WEB_TLS_KEY",
        "RACK_WEB_ALLOWED_ORIGINS", "RACK_WEB_TRUSTED_PROXIES",
    ):
        env.pop(leaked, None)

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "gunicorn",
            "-c", str(REPO_ROOT / "gunicorn.conf.py"),
            "--workers", "1", "--threads", "2", "--worker-class", "gthread",
            "--pythonpath", str(REPO_ROOT / "backend"),
            "--bind", f"127.0.0.1:{port}",
            "app:app",
        ],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"server exited during startup ({proc.returncode}):\n{out[-3000:]}")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    ready = True
                    break
            time.sleep(0.2)
        if not ready:
            pytest.fail(f"server never listened on {port} within {STARTUP_TIMEOUT_SEC}s")
        yield _LiveServer(port, audit_file, proc, tmp_path / "tls" / "server.crt")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_default_deployment_produces_a_readable_trail(live_server):
    """NFR-8 / AC-26: a permitted action lands in the trail with nothing switched on.

    The assertion is deliberately on the FR-5 field set and not merely on the action string: an
    event that arrives without a principal, a result or a request id is not usable as audit
    evidence, and "a line appeared" would pass while the line was useless.
    """
    assert live_server.login() == 200, "token-mode login must succeed for the permitted request"

    status, _, _ = live_server._request(
        "POST", CANONICAL_ENDPOINT, {"rack": "p5-r01"}, with_session=True
    )
    assert status != 401, "the session cookie should have authenticated this request"

    assert live_server.wait_for(
        lambda: live_server.find(action=CANONICAL_ACTION, result="success") is not None
    ), (
        "no successful audited event reached the trail under default configuration. NFR-8 claims a "
        "deployment that enables nothing still produces a readable trail; this is that claim.\n"
        f"trail so far: {json.dumps(live_server.audit_events()[-5:], indent=1)}"
    )

    event = live_server.find(action=CANONICAL_ACTION, result="success")
    assert event is not None
    for field in ("event_id", "ts", "principal", "action", "result", "request_id", "src_ip"):
        assert field in event, f"{field} missing — the event is not usable as audit evidence"
    assert event["principal"], "an authenticated request must not record an empty principal"


def test_refusals_reach_the_trail_on_a_live_server(live_server):
    """AC-14 / AC-14d / FR-10b: both refusal paths are recorded, each with its reason.

    Two refusals, chosen because they are answered in different places. The first is refused by the
    Flask gate and so passes through the `after_request` chokepoint. The second is refused by WSGI
    middleware *above* the router, where no Flask hook runs at all — a separate, deliberate emit
    call site, and the one that would silently disappear if that call were ever dropped.
    """
    # (b) no credential at all -> the gate refuses.
    status, headers, _ = live_server._request("POST", CANONICAL_ENDPOINT, {"rack": "p5-r01"})
    assert status == 401, f"expected the gate to refuse an uncredentialed request, got {status}"
    assert any(k.lower() == "www-authenticate" for k, _v in headers), (
        "RFC 9110 requires a challenge on a 401"
    )

    assert live_server.wait_for(
        lambda: any(
            e.get("result") in ("refused", "denied") and e.get("action") == CANONICAL_ACTION
            for e in live_server.audit_events()
        )
    ), (
        "the auth refusal did not reach the trail. A trail holding only successes is an activity "
        f"log.\ntrail: {json.dumps(live_server.audit_events()[-5:], indent=1)}"
    )
    refusal = next(
        e for e in live_server.audit_events()
        if e.get("result") in ("refused", "denied") and e.get("action") == CANONICAL_ACTION
    )
    assert refusal.get("decision"), "AC-14d: a refusal must record the decision that produced it"

    # (c) an unauthenticated terminal upgrade -> refused above Flask's router.
    assert live_server.upgrade_terminal() == 403, (
        "an unauthenticated /ws/terminal upgrade must be refused with a plain 403 and never "
        "reach a socket"
    )

    assert live_server.wait_for(
        lambda: live_server.find(action="terminal:open", result="refused") is not None
    ), (
        "the terminal refusal did not reach the trail. This surface is answered above Flask's "
        "router, so no after_request hook can record it -- it is the product's highest-privilege "
        f"endpoint.\ntrail: {json.dumps(live_server.audit_events()[-5:], indent=1)}"
    )

    terminal = live_server.find(action="terminal:open", result="refused")
    assert terminal is not None
    assert terminal.get("reason") == "unauthenticated", (
        f"the refusal must say why; got {terminal.get('reason')!r}"
    )
    assert terminal.get("principal") == "anonymous"
    assert terminal.get("resource") == "/ws/terminal"


def test_the_trail_is_one_object_per_line(live_server):
    """The readability half of NFR-8: "readable" means machine-readable, line by line.

    `audit_events()` fails the test on any unparseable line, so this drives traffic and then simply
    requires that the file be non-empty and every line have parsed. Without it, a trail of pretty-
    printed multi-line JSON would satisfy the two cases above and be unreadable by any collector.
    """
    live_server.login()
    live_server._request("POST", CANONICAL_ENDPOINT, {"rack": "p5-r01"}, with_session=True)
    assert live_server.wait_for(lambda: bool(live_server.audit_events()))

    raw = live_server.audit_file.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert lines, "the trail is empty"
    for line in lines:
        parsed = json.loads(line)  # audit_events() already asserted this; be explicit
        assert isinstance(parsed, dict), "each line must be one JSON object"
