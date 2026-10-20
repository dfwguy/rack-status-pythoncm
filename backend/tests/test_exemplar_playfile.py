# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The playfile install-dgxc-benchmarking-workspace.sh writes for llmb-install --play.

v26.06+ headless installs build their config from the playfile alone and ignore the -i flag, so a
shared image folder passed only as -i was dropped and the NeMo image was srun-imported on an
air-gapped compute node. These run the script in record mode against a stand-in for the
dgxc-benchmarking checkout (and a stub git, which the CI image does not ship) and read back what
it wrote.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "scripts" / "install-dgxc-benchmarking-workspace.sh"
UNIFIED_MARKER = "slurm_info/env_vars are no longer supported"


def _checkout(top: Path, unified: bool) -> Path:
    # A .git directory is all Phase 1 needs to treat the checkout as present; the stub git below
    # answers its fetch/checkout/config calls, so no real repository (or git binary) is required.
    repo = top / "dgxc-benchmarking"
    src = repo / "cli" / "llmb-install" / "src"
    src.mkdir(parents=True)
    (repo / ".git").mkdir()
    (src / "models.py").write_text(f'MSG = "{UNIFIED_MARKER}"\n' if unified else "MSG = ''\n")
    return repo


def _stub_git(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "git"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return bin_dir


def _record(tmp_path: Path, unified: bool, **env: str) -> dict:
    top = tmp_path / "ws"
    repo = _checkout(top, unified)
    run_env = {
        "PATH": f"{_stub_git(tmp_path)}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "NVIS_EXEMPLAR_MODE": "record",
        "NVIS_EXEMPLAR_GPU_TYPE": "gb300",
        "NVIS_EXEMPLAR_TRUST_GIT": "0",
        "NVIS_EXEMPLAR_REPO_URL": str(tmp_path / "no-remote"),
        **env,
    }
    (tmp_path / "home").mkdir()
    proc = subprocess.run(
        ["bash", str(SCRIPT), str(top)], env=run_env, capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return yaml.safe_load((repo / "nvis-llmb-config.yaml").read_text())


@pytest.mark.parametrize("unified", [True, False], ids=["v26-unified", "legacy"])
def test_the_image_folder_is_written_into_the_playfile(tmp_path, unified):
    play = _record(tmp_path, unified, NVIS_EXEMPLAR_IMAGE_FOLDER="/opt/containers")
    assert play["image_folder"] == "/opt/containers"
    assert play["gpu_type"] == "gb300"


@pytest.mark.parametrize("unified", [True, False], ids=["v26-unified", "legacy"])
def test_no_image_folder_leaves_the_key_out(tmp_path, unified):
    # llmb then falls back to <install>/images, as it always did; an empty key would not.
    play = _record(tmp_path, unified)
    assert "image_folder" not in play


def test_the_unified_schema_still_carries_no_legacy_keys(tmp_path):
    play = _record(tmp_path, True, NVIS_EXEMPLAR_IMAGE_FOLDER="/opt/containers")
    assert "slurm_info" not in play and "env_vars" not in play
    assert play["slurm"]["gpu_partition"] == "defq"


# ---------------------------------------------------------------- Hugging Face downloads in install mode

_XET_LINE = 'export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"'


def _xet_value(**env: str) -> str:
    proc = subprocess.run(["bash", "-c", f'{_XET_LINE}\necho "$HF_HUB_DISABLE_XET"'],
                          env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
                          capture_output=True, text=True, timeout=10, check=True)
    return proc.stdout.strip()


def test_install_mode_downloads_without_xet_on_both_install_paths():
    """Xet's read-only shared blobs failed the v26.08.01 install when llmb-install rewrote a tokenizer."""
    text = SCRIPT.read_text(encoding="utf-8")
    line = text.index(_XET_LINE)
    assert text.index("# --- Phase 3 (install mode)") < line  # install mode only; record mode downloads nothing
    assert line < text.index('if [[ -f "${INSTALL_PATH}/cluster_config.yaml"')  # the incremental-add path
    assert line < text.index('"${PLAY_CMD[@]}" < <(yes')  # and the fresh --play path


def test_a_caller_choice_about_xet_wins():
    assert _xet_value() == "1"
    assert _xet_value(HF_HUB_DISABLE_XET="0") == "0"
