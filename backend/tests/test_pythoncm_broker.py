# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Unit coverage for the long-lived, serialized PythonCM owner."""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "scripts" / "nvis_pythoncm_broker.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("nvis_pythoncm_broker", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


broker = _load_script()


class _Cluster:
    def __init__(self):
        self._nvis_auth_mode = "certificate"
        self.disconnects = 0

    def disconnect(self):
        self.disconnects += 1


def _manager(monkeypatch):
    clusters = []

    def connect(_args):
        cluster = _Cluster()
        clusters.append(cluster)
        return cluster

    monkeypatch.setattr(broker.query_helper, "_connect", connect)
    args = broker.query_helper.build_parser().parse_args(["--query", "version"])
    return broker.PersistentCluster(args, "/matching/pythoncm"), clusters


def test_multiple_queries_reuse_one_cluster_connection(monkeypatch):
    manager, clusters = _manager(monkeypatch)
    monkeypatch.setattr(
        broker.query_helper,
        "_run_query",
        lambda cluster, args, used, started: {"ok": True, "query": args.query, "data": []},
    )

    first = manager.execute(["--query", "nodes"])
    second = manager.execute(["--query", "node-states"])

    assert len(clusters) == 1
    assert first["connection_generation"] == second["connection_generation"] == 1
    assert first["transport"] == second["transport"] == "persistent-pythoncm"


def test_failed_read_reconnects_once_then_succeeds(monkeypatch):
    manager, clusters = _manager(monkeypatch)
    calls = 0

    def run(cluster, args, used, started):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("stale connection")
        return {"ok": True, "query": args.query, "data": []}

    monkeypatch.setattr(broker.query_helper, "_run_query", run)
    result = manager.execute(["--query", "nodes"])

    assert result["ok"] is True
    assert result["connection_generation"] == 2
    assert len(clusters) == 2
    assert clusters[0].disconnects == 1


def test_failed_mutation_is_never_replayed(monkeypatch):
    manager, clusters = _manager(monkeypatch)
    manager.connect()._nvis_auth_mode = "cookie"
    calls = 0

    def fail(cluster, args, used, started):
        nonlocal calls
        calls += 1
        raise TimeoutError("result unknown")

    monkeypatch.setattr(broker.query_helper, "_run_query", fail)
    result = manager.execute(["--query", "power", "--target", "n1", "--action", "reset"])

    assert result["ok"] is False
    assert calls == 1
    assert len(clusters) == 1


def test_wedged_disconnect_forces_broker_exit(monkeypatch):
    manager, _clusters = _manager(monkeypatch)
    manager.connect().disconnect = lambda: threading.Event().wait()
    exits = []
    manager.disconnect_timeout = 0.01
    manager.fatal_exit = exits.append

    with pytest.raises(broker.BrokerFatalError, match="disconnect timeout"):
        manager.close()

    assert exits == [75]
