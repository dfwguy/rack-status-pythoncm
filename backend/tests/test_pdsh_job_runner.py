# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The pdsh fan-out contract of the bug-report endpoint (services restart left pdsh; see below).

Author: Rafael M. Koike & Cursor Agent ("Ada")

Written BEFORE extracting the ~28 lines these two functions had in common, because they had no
tests at all and they are not harmless: each fans pdsh across a rack, one to collect
nvidia-bug-report.sh and the other to restart munge/slurmd/nvidia-imex. Refactoring that on the
strength of "the diff looks equivalent" is how a rack gets a service bounce nobody asked for.

Every test was parametrized over both endpoints. Services restart has since moved to
``host_exec.run_on_nodes`` -- per-node results, the cluster's direct/via-head route and its sudo
setting -- and is tested in test_node_exec_settings.py; the parametrization is kept so another
pdsh job can join it. `pdsh_cmd` and `run_on_host` are stubbed: nothing here may reach a real host.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import host_exec
from api import bug_report

# (module, callable, the env var that forces a dry run)
ENDPOINTS = [
    pytest.param(bug_report, "run_bug_report_pdsh", "RACK_BUG_REPORT_DRY_RUN", id="bug-report"),
]


class _Platform:
    """Minimal stand-in for the BCM/MAAS provider: records the pdsh argv it was asked to build."""

    def __init__(self):
        self.calls = []

    def pdsh_cmd(self, w, inner, fanout=None):
        self.calls.append((w, inner, fanout))
        return ["pdsh", "-w", w, inner]

    def services_restart_remote_cmd(self):
        return "mount -a && systemctl restart munge"


@pytest.fixture
def rig(monkeypatch):
    """Neutralise expansion, the platform provider and execution for BOTH modules."""
    plat = _Platform()
    for mod in (bug_report,):
        monkeypatch.setattr(mod, "_expand_pdsh_w_slurm_nodenames", lambda w: w, raising=True)
        monkeypatch.setattr(mod, "_get_platform", lambda: plat, raising=True)
    # A remote dir that does not depend on the deployment layout.
    monkeypatch.setattr(bug_report, "_bug_report_remote_dir", lambda: "/tmp/bugreports",
                        raising=True)
    monkeypatch.delenv("RACK_BUG_REPORT_DRY_RUN", raising=False)
    return plat


def _run(mod, name, w="node-[01-02]"):
    return getattr(mod, name)(w)


@pytest.mark.parametrize(("mod", "name", "dry_env"), ENDPOINTS)
def test_empty_pattern_is_refused(mod, name, dry_env, rig):
    """An empty -w would make pdsh target nothing, or everything, depending on the build."""
    for blank in ("", "   ", None):
        with pytest.raises(ValueError, match="empty pdsh -w pattern"):
            getattr(mod, name)(blank)


@pytest.mark.parametrize(("mod", "name", "dry_env"), ENDPOINTS)
def test_dry_run_reports_the_command_without_executing(mod, name, dry_env, rig, monkeypatch):
    monkeypatch.setenv(dry_env, "1")

    def _explode(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("dry run must not execute anything")

    monkeypatch.setattr(host_exec, "run_on_host", _explode)

    out = _run(mod, name)
    assert out["returncode"] == 0
    assert out["pdsh_w"] == "node-[01-02]"
    assert "pdsh" in out["command_display"]
    assert out["stderr"] == ""
    assert "dry-run" in out["stdout"]


@pytest.mark.parametrize(("mod", "name", "dry_env"), ENDPOINTS)
def test_success_surfaces_returncode_and_stripped_streams(mod, name, dry_env, rig, monkeypatch):
    monkeypatch.setattr(
        host_exec, "run_on_host",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="  ok\n", stderr="  warn\n"),
    )
    out = _run(mod, name)
    assert out["returncode"] == 0
    assert out["stdout"] == "ok"
    assert out["stderr"] == "warn"
    assert out["pdsh_w"] == "node-[01-02]"


@pytest.mark.parametrize(("mod", "name", "dry_env"), ENDPOINTS)
def test_nonzero_returncode_is_reported_not_raised(mod, name, dry_env, rig, monkeypatch):
    """A partial pdsh failure must come back as data; the caller decides what it means."""
    monkeypatch.setattr(
        host_exec, "run_on_host",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="node-01: failed"),
    )
    out = _run(mod, name)
    assert out["returncode"] == 1
    assert out["stderr"] == "node-01: failed"


@pytest.mark.parametrize(("mod", "name", "dry_env"), ENDPOINTS)
def test_missing_pdsh_binary_becomes_oserror(mod, name, dry_env, rig, monkeypatch):
    def _missing(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "pdsh")

    monkeypatch.setattr(host_exec, "run_on_host", _missing)
    with pytest.raises(OSError, match="pdsh executable not found"):
        _run(mod, name)


@pytest.mark.parametrize(("mod", "name", "dry_env"), ENDPOINTS)
def test_timeout_is_reported_as_negative_one(mod, name, dry_env, rig, monkeypatch):
    """A wedged fan-out must not raise: the UI needs the command text to show the operator."""
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pdsh", timeout=7)

    monkeypatch.setattr(host_exec, "run_on_host", _timeout)
    out = _run(mod, name)
    assert out["returncode"] == -1
    assert out["stdout"] == ""
    assert "timeout after" in out["stderr"]
    assert "pdsh" in out["command_display"]


@pytest.mark.parametrize(("mod", "name", "dry_env"), ENDPOINTS)
def test_expansion_result_is_what_gets_targeted(mod, name, dry_env, rig, monkeypatch):
    """The rack id -> real hostname expansion must reach pdsh, not the caller's raw string."""
    monkeypatch.setattr(mod, "_expand_pdsh_w_slurm_nodenames", lambda w: f"{w}-expanded")
    monkeypatch.setattr(
        host_exec, "run_on_host",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    out = _run(mod, name, "rack-a")
    assert out["pdsh_w"] == "rack-a-expanded"
    assert rig.calls[-1][0] == "rack-a-expanded"


def test_bug_report_passes_its_own_timeout(rig, monkeypatch):
    """The shared runner must not flatten the bug report's long timeout budget."""
    seen = {}

    def _capture(cmd, timeout=None, **k):
        seen[tuple(cmd)] = timeout
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(host_exec, "run_on_host", _capture)
    monkeypatch.setenv("RACK_BUG_REPORT_TIMEOUT", "4242")
    bug_report.run_bug_report_pdsh("n1")
    assert 4242 in seen.values(), seen
