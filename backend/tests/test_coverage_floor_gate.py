# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The coverage gate refuses what it is supposed to refuse (REQ-45).

Author: Rafael M. Koike & Cursor Agent ("Ada")

The gate is a merge blocker, which makes its FAILURE paths the interesting ones. A gate that stops
failing does not announce itself -- it just quietly lets new untested code in, and the first symptom
is a coverage number drifting down over months. So these cases drive it with synthetic reports and
assert the exit status, rather than checking it passes on the real one (CI does that already).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "bin" / "check-coverage-floor.py"


def _load_gate():
    """Import the gate by path -- it lives in bin/ and is not an importable module."""
    spec = importlib.util.spec_from_file_location("_coverage_floor_gate", GATE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()


def _report(files: dict[str, float], statements: int = 100) -> dict:
    """A coverage.json shaped like the real thing, with just the fields the gate reads."""
    return {
        "files": {
            name: {"summary": {"percent_covered": pct, "num_statements": statements}}
            for name, pct in files.items()
        },
        "totals": {"percent_covered": 65.0},
    }


def _run(tmp_path: Path, report: dict) -> subprocess.CompletedProcess:
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(GATE), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


class TestTheGateRefuses:
    def test_a_new_file_below_the_floor_fails(self, tmp_path):
        """The whole point: a new feature cannot merge untested."""
        proc = _run(tmp_path, _report({"backend/brand_new_feature.py": 12.0}))
        assert proc.returncode == 1
        assert "below the 50% floor" in proc.stdout
        assert "brand_new_feature.py" in proc.stdout

    def test_a_covered_file_regressing_below_the_floor_fails(self, tmp_path):
        proc = _run(tmp_path, _report({"backend/audit/sink.py": 30.0}))
        assert proc.returncode == 1
        assert "audit/sink.py" in proc.stdout

    def test_a_listed_file_getting_worse_fails(self, tmp_path):
        """The exception list may only shrink, so a listed file may not decay further."""
        name, baseline = next(iter(gate.EXCEPTIONS.items()))
        proc = _run(tmp_path, _report({name: baseline - 10.0}))
        assert proc.returncode == 1
        assert "may only shrink" in proc.stdout

    def test_a_missing_report_is_a_usage_error_not_a_pass(self, tmp_path):
        """A gate that passes when its input is absent is worse than no gate."""
        proc = subprocess.run(
            [sys.executable, str(GATE), str(tmp_path / "nope.json")],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 64

    def test_a_malformed_report_is_a_usage_error_not_a_pass(self, tmp_path):
        path = tmp_path / "coverage.json"
        path.write_text("{\"not\": \"a coverage report\"}", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(GATE), str(path)],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 64


class TestTheGateAllows:
    def test_a_file_at_the_floor_passes(self, tmp_path):
        assert _run(tmp_path, _report({"backend/thing.py": gate.FLOOR})).returncode == 0

    def test_a_file_with_no_statements_is_not_a_violation(self, tmp_path):
        """An empty __init__.py reports 0% and must not fail the build."""
        report = _report({"backend/__init__.py": 0.0}, statements=0)
        assert _run(tmp_path, report).returncode == 0

    def test_noise_within_tolerance_on_a_listed_file_passes(self, tmp_path):
        """Coverage moves by fractions when unrelated code lands; a gate that fails on that
        gets switched off, which is the real failure mode."""
        name, baseline = next(iter(gate.EXCEPTIONS.items()))
        proc = _run(tmp_path, _report({name: baseline - (gate.TOLERANCE / 2)}))
        assert proc.returncode == 0

    def test_a_listed_file_rising_above_the_floor_passes_but_says_so(self, tmp_path):
        """Paying down debt must not fail the build, and the advice must be qualified.

        A working tree can read higher than a clean checkout -- universal_parser.py is 52.8% locally
        and 48.1% from a tracked-files-only export -- so "remove this entry" is wrong advice unless
        it was confirmed in CI's tree. The gate says so rather than inviting a removal that turns
        the pipeline red.
        """
        name = next(iter(gate.EXCEPTIONS))
        proc = _run(tmp_path, _report({name: 90.0}))
        assert proc.returncode == 0
        assert "cleared the floor in this run" in proc.stdout
        assert "tracked-files-only" in proc.stdout


class TestTheExceptionListIsHonest:
    def test_every_listed_file_exists(self):
        """A stale entry is a hole: the gate skips a path that no longer means anything."""
        missing = [n for n in gate.EXCEPTIONS if not (REPO_ROOT / n).is_file()]
        assert not missing, f"EXCEPTIONS names files that do not exist: {missing}"

    def test_every_listed_baseline_is_below_the_floor(self):
        """An entry at or above the floor does not need to be an exception at all."""
        pointless = {n: v for n, v in gate.EXCEPTIONS.items() if v >= gate.FLOOR}
        assert not pointless, f"EXCEPTIONS entries that are already above the floor: {pointless}"

    @pytest.mark.parametrize("name", sorted(gate.EXCEPTIONS))
    def test_listed_files_are_backend_sources(self, name):
        assert name.startswith("backend/") and name.endswith(".py")
