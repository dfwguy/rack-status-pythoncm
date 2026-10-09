# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The cluster's workspace is a project setting that cascades to every script -- never a script literal.

The NCCL Benchmark (Stage-1) failed on Nscale NC with ``llmb-run not found (searched under
LLMB_INSTALL=/home/nvis/dgxc-benchmarking)``: the launcher carried a default from an earlier
cluster while the installer had put LLMB under ``/home/cmods/workspace/llmb``. Each script had
its own guess (``/home/nvis``, ``/home/cmods``, ``/home/cmsupport``, ``/home/*``), so every new
deployment meant editing scripts. Now the cluster record's ``connection.workspace_dir`` is
exported as ``NVIS_WORKSPACE_DIR`` and ``bin/scripts/workspace-dir-common.sh`` is the one place
that turns it into paths. See docs/explanation/how-cluster-paths-reach-scripts.md.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import config_env
import pytest
from catalog import context, models

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"
COMMON = SCRIPTS / "workspace-dir-common.sh"
LLMB = SCRIPTS / "lib" / "llmb_paths.inc.sh"
_WS_VARS = ("WORKSPACE_DIR", "NVIS_WORKSPACE_DIR", "RACK_WORKSPACE_DIR", "TOPDIR", "LLMB_INSTALL", "LLMB_VENV")


def _cluster(**connection):
    return models.Cluster.from_dict({"id": "c1", "name": "C1", "connection": connection})


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in _WS_VARS}
    env.update(extra)
    return env


class TestTheProjectSetting:
    def test_it_round_trips(self):
        c = _cluster(workspace_dir="/home/cmods/workspace")
        assert c.to_dict()["connection"]["workspace_dir"] == "/home/cmods/workspace"

    @pytest.mark.parametrize("bad", ["relative/ws", "/home/my ws", "/home/$(id)", "/a/../b"])
    def test_paths_a_script_cannot_use_safely_are_refused(self, bad):
        assert any("workspace_dir" in p for p in _cluster(workspace_dir=bad).connection.validate())

    def test_the_overlay_exports_it(self):
        env = context.high_overlay(None, _cluster(workspace_dir="/home/cmods/workspace"), None)
        assert env["NVIS_WORKSPACE_DIR"] == "/home/cmods/workspace"
        assert "NVIS_WORKSPACE_DIR" not in context.high_overlay(None, _cluster(), None)


class TestNoGhostDefault:
    def test_unset_resolves_to_nothing_not_a_strangers_home(self, monkeypatch):
        monkeypatch.setattr(config_env, "_eget", lambda key, default=None: "")
        assert config_env._nvis_workspace_dir_effective() == ""

    def test_a_tool_without_a_workspace_says_where_to_set_it(self, monkeypatch):
        from api import tools

        monkeypatch.setattr(tools, "_nvis_workspace_dir_effective", lambda: "")
        with pytest.raises(ValueError, match="Projects -> cluster -> Workspace"):
            tools._resolve_workspace({})

    def test_a_request_may_still_name_one(self, monkeypatch):
        from api import tools

        monkeypatch.setattr(tools, "_nvis_workspace_dir_effective", lambda: "")
        monkeypatch.setattr(tools, "_resolve_tools_destination", lambda raw: raw)
        assert tools._resolve_workspace({"workspace": "/scratch/ws"}) == "/scratch/ws"


def _source(env, extra=""):
    script = f'. "{COMMON}" || exit $?; {extra}'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)


class TestTheShellResolver:
    def test_no_workspace_is_an_error_naming_the_setting(self):
        r = _source(_clean_env())
        assert r.returncode == 2
        assert "Projects -> cluster -> Workspace" in r.stderr

    def test_the_workspace_resolves_and_trims(self):
        r = _source(_clean_env(NVIS_WORKSPACE_DIR="/home/cmods/workspace/"), 'echo "$WORKSPACE_DIR|$TOPDIR"')
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "/home/cmods/workspace|/home/cmods/workspace"

    def test_an_installer_argument_beats_the_setting(self):
        r = _source(_clean_env(NVIS_WORKSPACE_DIR="/ws", WORKSPACE_DIR="/arg"), 'echo "$TOPDIR"')
        assert r.stdout.strip() == "/arg"

    def test_an_installer_refuses_to_guess(self, tmp_path):
        """Installers used to default to /home/cmsupport/workspace -- installing where nothing reads."""
        r = subprocess.run(["bash", str(SCRIPTS / "install-hpcx-workspace.sh")], capture_output=True,
                           text=True, env=_clean_env(), cwd=tmp_path, check=False, timeout=60)
        assert r.returncode == 2
        assert "no workspace configured" in r.stderr


def _llmb(env, extra=""):
    script = f'. "{LLMB}" || exit $?; {extra}'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)


class TestTheLlmbResolver:
    def test_llmb_paths_derive_from_the_workspace(self):
        r = _llmb(_clean_env(NVIS_WORKSPACE_DIR="/home/cmods/workspace/"), 'echo "$LLMB_INSTALL|$LLMB_VENV"')
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "/home/cmods/workspace/llmb|/home/cmods/workspace/llmb_venv"

    def test_an_explicit_install_is_complete_without_a_workspace(self):
        """A collector told exactly where LLMB is must not demand a workspace it does not use."""
        r = _llmb(_clean_env(LLMB_INSTALL="/elsewhere/llmb/"), 'echo "$LLMB_INSTALL|${LLMB_VENV:-<none>}"')
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "/elsewhere/llmb|<none>"

    def test_neither_is_an_error_naming_the_setting(self):
        r = _llmb(_clean_env())
        assert r.returncode == 2
        assert "Projects -> cluster -> Workspace" in r.stderr


# Code lines only: comments may tell the history of a path, code may not carry one.
_HOME_LITERAL = re.compile(r"/home/(?:\*|[a-z_][a-z0-9_-]*)/")


def _code_lines(path: Path):
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield number, line


def test_no_script_carries_a_user_home():
    offenders = [
        f"{path.relative_to(SCRIPTS)}:{n}: {line.strip()[:120]}"
        for path in sorted(SCRIPTS.rglob("*"))
        if path.is_file() and path.suffix in (".sh", ".sbatch", ".py")
        for n, line in _code_lines(path)
        if _HOME_LITERAL.search(line)
    ]
    assert not offenders, (
        "scripts must derive cluster paths from project settings (workspace-dir-common.sh, "
        "node_images.inc.sh, NVIS_SCRIPTS_DIR), not carry a site's /home:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("name", [
    "run_llmb_nccl.sh", "run_llmb_nemo.sh", "nccl_campaign.sh", "collect_llmb_nccl_log.sh",
    "collect_llmb_nemo_log.sh", "run-exemplar-system-info.sh", "run-exemplar-llama.sh",
    "run-llmb-nccl-container.sh", "run-validation-preset.sh", "llama405b_fp8_1024.sbatch",
    "llama405b_fp8_4096.sbatch",
])
def test_every_llmb_consumer_uses_the_one_resolver(name):
    assert "lib/llmb_paths.inc.sh" in (SCRIPTS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", [
    "nccl_allreduce.sbatch", "nccl_fullrack_rounds.sh", "build_nccl_host.sh", "install-hpcx-workspace.sh",
    "install-dgxc-benchmarking-workspace.sh", "install-cuda-nccl-workspace.sh",
])
def test_every_workspace_consumer_uses_the_one_resolver(name):
    assert "workspace-dir-common.sh" in (SCRIPTS / name).read_text(encoding="utf-8")
