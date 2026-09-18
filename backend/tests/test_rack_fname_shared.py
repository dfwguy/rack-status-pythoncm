# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Rack id and job id from a log basename must be one mapping, not one per script.

``rack_status.sh`` (the shell table) and ``parser.sh`` (Parser Results) each carried their own
``rack_from_fname``/``jobid_from_fname``, and the copies had drifted so each lost data the
other kept:

  * ClusterKit ``root-CK-…`` names resolved only in rack_status.sh.
  * The generic ``NVIS_RACK_FNAME_RE`` fallback existed only in parser.sh, so on Nscale TX
    (``ink50-dh01-h22``) every log showed rack ``?`` in the shell table.
  * A plain ``…-<jid>.txt`` tail (MNUBERGEMM, nvbandwidth) yielded a job id only in parser.sh.

Both now source ``bin/scripts/lib/rack_fname.inc.sh``, which is the union. Where the callers
genuinely need different answers -- Llama stdout, tabulated per node by the parser and per rack
by the table -- there are two named functions instead of an accidental divergence.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "bin" / "scripts"
INC = SCRIPTS / "lib" / "rack_fname.inc.sh"
RACK_STATUS = SCRIPTS / "rack_status.sh"
PARSER = SCRIPTS / "parser.sh"


def _sh(func: str, name: str, **env: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", f'source "{INC}"; {func} "$1"', "_", name],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **env},
    check=False)
    assert proc.returncode in (0, 1), proc.stderr
    return proc.stdout.strip()


# --- rack id ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,want",
    [
        # Bright / cmsh
        ("root-NEMO-1Rack-p05-r01-18N-294.txt", "p05-r01"),
        ("cmods-NCCL-1Rack-p5-r1-18N-1.txt", "p5-r1"),
        # CBR / Firmus
        ("root-NEMO-1Rack-CBR01-DH01-SVR-CN0102[11-18]-18N-7.txt", "CBR01-DH01-SVR-CN0102"),
        # ClusterKit: used to resolve only in rack_status.sh
        ("root-CK-CBR01-DH01-SVR-CN0102-888.txt", "CBR01-DH01-SVR-CN0102"),
        # Nscale TX via the generic fallback: used to be "?" in rack_status.sh
        ("cmods-NCCL-1Rack-ink50-dh01-h01-18N-127318.txt", "ink50-dh01-h01"),
        ("cmods-NEMO-1Rack-ink50-dh01-h22-18N-127327.txt", "ink50-dh01-h22"),
        ("cmods-MNUBERGEMM-ink50-dh01-i05-4242.txt", "ink50-dh01-i05"),
        # Llama stdout names a node; rack_from_fname must return the RACK
        ("root-NEMO-LLAMA3_8B-1N-p05-r01-ct05-212.txt", "p05-r01"),
        ("root-NEMO-LLAMA3_8B-1N-CBR01-DH01-SVR-CN010211-212.txt", "CBR01-DH01-SVR-CN0102"),
        # Nothing rack-shaped
        ("sysinfo.txt", "?"),
    ],
)
def test_rack_from_fname(name, want):
    assert _sh("rack_from_fname", name) == want


def test_rack_fname_re_is_overridable_per_site():
    """A site whose rack ids do not match the default shape can supply its own pattern."""
    name = "cmods-NEMO-1Rack-ZONE9_RACK7-18N-5.txt"
    assert _sh("rack_from_fname", name) == "?"
    assert _sh("rack_from_fname", name, NVIS_RACK_FNAME_RE="(ZONE[0-9]+_RACK[0-9]+)") == "ZONE9_RACK7"


def test_specific_shapes_win_over_the_generic_fallback():
    """The fallback is last, so a Bright name still yields the Bright rack id."""
    assert _sh("rack_from_fname", "root-NEMO-1Rack-p05-r01-18N-1.txt") == "p05-r01"


# --- Llama: two deliberate answers ---------------------------------------------------------


def test_llama_host_and_rack_are_both_available():
    name = "root-NEMO-LLAMA3_8B-1N-p05-r01-ct05-212.txt"
    assert _sh("llama_host_from_fname", name) == "p05-r01-ct05"
    assert _sh("rack_from_fname", name) == "p05-r01"
    # The parser's per-file tables key by node, matching its historical behaviour.
    assert _sh("rack_or_llama_host_from_fname", name) == "p05-r01-ct05"


def test_llama_host_is_empty_for_non_llama_logs():
    name = "root-NEMO-1Rack-p05-r01-18N-294.txt"
    assert _sh("llama_host_from_fname", name) == ""
    assert _sh("rack_or_llama_host_from_fname", name) == "p05-r01"


# --- job id -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,want",
    [
        ("root-NEMO-1Rack-p05-r01-18N-294.txt", "294"),
        ("cmods-NCCL-1Rack-ink50-dh01-h01-18N-127318.txt", "127318"),
        ("root-NEMO-LLAMA3_8B-1N-p05-r01-ct05-212.txt", "212"),
        # Plain trailing job id: used to be "?" in rack_status.sh
        ("cmods-MNUBERGEMM-ink50-dh01-i05-4242.txt", "4242"),
        ("sysinfo.txt", "?"),
    ],
)
def test_jobid_from_fname(name, want):
    assert _sh("jobid_from_fname", name) == want


def test_jobid_agrees_with_the_python_implementation():
    """The shell and Python extractors must not disagree about which digits are the job id."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "backend"))
    import nvis_id_templates as nid

    for name in (
        "root-NEMO-1Rack-p05-r01-18N-294.txt",
        "cmods-NEMO-1Rack-ink50-dh01-h22-18N-127327.txt",
        "root-NEMO-LLAMA3_8B-1N-p05-r01-ct05-212.txt",
    ):
        assert _sh("jobid_from_fname", name) == nid.job_id_from_log_basename(name), name


# --- structure ----------------------------------------------------------------------------


@pytest.mark.parametrize("script", [RACK_STATUS, PARSER])
def test_script_sources_the_shared_include(script: Path):
    assert "lib/rack_fname.inc.sh" in script.read_text(encoding="utf-8")


@pytest.mark.parametrize("script", [RACK_STATUS, PARSER])
@pytest.mark.parametrize("func", ["rack_from_fname", "jobid_from_fname"])
def test_script_does_not_redefine_the_mapping(script: Path, func: str):
    for line in script.read_text(encoding="utf-8").splitlines():
        if re.match(rf"^\s*{func}\s*\(\)\s*\{{", line):
            assert "declare -F" in line, f"{script.name} redefines {func}: {line.strip()}"
