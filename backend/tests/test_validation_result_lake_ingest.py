# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
from __future__ import annotations

import os

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")
os.environ.setdefault("RACK_WEB_AUTH_DISABLED", "1")

import app as app_module
import core  # noqa: E402


def test_new_activity_result_ingests_once_when_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "_universal_enabled", lambda: True)
    monkeypatch.setattr(core, "history_enabled", lambda: True)
    monkeypatch.setattr(app_module.results_history, "latest_run_for_log", lambda _p: None)
    monkeypatch.setattr(
        app_module,
        "_universal_fallback_for_log",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"history_run_id": "run-1"},
    )

    app_module._ingest_new_activity_result("/logs", "/logs/result-42.txt", "r01")

    assert len(calls) == 1
    assert str(calls[0][0][0]) == "/logs"
    assert calls[0][0][1] == "/logs/result-42.txt"
    assert calls[0][1] == {"rack_hint": "r01", "ingest": True}


def test_existing_activity_result_does_not_reread_remote_log(monkeypatch):
    monkeypatch.setattr(app_module, "_universal_enabled", lambda: True)
    monkeypatch.setattr(core, "history_enabled", lambda: True)
    monkeypatch.setattr(
        app_module.results_history,
        "latest_run_for_log",
        lambda _p: {"run_id": "already-there"},
    )
    monkeypatch.setattr(
        app_module,
        "_universal_fallback_for_log",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not reread")),
    )

    app_module._ingest_new_activity_result("/logs", "/logs/result-42.txt", "r01")
