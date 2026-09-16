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

import healthcheck_evidence


def _node(root, name: str, marker: str, log: str = ""):
    path = root / name
    path.mkdir()
    (path / ".last-check").write_text(marker, encoding="utf-8")
    (path / "health.log").write_text(log, encoding="utf-8")


def test_healthcheck_events_are_current_attributed_and_explain_failures(tmp_path, monkeypatch):
    _node(
        tmp_path,
        "ink50-dh02-a03-14",
        "1788396873\tfailed\n",
        "2026-09-02 differs from /cluster/bringup/valid-config/default/: ib-link imex-channels enroot\n",
    )
    _node(tmp_path, "ink50-dh02-a03-15", "1788396874 passed\n")
    monkeypatch.setenv("RACK_HEALTHCHECK_LOG_ROOT", str(tmp_path))
    healthcheck_evidence.clear_cache()

    events = healthcheck_evidence.post_run_events()

    assert [(row["node"], row["state"]) for row in events] == [
        ("ink50-dh02-a03-14", "FAIL"),
        ("ink50-dh02-a03-15", "PASS"),
    ]
    assert events[0]["reason"] == "Failed checks: ib-link, imex-channels, enroot"
    assert events[0]["attributed_failure"] is True
    assert events[0]["validation_test"] == "node-health"
    assert events[1]["reason"].startswith("All configured")


def test_healthcheck_events_support_node_and_rack_filters(tmp_path, monkeypatch):
    _node(tmp_path, "ink50-dh02-a03-14", "1788396873 failed\n")
    _node(tmp_path, "ink50-dh02-a04-14", "1788396874 passed\n")
    monkeypatch.setenv("RACK_HEALTHCHECK_LOG_ROOT", str(tmp_path))
    healthcheck_evidence.clear_cache()

    assert [row["node"] for row in healthcheck_evidence.post_run_events(rack="ink50-dh02-a03")] == ["ink50-dh02-a03-14"]
    assert [row["node"] for row in healthcheck_evidence.post_run_events(node="ink50-dh02-a04-14")] == ["ink50-dh02-a04-14"]
