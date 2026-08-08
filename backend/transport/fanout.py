"""Fan-out: the scale mechanism over the Connection lego.

Runs one command across N nodes by instantiating N *chained* connections
(``base.hop(node)``) and executing them concurrently — the same model as pdsh
(independent parallel sessions), but every session is a first-class
:class:`~transport.base.Connection`, so ssh/local/sudo/env-forwarding all compose
in from the base. Output is coalesced dshbak-style (nodes grouped by identical
stdout; silent nodes omitted so "expect no output" checks stay clean).

The base connection is the shared control hop (e.g. app->head); each worker adds
the node leg on top. (A future optimization can enable SSH ControlMaster on the
base so the N node legs reuse a single app->head master; the model here is
unchanged.)
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from transport.base import Connection

_logger = logging.getLogger(__name__)

# Transient SSH handshake/relay failures worth retrying (vs a real command failure).
# These come from the shared gateway's sshd throttling concurrent sessions
# (MaxStartups / MaxSessions): "kex_exchange_identification", connection resets, etc.
_TRANSIENT = (
    "kex_exchange_identification",
    "Connection reset by",
    "Connection closed by",
    "Session open refused",
    "session request failed",
    "Permission denied, please try again",
    "onnection timed out",
    "Too many authentication failures",
)


@dataclass
class NodeResult:
    node: str
    stdout: str
    stderr: str
    returncode: int


def fanout(
    base: Connection,
    nodes: list[str],
    cmd: str | list[str],
    *,
    timeout: int = 120,
    max_workers: int = 8,
    user: str | None = None,
    key: str = "",
    extra_opts: list[str] | None = None,
    connect_timeout: str = "15",
    sudo: bool = False,
    env_extra: dict[str, str] | None = None,
    retries: int = 2,
) -> list[NodeResult]:
    """Run *cmd* on each node via ``base.hop(node)`` concurrently; return per-node results.

    ``max_workers`` defaults to 8 to stay under a typical gateway sshd's MaxStartups/
    MaxSessions; combined with the base connection's ControlMaster (multiplexing),
    the concurrent hops reuse a single app->head connection. Transient handshake
    failures are retried (``retries``) with a short backoff.
    """
    nodes = [n for n in nodes if n]
    if not nodes:
        return []

    # Warm the shared base (app->head) connection once so its SSH ControlMaster is
    # established before the concurrent hops attach — otherwise N simultaneous
    # first-connections race to create the master and can trip sshd MaxStartups.
    if len(nodes) > 1:
        try:
            base.run("true", timeout=max(10, int(str(connect_timeout)) if str(connect_timeout).isdigit() else 15))
        except Exception:
            _logger.debug("fanout: ignoring error", exc_info=True)

    def _attempt(node: str) -> NodeResult:
        conn = base.hop(node, user=user, key=key, extra_opts=extra_opts,
                        connect_timeout=connect_timeout, sudo=sudo)
        try:
            p = conn.run(cmd, timeout=timeout, env_extra=env_extra)
            return NodeResult(node, p.stdout or "", p.stderr or "", p.returncode)
        except Exception as exc:  # timeout / transport error -> stderr, non-zero
            _logger.debug("_attempt: ignoring error", exc_info=True)
            return NodeResult(node, "", f"{type(exc).__name__}: {exc}", 255)

    def _one(node: str) -> NodeResult:
        r = _attempt(node)
        for i in range(max(0, retries)):
            if r.returncode == 0 or not any(t in r.stderr for t in _TRANSIENT):
                break
            time.sleep(0.4 * (i + 1))  # brief backoff so the gateway can drain
            r = _attempt(node)
        return r

    results: list[NodeResult] = []
    workers = max(1, min(max_workers, len(nodes)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_one, n): n for n in nodes}
        for fut in as_completed(futs):
            results.append(fut.result())
    # stable order by node name for deterministic output
    results.sort(key=lambda r: r.node)
    return results


def group_outputs(results: list[NodeResult]) -> str:
    """Coalesce results dshbak -c style: group nodes by identical stdout.

    Silent nodes (empty stdout) are omitted so "expect no output" gating stays
    empty when everything is clean, matching ``pdsh | dshbak -c``.
    """
    groups: dict[str, list[str]] = {}
    for r in results:
        if r.stdout.strip():
            groups.setdefault(r.stdout.rstrip("\n"), []).append(r.node)
    blocks: list[str] = []
    for out, nodes in groups.items():
        blocks.append("----------------")
        blocks.append(",".join(sorted(nodes)))
        blocks.append("----------------")
        blocks.append(out)
    return "\n".join(blocks)


def fanout_errors(results: list[NodeResult]) -> str:
    """Per-node stderr / connection errors (kept separate from grouped stdout)."""
    lines: list[str] = []
    for r in results:
        if r.stderr.strip():
            for ln in r.stderr.rstrip("\n").splitlines():
                lines.append(f"{r.node}: {ln}")
    return "\n".join(lines)
