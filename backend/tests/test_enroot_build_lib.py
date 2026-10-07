# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The two decisions in an enroot image build that make the image trustworthy.

Author: Rafael M. Koike

``bin/scripts/lib/enroot_build.inc.sh`` answers *which rootfs gets scrubbed* before export -- resolve
it wrong and the builder's identity ships in the image -- and *what the image records about its
sources* -- an untagged tree must be called out, because that is how a rebuild silently picked up a
two-day-old nccl-tests on Nscale NC. Both are pinned here without a 40-minute build: rootfs
resolution against enroot's own precedence, provenance against a stub ``git``. And the recipe must
resolve the rootfs *before* the long build, so a path it cannot find costs seconds, not an hour.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "bin" / "scripts" / "lib" / "enroot_build.inc.sh"
RECIPE = REPO / "bin" / "scripts" / "build_nemo_nccl_image.sh"


def _bash(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    # check=False: some cases assert on stderr and exit status.
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}"; {snippet}'], env=env, capture_output=True, text=True, check=False
    )


@pytest.fixture
def base_env(tmp_path):
    """A clean environment: no enroot variables, and a sysconf dir the test controls."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ENROOT_", "XDG_"))}
    env["HOME"] = str(tmp_path / "home")
    env["ENROOT_SYSCONF_PATH"] = str(tmp_path / "etc-enroot")
    (tmp_path / "etc-enroot").mkdir()
    return env


def _conf(env: dict[str, str], text: str) -> None:
    Path(env["ENROOT_SYSCONF_PATH"], "enroot.conf").write_text(text)


class TestEnrootRootfsDir:
    def test_the_environment_wins_over_the_config_file(self, base_env):
        _conf(base_env, "ENROOT_DATA_PATH /from/conf\n")
        base_env["ENROOT_DATA_PATH"] = "/from/env"
        assert _bash("enroot_rootfs_dir nemo", base_env).stdout.strip() == "/from/env/nemo"

    def test_a_config_value_holding_shell_is_evaluated(self, base_env):
        # Nscale NC's enroot.conf: "ENROOT_DATA_PATH /var/lib/enroot/data/$(id -u)".
        _conf(base_env, "ENROOT_RUNTIME_PATH /run/x\nENROOT_DATA_PATH     /var/lib/enroot/data/$(id -u)\n")
        out = _bash("enroot_rootfs_dir nemo", base_env).stdout.strip()
        assert out == f"/var/lib/enroot/data/{os.getuid()}/nemo"

    def test_a_commented_out_setting_is_ignored(self, base_env):
        _conf(base_env, "#ENROOT_DATA_PATH /wrong\n")
        base_env["XDG_DATA_HOME"] = "/xdg"
        assert _bash("enroot_rootfs_dir nemo", base_env).stdout.strip() == "/xdg/enroot/nemo"

    def test_without_any_setting_it_is_enroots_default_under_home(self, base_env):
        out = _bash("enroot_rootfs_dir nemo", base_env).stdout.strip()
        assert out == f"{base_env['HOME']}/.local/share/enroot/nemo"


@pytest.fixture
def stub_git(tmp_path, base_env):
    """`git` on PATH that answers `describe` from the test's settings."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    git = bindir / "git"
    git.write_text(
        "#!/bin/bash\n"
        'case " $* " in\n'
        '  *" --exact-match "*) exit "${STUB_EXACT:-0}" ;;\n'
        "esac\n"
        '[ -n "${STUB_FAIL:-}" ] && exit 128\n'
        'echo "$STUB_DESCRIBE"\n'
    )
    git.chmod(0o755)
    base_env["PATH"] = f"{bindir}:{base_env['PATH']}"
    return base_env


class TestSrcRev:
    def test_a_tree_at_a_tag_reports_the_tag_quietly(self, stub_git):
        res = _bash("src_rev /src/nccl", {**stub_git, "STUB_DESCRIBE": "v2.32.3-1", "STUB_EXACT": "0"})
        assert res.stdout == "v2.32.3-1"
        assert res.stderr == ""

    def test_an_untagged_tree_is_called_out(self, stub_git):
        res = _bash(
            "src_rev /src/nccl-tests",
            {**stub_git, "STUB_DESCRIBE": "v2.20.0-11-gc9222a4", "STUB_EXACT": "128"},
        )
        assert res.stdout == "v2.20.0-11-gc9222a4"
        assert "WARNING: /src/nccl-tests is not at a tag (v2.20.0-11-gc9222a4)" in res.stderr

    def test_a_tree_git_cannot_describe_is_unknown(self, stub_git):
        res = _bash("src_rev /not/a/repo", {**stub_git, "STUB_FAIL": "1", "STUB_EXACT": "128"})
        assert res.stdout == "unknown"
        assert "not at a tag (unknown)" in res.stderr


def test_the_recipe_resolves_the_rootfs_before_the_long_build():
    """A rootfs it cannot find must abort in seconds, before NCCL compiles -- not after."""
    text = RECIPE.read_text()
    resolve = text.index('ROOTFS="$(enroot_rootfs_dir "$NAME")"')
    guard = text.index('[ -f "$ROOTFS/etc/passwd" ] || {')
    build = text.index('echo "== [2/5] build NCCL')
    scrub = text.index('"$SCRUB" "$ROOTFS" "$BASE_SQSH"')
    export = text.index('enroot export --output "$OUT_SQSH"')
    assert resolve < guard < build < scrub < export
