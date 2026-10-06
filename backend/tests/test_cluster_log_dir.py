# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Where a cluster's logs are read from is a property of the cluster, not of the process.

`RACK_STATUS_LOG_DIR` was a single global directory, which is correct only for a single-tenant
install: the moment a deployment serves more than one cluster, that one value makes a project read
logs from a directory belonging to a cluster it does not operate. The log directory is now a
per-cluster field (`connection.log_dir`) projected by the overlay, so it travels with the active
cluster; the global env var stays as a deprecated fallback that warns when it is actually used.
"""
from __future__ import annotations

import logging

import paths
import pytest
from catalog import context, models


def _cluster(**connection):
    return models.Cluster.from_dict({"id": "c1", "name": "C1", "connection": connection})


class TestTheField:
    def test_the_default_is_blank(self):
        assert _cluster().connection.log_dir == ""

    def test_a_value_is_kept(self):
        assert _cluster(log_dir="/data/nemo/logs").connection.log_dir == "/data/nemo/logs"

    def test_it_round_trips(self):
        c = _cluster(log_dir="/data/nemo/logs")
        assert c.to_dict()["connection"]["log_dir"] == "/data/nemo/logs"

    def test_it_is_independent_of_the_remote_base_path(self):
        """The default log dir derives from remote_base_path, but an explicit log_dir is its own
        field -- a site whose logs live apart from the tooling can say so."""
        c = _cluster(remote_base_path="/opt/nvis", log_dir="/scratch/validation/logs")
        assert (c.connection.remote_base_path, c.connection.log_dir) == (
            "/opt/nvis", "/scratch/validation/logs"
        )


class TestTheOverlayProjection:
    def test_the_overlay_publishes_the_cluster_log_dir(self):
        """paths._default_log_dir() reads NVIS_LOG_DIR. The cluster record is what sets it, so the
        log location travels with the active cluster rather than being process-global."""
        env = context.high_overlay(None, _cluster(log_dir="/data/nemo/logs"), None)
        assert env["NVIS_LOG_DIR"] == "/data/nemo/logs"

    def test_a_blank_log_dir_projects_nothing(self):
        """A cluster that has not set a log dir must not stamp an empty value over whatever a
        deployment has configured -- _put drops blanks."""
        env = context.high_overlay(None, _cluster(), None)
        assert "NVIS_LOG_DIR" not in env

    def test_the_overlay_does_not_touch_the_deprecated_global(self):
        """The clean per-cluster key is NVIS_LOG_DIR; the overlay never writes the deprecated
        RACK_STATUS_LOG_DIR, which is why configuring a cluster never trips its deprecation warning."""
        env = context.high_overlay(None, _cluster(log_dir="/data/nemo/logs"), None)
        assert "RACK_STATUS_LOG_DIR" not in env


class TestResolutionPrecedence:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.delenv(paths.CLUSTER_LOG_DIR_ENV, raising=False)
        monkeypatch.delenv(paths.LEGACY_LOG_DIR_ENV, raising=False)
        monkeypatch.setattr(paths.host_exec, "role_root", lambda _role: "")
        monkeypatch.setattr(paths, "_eget", lambda _key, default="": default)
        # The warning is once-per-process; reset so each case sees a fresh latch.
        monkeypatch.setattr(paths, "_warned_legacy_log_dir", False)

    def test_the_cluster_field_wins(self, monkeypatch):
        monkeypatch.setenv(paths.CLUSTER_LOG_DIR_ENV, "/data/cluster/logs")
        assert str(paths._default_log_dir()) == "/data/cluster/logs"

    def test_the_cluster_field_beats_the_deprecated_global(self, monkeypatch, caplog):
        """The whole point: with both set, the per-cluster value is used AND no deprecation warning
        fires, because the clean field -- not the global var -- is the source."""
        monkeypatch.setenv(paths.CLUSTER_LOG_DIR_ENV, "/data/cluster/logs")
        monkeypatch.setenv(paths.LEGACY_LOG_DIR_ENV, "/global/logs")
        with caplog.at_level(logging.WARNING, logger="paths"):
            assert str(paths._default_log_dir()) == "/data/cluster/logs"
        assert "DEPRECATED" not in caplog.text

    def test_the_deprecated_global_is_a_fallback_and_warns(self, monkeypatch, caplog):
        monkeypatch.setenv(paths.LEGACY_LOG_DIR_ENV, "/global/logs")
        with caplog.at_level(logging.WARNING, logger="paths"):
            assert str(paths._default_log_dir()) == "/global/logs"
        assert "RACK_STATUS_LOG_DIR" in caplog.text and "DEPRECATED" in caplog.text

    def test_the_warning_fires_once(self, monkeypatch, caplog):
        monkeypatch.setenv(paths.LEGACY_LOG_DIR_ENV, "/global/logs")
        with caplog.at_level(logging.WARNING, logger="paths"):
            paths._default_log_dir()
            paths._default_log_dir()
        assert caplog.text.count("will be removed in a future release") == 1

    def test_neither_set_falls_back_to_the_derived_default(self, monkeypatch):
        assert paths._default_log_dir() == paths._rpath(str(paths.REPO_ROOT)) / "var" / "logs"

    def test_default_follows_base_changes_after_startup(self, monkeypatch):
        base = {"value": "/head/project-a"}
        monkeypatch.setattr(paths, "_eget", lambda _key, default="": base["value"])
        assert str(paths._default_log_dir()) == "/head/project-a/var/logs"
        base["value"] = "/head/project-b"
        assert str(paths._default_log_dir()) == "/head/project-b/var/logs"
        base["value"] = ""
        assert paths._default_log_dir() == paths._rpath(str(paths.REPO_ROOT)) / "var" / "logs"

    def test_logs_role_root_wins_over_remote_base(self, monkeypatch):
        monkeypatch.setattr(paths.host_exec, "role_root", lambda _role: "/shared/logs")
        monkeypatch.setattr(paths, "_eget", lambda _key, default="": "/head/tooling")
        assert str(paths._default_log_dir()) == "/shared/logs"

    def test_validation_checks_the_new_projects_log_directory(self, monkeypatch):
        monkeypatch.delenv("RACK_STATUS_ALLOWED_PREFIXES", raising=False)
        monkeypatch.setattr(paths, "_eget", lambda _key, default="": "/head/new-project")
        monkeypatch.setattr(paths.host_exec, "execution_host_configured", lambda: True)
        monkeypatch.setattr(
            paths.host_exec, "is_dir", lambda path: str(path) == "/head/new-project/var/logs"
        )
        assert str(paths._resolve_log_dir(None)) == "/head/new-project/var/logs"

    def test_explicit_cluster_log_dir_wins_over_role_root(self, monkeypatch):
        monkeypatch.setenv(paths.CLUSTER_LOG_DIR_ENV, "/cluster/logs")
        monkeypatch.setattr(paths.host_exec, "role_root", lambda _role: "/shared/logs")
        assert str(paths._default_log_dir()) == "/cluster/logs"
