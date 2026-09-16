# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Contracts for terminal NeMo evidence consumed by the hot manifest collector."""
from __future__ import annotations

from pathlib import Path
import re

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "bin/scripts/nemo-nemotron5h_56b-mbridge-fp8.sh"
HOT_MANIFEST_MARKER = re.compile(r"^NEMO_VALIDATION_(PASS|FAIL)\b", re.MULTILINE)


@pytest.mark.parametrize(
    ("stage", "rc_var", "error_text"),
    (
        ("training", "train_rc", "NeMo training srun failed"),
        ("postcheck", "postcheck_rc", "NeMo post-run checker failed"),
    ),
)
def test_failure_branch_emits_anchored_machine_record_before_exit(stage: str, rc_var: str, error_text: str):
    body = SCRIPT.read_text(encoding="utf-8")
    branch = re.search(
        rf'^if \(\( {rc_var} != 0 \)\); then\n(?P<body>.*?)(?=^fi$)',
        body,
        re.MULTILINE | re.DOTALL,
    )
    assert branch, f"missing {stage} failure branch"
    lines = [line for line in branch.group("body").splitlines() if line.strip()]
    marker = (
        f'  echo "NEMO_VALIDATION_FAIL stage={stage} rc=${{{rc_var}}} '
        'nodes=${SLURM_NNODES} max_steps=${MAX_STEPS}"'
    )
    assert lines[0] == marker
    assert error_text in lines[1]
    assert lines[-1] == f'  exit "${rc_var}"'

    rendered = lines[0].strip()[len('echo "') : -1]
    rendered = rendered.replace(f"${{{rc_var}}}", "9")
    rendered = rendered.replace("${SLURM_NNODES}", "18").replace("${MAX_STEPS}", "2010")
    match = HOT_MANIFEST_MARKER.search(rendered)
    assert match and match.group(1) == "FAIL"
    assert f"stage={stage} rc=9 nodes=18 max_steps=2010" in rendered


def test_success_and_failure_records_share_the_collector_anchor():
    body = SCRIPT.read_text(encoding="utf-8")
    terminal_records = re.findall(r'^\s*echo "(NEMO_VALIDATION_(?:PASS|FAIL)[^"]*)"', body, re.MULTILINE)
    assert len(terminal_records) == 3
    assert [HOT_MANIFEST_MARKER.search(record).group(1) for record in terminal_records] == [
        "FAIL",
        "FAIL",
        "PASS",
    ]
