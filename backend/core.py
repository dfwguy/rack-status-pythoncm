# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Helpers shared by the Flask app and every API module under ``backend/api/``.

Author: Rafael M. Koike & Cursor Agent ("Ada")

**This module must never import Flask, and must never import ``app``.** That is the whole
point of it. ``app.py`` imports the API modules in order to register them, so an API module
that reached back into ``app`` for a helper would form an import cycle. Putting the shared
helpers here means the dependency only ever points one way:

    app.py  ->  api/<domain>.py  ->  core.py

The cycle is not hypothetical: ``workflow_engine.py`` and ``bcm_query.py`` already work around
it today with ``import app`` *inside* function bodies, which defers the problem to call time
rather than solving it. Anything they need should migrate here instead.

Scope: small, dependency-light utilities with no request context and no application state.
Configuration resolution (``_eget`` and the job-env override store) is a larger subsystem with a
file-locked cache and a settings registry, so it has its own module: ``config_env.py``. Read knobs
through that, never with a bare ``os.environ.get`` -- see ``core-designs.mdc`` §5.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import statistics
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import host_exec


def truthy_env(name: str, default: bool = False) -> bool:
    """Read an environment variable as a boolean.

    Accepts the spellings operators actually use in ``rackstatus.toml``, env files and compose files.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    s = raw.strip().lower()
    if not s:
        return default
    return s in ("1", "true", "yes", "on")


def truthy_arg(val: object, *, default: bool = False) -> bool:
    """Query-string / JSON-body boolean. Absent or blank -> *default*."""
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if not s:
        return default
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


def rpath(p: object) -> PurePosixPath:
    """The single path resolver: canonicalise *p* against the execution host.

    Returns a :class:`PurePosixPath` deliberately. The path names a file on the execution host,
    which is not necessarily this machine, so local filesystem calls on it are a category error --
    and an inert path type makes that a loud ``AttributeError`` at the call site instead of a
    silently wrong answer. Ask the connection instead: ``host_exec.is_file`` / ``stat`` /
    ``read_text``.
    """
    return PurePosixPath(host_exec.resolve(p))


#: ``StdOut=`` in ``scontrol show job`` / ``sacct`` output -- where a job's log actually landed.
#:
#: Shared because both the scontrol parser and the recent-jobs view need it, and they now live in
#: different modules.
SLURM_STDOUT_RE = re.compile(r"StdOut=(\S+)")

#: A Slurm job id as ``scancel``/``sacct`` will accept it, optionally an array task.
#:
#: Guards every place a job id reaches a command line. Shared because both the app and the
#: history API validate ids before use, and two copies of a security-relevant pattern is one
#: too many.
SLURM_JOB_ID_RE = re.compile(r"^[1-9][0-9]*(_[0-9]+)?$")

#: Workflow ids whose runs are "deep audits" rather than ordinary workflow runs.
#:
#: Shared because the deep-audit endpoints and the post-run report both filter on it, and they
#: now live in different modules. A second copy would drift the day a third audit is added.
DEEP_AUDIT_WORKFLOWS = frozenset({"hardware-deep-audit", "nvlink-network-deep-audit"})


def history_enabled() -> bool:
    """True when the SQLite run-history store is importable and not disabled by env.

    Lives here rather than with the history API because ``app.py`` gates six separate code paths
    on it -- an availability predicate is configuration, not an endpoint.
    """
    try:
        import results_history
    except ImportError:
        return False
    return results_history is not None and not truthy_env("RACK_RESULTS_HISTORY_DISABLED")


def utc_now_iso() -> str:
    """Now, as ``2026-08-18T15:04:05.123456+00:00``.

    One of TWO timestamp conventions in this codebase, and this is the one most modules use --
    ``datetime.now(timezone.utc).isoformat()``, with microseconds and a numeric offset. Four modules
    each carried a byte-identical copy of it (node_issues, post_run_report, rack_locks,
    validation_activities).

    The other convention is ``strftime("%Y-%m-%dT%H:%M:%SZ")``: second precision, literal Z. It is
    NOT interchangeable -- ``node_report`` documents that the customer schema's ``isoDateTime``
    pattern requires the Z form -- so those callers are deliberately left alone rather than folded
    in here. Anything parsing both formats has to accept both.
    """
    return datetime.now(timezone.utc).isoformat()


def utc_iso_from_epoch(timestamp: float | None) -> str | None:
    """Epoch seconds -> the same ``+00:00`` ISO form; ``None`` passes through."""
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


#: A browser-supplied dashboard client id, as sent in ``X-Rack-Client-Id``.
#:
#: Shared because two modules validate it -- ``api/_common.py`` when reading the header and
#: ``app_clients.py`` before recording a heartbeat -- and a client id that one accepts and the other
#: rejects would silently drop that browser out of the connected-user count.
APP_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,80}$")


def slurm_job_base_id(job_raw: str | None) -> str | None:
    """The allocation id from a Slurm job id, dropping any array-task suffix.

    ``None`` for anything that is not a plain number -- sacct prints "-" for rows that have no
    job id, and a non-numeric id must never reach a command line.
    """
    s = (job_raw or "").strip()
    if not s or s == "-":
        return None
    if "_" in s:
        s = s.split("_", 1)[0]
    return s if s.isdigit() else None


#: One DNS label: alphanumeric at both ends, hyphens allowed inside, 63 characters max.
_HOSTNAME_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"

#: A plain RFC 1123 hostname (or bare IPv4 literal, which is a subset of it).
#:
#: Deliberately narrow: no ``@``, no ``:``, no ``/``, no whitespace, no shell metacharacter, and
#: no leading hyphen. Shared because both the web terminal and node-target construction need the
#: same answer, and a name one accepts while the other rejects is how a validated value reaches a
#: command line by another route.
NODE_HOSTNAME_RE = re.compile(rf"^{_HOSTNAME_LABEL}(?:\.{_HOSTNAME_LABEL})*$")


def is_valid_node_hostname(name: str | None) -> bool:
    """Whether *name* is safe to place as a bare argv element in an ssh command line.

    ``host_exec.ssh_to_node_argv`` appends the node after ``ssh -tt <opts> user@head``, and
    OpenSSH joins that trailing argv with spaces for the *remote* shell to interpret. So a ``;``
    or a backtick in a node name is remote command injection on the head node, and a leading
    ``-`` is an ssh option (``-oProxyCommand=...``) on the inner hop rather than a host at all.
    Neither shape can match :data:`NODE_HOSTNAME_RE`.

    Same reasoning as :func:`slurm_job_base_id`: a value that cannot be trusted on a command
    line must be rejected at the edge, not escaped at the call site.
    """
    s = (name or "").strip()
    if not s or len(s) > 253:
        return False
    return bool(NODE_HOSTNAME_RE.match(s))


# --- Small generic utilities ------------------------------------------------------------------
# No domain meaning, no application state. utc_iso_z is the SECOND timestamp convention described
# in utc_now_iso above: second precision with a literal Z, which the customer report schema's
# isoDateTime pattern requires. Both exist on purpose; pick by what the consumer parses.


def median_float(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def utc_iso_z(ts: float) -> str:
    """RFC3339 UTC with Z suffix for Grafana ``from`` / ``to`` URL params."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    s = dt.isoformat(timespec="milliseconds")
    if s.endswith("+00:00"):
        return s[:-6] + "Z"
    return s


def last_regex_int(pat: re.Pattern[str], text: str) -> int | None:
    last: int | None = None
    for m in pat.finditer(text):
        try:
            last = int(m.group(1))
        except (ValueError, IndexError):
            continue
    return last


def mean_finite(vals: list[float | None]) -> float | None:
    xs = [float(v) for v in vals if v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not xs:
        return None
    return float(statistics.mean(xs))


# ---------------------------------------------------------------------------
# Client address provenance (FR-8, NFR-14)
# ---------------------------------------------------------------------------

#: How ``src_ip`` was arrived at. The label is the point: an audit field that looks precise and is
#: not is worse than one that admits its limit.
TRUST_PEER = "peer"                      # no forwarded header; the TCP peer is the whole truth
TRUST_PROXY = "proxy"                    # resolved through a *configured* trusted proxy
TRUST_CLAIMED_UNTRUSTED = "claimed-untrusted"  # a header was offered and deliberately not believed

#: Narrowest prefix a trusted-proxy entry may declare. A proxy list is a statement about a handful
#: of known hosts, so anything broader is a configuration mistake rather than a wide deployment --
#: and ``0.0.0.0/0`` would make every forged ``X-Forwarded-For`` authoritative *and* relabel it
#: ``proxy``, which is worse than not labelling it at all (threat T-54).
MIN_TRUSTED_PREFIX_V4 = 24
MIN_TRUSTED_PREFIX_V6 = 64


def parse_trusted_proxies(raw: str | None) -> tuple[tuple[Any, ...], tuple[str, ...]]:
    """Parse a comma-separated proxy list into networks, plus refusals to report (NFR-14).

    Returns ``(accepted, refusals)``. Refusing loudly and continuing beats raising: an unusable
    proxy entry must not stop the app serving, but it must not silently take effect either -- so
    the entry is dropped and the reason is returned for the caller to log once at startup.
    """
    accepted: list[Any] = []
    refusals: list[str] = []
    for part in (raw or "").split(","):
        entry = part.strip()
        if not entry:
            continue
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            refusals.append(f"{entry!r} is not an IP address or CIDR network")
            continue
        floor = MIN_TRUSTED_PREFIX_V4 if net.version == 4 else MIN_TRUSTED_PREFIX_V6
        if net.prefixlen < floor:
            refusals.append(
                f"{entry!r} is broader than /{floor}: a trusted-proxy entry names a few known "
                f"hosts, and a wide one would make any forwarded header authoritative"
            )
            continue
        accepted.append(net)
    return tuple(accepted), tuple(refusals)


def _is_trusted(addr: str, trusted: tuple[Any, ...]) -> bool:
    try:
        ip = ipaddress.ip_address(addr.strip())
    except ValueError:
        return False
    return any(ip in net for net in trusted)


def resolve_client_addr(
    peer: str | None,
    forwarded_for: str | None,
    trusted: tuple[Any, ...] = (),
) -> tuple[str, str]:
    """Resolve ``(src_ip, src_ip_trust)`` from the TCP peer and any forwarded chain (FR-8).

    The rule that matters: **a forwarded header is believed only when the peer is a configured
    trusted proxy.** The leftmost entry of ``X-Forwarded-For`` is whatever the client typed, so
    trusting it unconditionally means every operator can claim any source address -- and the
    resulting audit record is worse than useless because it looks authoritative.

    When the peer *is* trusted, the chain is walked right-to-left past further trusted hops, since
    each proxy appends the address it saw. The first untrusted entry from the right is the real
    client; everything left of it is unverifiable client-supplied text.
    """
    peer_s = (peer or "").strip()
    chain_raw = (forwarded_for or "").strip()
    if not chain_raw:
        return peer_s, TRUST_PEER
    if not peer_s or not _is_trusted(peer_s, trusted):
        # A header was offered by someone we have no reason to believe. Record the peer and say so.
        return peer_s, TRUST_CLAIMED_UNTRUSTED

    hops = [h.strip() for h in chain_raw.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop, trusted):
            return hop, TRUST_PROXY
    # Every hop is itself a trusted proxy: the leftmost is the closest thing to a client we have.
    return (hops[0] if hops else peer_s), TRUST_PROXY
