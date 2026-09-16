#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Long-lived, serialized PythonCM query broker for Rack Status.

PythonCM's entity and parallel-operation APIs are fast after ``Cluster`` is
connected, but constructing a fresh client costs roughly ten CPU-seconds on the
Nscale 16K controller.  This process owns exactly one connection and accepts
newline-delimited JSON requests over a host-local Unix socket.  The synchronous
server is intentional: PythonCM calls are serialized through one connection.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nvis_bcm_query as query_helper
import nvis_pythoncm_exec as exec_helper

_LOG = logging.getLogger("nvis-pythoncm-broker")
_MUTATING_QUERIES = frozenset({"power", "drain", "undrain"})
_MAX_REQUEST_BYTES = 1_000_000


class BrokerFatalError(RuntimeError):
    """Raised only when an injected fatal-exit callback returns during tests."""


class PersistentCluster:
    """Own one PythonCM ``Cluster`` and reconnect only after a real failure."""

    def __init__(
        self,
        base_args: argparse.Namespace,
        module_path: str,
        *,
        disconnect_timeout: float = 5.0,
        fatal_exit: Callable[[int], None] = os._exit,
    ) -> None:
        self.base_args = base_args
        self.module_path = module_path
        self.cluster: Any | None = None
        self.generation = 0
        self.connected_at = 0.0
        self.last_activity = 0.0
        self.disconnect_timeout = max(0.01, disconnect_timeout)
        self.fatal_exit = fatal_exit

    def connect(self) -> Any:
        if self.cluster is not None:
            return self.cluster
        started = time.monotonic()
        self.cluster = query_helper._connect(self.base_args)
        self.generation += 1
        self.connected_at = time.time()
        self.last_activity = time.monotonic()
        _LOG.info(
            "connected to CMDaemon generation=%d in %.3fs",
            self.generation,
            time.monotonic() - started,
        )
        return self.cluster

    def close(self) -> None:
        cluster, self.cluster = self.cluster, None
        if cluster is None:
            return
        failure: list[BaseException] = []

        def disconnect() -> None:
            try:
                cluster.disconnect()
            except BaseException as exc:  # noqa: BLE001 - propagate from helper thread
                failure.append(exc)

        worker = threading.Thread(
            target=disconnect,
            name="pythoncm-disconnect",
            daemon=True,
        )
        worker.start()
        worker.join(self.disconnect_timeout)
        if worker.is_alive():
            _LOG.critical(
                "PythonCM disconnect exceeded %.1fs; terminating wedged broker for systemd restart",
                self.disconnect_timeout,
            )
            self.fatal_exit(75)
            raise BrokerFatalError("fatal exit callback returned after disconnect timeout")
        if failure:
            _LOG.warning(
                "PythonCM disconnect failed: %s: %s",
                type(failure[0]).__name__,
                failure[0],
            )

    def reconnect(self) -> Any:
        self.close()
        return self.connect()

    def execute(self, argv: list[str]) -> dict[str, Any]:
        args = query_helper.build_parser().parse_args(argv)
        if args.bootstrap_ca:
            return {
                "ok": False,
                "error": "unsupported broker request",
                "detail": "CA bootstrap is a one-shot administrative command",
            }
        if args.format != "json":
            return {
                "ok": False,
                "error": "unsupported broker format",
                "detail": "persistent broker responses are JSON",
            }

        cluster = self.connect()
        auth_mode = str(getattr(cluster, "_nvis_auth_mode", "certificate"))
        if args.query in query_helper._AUTHENTICATED_QUERIES and auth_mode == "certificate":
            return {
                "ok": False,
                "error": "authentication failed",
                "detail": f"{args.query} requires a BCM login cookie",
            }

        for attempt in (1, 2):
            started_wall = time.time()
            started_mono = time.monotonic()
            try:
                payload = query_helper._run_query(cluster, args, self.module_path, started_wall)
                payload["transport"] = "persistent-pythoncm"
                payload["connection_generation"] = self.generation
                payload["broker_query_seconds"] = round(time.monotonic() - started_mono, 4)
                self.last_activity = time.monotonic()
                return payload
            except Exception as exc:  # noqa: BLE001 - structured response, optional read retry
                _LOG.warning(
                    "query=%s generation=%d attempt=%d failed: %s: %s",
                    args.query,
                    self.generation,
                    attempt,
                    type(exc).__name__,
                    exc,
                )
                # A mutating request may have reached CMDaemon. Never retry it implicitly.
                if args.query in _MUTATING_QUERIES or attempt == 2:
                    return {
                        "ok": False,
                        "error": "persistent PythonCM query failed",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "transport": "persistent-pythoncm",
                        "connection_generation": self.generation,
                    }
                try:
                    cluster = self.reconnect()
                except Exception as reconnect_exc:  # noqa: BLE001
                    return {
                        "ok": False,
                        "error": "persistent PythonCM reconnect failed",
                        "detail": f"{type(reconnect_exc).__name__}: {reconnect_exc}",
                        "transport": "persistent-pythoncm",
                        "connection_generation": self.generation,
                    }
        raise AssertionError("unreachable")

    def execute_programrunner(self, argv: list[str]) -> dict[str, Any]:
        """Run submit/status/result/cancel through this same connected cluster."""
        args = exec_helper.build_parser().parse_args(argv)
        started = time.monotonic()
        payload = exec_helper.execute(args, cluster=self.connect())
        payload["transport"] = "persistent-pythoncm"
        payload["connection_generation"] = self.generation
        payload["broker_query_seconds"] = round(time.monotonic() - started, 4)
        self.last_activity = time.monotonic()
        return payload

    def keepalive(self, interval: float) -> None:
        if self.cluster is None or time.monotonic() - self.last_activity < interval:
            return
        try:
            self.cluster.settings.get_version()
            self.last_activity = time.monotonic()
        except Exception:  # noqa: BLE001 - next reconnect is logged and retried forever by systemd
            _LOG.warning("PythonCM keepalive failed; reconnecting", exc_info=True)
            self.reconnect()


class BrokerHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        started = time.monotonic()
        service = "invalid"
        query = "unknown"
        raw = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        if len(raw) > _MAX_REQUEST_BYTES:
            response = {"ok": False, "error": "broker request too large"}
        else:
            try:
                request = json.loads(raw)
                service = request.get("service", "query") if isinstance(request, dict) else None
                argv = request.get("argv") if isinstance(request, dict) else None
                if service not in {"query", "exec"}:
                    raise ValueError("service must be query or exec")
                if service not in self.server.allowed_services:  # type: ignore[attr-defined]
                    raise ValueError(f"service {service} is not enabled on this broker")
                if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
                    raise ValueError("request must contain a string argv list")
                if service == "query" and "--query" in argv:
                    index = argv.index("--query") + 1
                    query = argv[index] if index < len(argv) else "missing"
                elif service == "exec" and argv:
                    query = argv[0]
                if service == "exec":
                    response = self.server.manager.execute_programrunner(argv)  # type: ignore[attr-defined]
                else:
                    response = self.server.manager.execute(argv)  # type: ignore[attr-defined]
            except SystemExit as exc:
                response = {"ok": False, "error": "invalid broker arguments", "detail": str(exc)}
            except Exception as exc:  # noqa: BLE001 - broker must always answer JSON
                response = {
                    "ok": False,
                    "error": "invalid broker request",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
        elapsed = time.monotonic() - started
        _LOG.info(
            "request service=%s operation=%s ok=%s duration=%.3fs",
            service,
            query,
            response.get("ok"),
            elapsed,
        )
        self.wfile.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")


class BrokerServer(socketserver.UnixStreamServer):
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(
        self,
        socket_path: str,
        manager: PersistentCluster,
        keepalive: float,
        *,
        allowed_services: frozenset[str],
        backlog: int,
    ) -> None:
        self.manager = manager
        self.keepalive_interval = keepalive
        self.allowed_services = allowed_services
        self.request_queue_size = max(8, backlog)
        super().__init__(socket_path, BrokerHandler)

    def service_actions(self) -> None:
        self.manager.keepalive(self.keepalive_interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--keepalive-seconds", type=float, default=60.0)
    parser.add_argument(
        "--services",
        choices=("query", "exec", "query,exec"),
        default="query,exec",
        help="request classes accepted by this broker",
    )
    parser.add_argument("--backlog", type=int, default=128)
    parser.add_argument("--disconnect-timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("NVIS_PYTHONCM_BROKER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    query_args = query_helper.build_parser().parse_args(["--query", "version"])
    candidates = query_args.module_path or list(query_helper.DEFAULT_MODULE_PATHS)
    if os.environ.get("NVIS_PYTHONCM_PATH"):
        candidates.insert(0, os.environ["NVIS_PYTHONCM_PATH"])
    module_path = query_helper.resolve_module_path(candidates)
    if not module_path:
        parser.error(f"pythoncm not found; searched: {', '.join(candidates)}")

    socket_path = os.path.abspath(os.path.expanduser(args.socket))
    Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    manager = PersistentCluster(
        query_args,
        module_path,
        disconnect_timeout=max(0.1, args.disconnect_timeout),
    )
    # Fail startup if the first connection cannot be established. systemd retries;
    # accepting requests while disconnected would only defer the same failure to users.
    manager.connect()
    server = BrokerServer(
        socket_path,
        manager,
        max(5.0, args.keepalive_seconds),
        allowed_services=frozenset(args.services.split(",")),
        backlog=args.backlog,
    )
    os.chmod(socket_path, 0o660)

    def stop(_signum: int, _frame: object) -> None:
        # shutdown() deadlocks when called from serve_forever's own thread. Raising
        # KeyboardInterrupt exits the loop and reaches the deterministic cleanup below.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    _LOG.info(
        "listening on %s services=%s backlog=%d",
        socket_path,
        ",".join(sorted(server.allowed_services)),
        server.request_queue_size,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.close()
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
