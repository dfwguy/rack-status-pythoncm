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
"""`bin/bench-auth.py` — that the harness works, never that the numbers are fast.

**No timing is asserted anywhere in this file, deliberately.** A latency assertion running on a
shared CI runner fails on commits that changed nothing, and a perf test that cries wolf becomes a
perf test somebody sets to `allow_failure` and stops reading. The `auth-benchmark` job records; this
guards that it still *can*.

Which is a real risk rather than a theoretical one: the benchmark imports `identity.srp`,
`identity.tokens`, `identity.keys` and `authz.policy` and calls into all four. Any of those moving
breaks it — silently, because a job that produces an empty artifact still goes green. Then the
latency series quietly stops growing and nobody notices until somebody asks for a trend.

The metric *names* are pinned too. GitLab tracks a metric across pipelines by name, so a rename
starts a fresh series and discards the history this whole job exists to accumulate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "bin" / "bench-auth.py"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One `--quick` run, shared by every test here.

    Module-scoped because even the quick path pays a few SRP exponentiations, and re-running it per
    test would add seconds to the suite for no extra coverage.
    """
    out = tmp_path_factory.mktemp("bench")
    env = os.environ | {
        "RACK_WEB_SECRET_KEY": "test-secret-not-a-credential",
        "NVIS_IDENTITY_FILE": str(out / "identity.json"),
        "RACK_WEB_SIGNING_KEY_DIR": str(out / "oidc"),
    }
    env.pop("RACK_WEB_OIDC_ENABLED", None)
    metrics, record = out / "metrics.txt", out / "bench.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--quick",
         "--metrics", str(metrics), "--json", str(record)],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=600,
        # Not `check=True`: the exit code is what `test_it_exits_zero` asserts, with stderr in the
        # failure message. Raising here instead would surface a CalledProcessError with the output
        # swallowed, which is a worse report of the same fact.
        check=False,
    )
    return proc, metrics, record


class TestItRuns:
    def test_it_exits_zero(self, run):
        proc, _m, _j = run
        assert proc.returncode == 0, proc.stderr[-2000:]

    def test_it_exits_zero_even_though_it_measures_nothing_against_a_budget(self, run):
        """The exit code is not a verdict, and that is the design.

        Anything that made this script fail on a slow number would put a red pipeline in front of
        somebody who cannot act on it. `allow_failure: true` on the job covers plumbing; the script
        itself is unconditionally 0.
        """
        proc, _m, _j = run
        assert proc.returncode == 0

    def test_it_reports_which_srp_backend_bound(self, run):
        """The single most load-bearing line of output.

        `srp.begin` is ~1,400x every other operation and the two backends differ by ~2x, so a run
        whose backend is unknown is not comparable to any other run. This is also how we find out
        that a base-image bump changed it.
        """
        proc, _m, _j = run
        assert "SRP backend" in proc.stdout


class TestTheDurableRecord:
    def test_it_is_valid_json_with_a_schema(self, run):
        _p, _m, record = run
        payload = json.loads(record.read_text())
        assert payload["schema"] == "nvis-greenlight/auth-bench/v1"

    def test_the_environment_travels_with_the_numbers(self, run):
        """A latency history whose hardware is unrecorded cannot be reasoned about later.

        That is not hypothetical: `docs/PERFORMANCE_BASELINE.md` exists partly to unpick a ~52 ms
        figure recorded elsewhere in this repo without its SRP backend, which turned out to be 2x
        off for the machine reading it.
        """
        _p, _m, record = run
        env = json.loads(record.read_text())["environment"]
        for field in ("arch", "system", "python", "cores", "srp_backend", "container", "ci"):
            assert field in env, f"{field!r} missing — the numbers become unattributable"
        assert env["srp_backend"].startswith("srp.")

    def test_every_operation_is_recorded(self, run):
        _p, _m, record = run
        labels = {r["label"] for r in json.loads(record.read_text())["results"]}
        # The set the baseline document tabulates. A missing row means the doc and the series
        # disagree, and the doc is what people read.
        for expected in ("srp.begin (login leg 1)", "verify ES256 (provider on)",
                         "policy eval (50 statements)"):
            assert expected in labels, f"{expected!r} was not measured"

    def test_each_result_carries_p50_p95_and_a_ceiling(self, run):
        _p, _m, record = run
        for row in json.loads(record.read_text())["results"]:
            assert row["p50_ms"] > 0
            assert row["p95_ms"] >= row["p50_ms"], "p95 below p50 means the sort broke"
            assert row["ceiling_per_s"] > 0


class TestTheMetricsReport:
    """GitLab's `reports:metrics` consumes OpenMetrics text; malformed lines are dropped silently."""

    def test_every_line_parses_as_a_metric_or_a_comment(self, run):
        _p, metrics, _j = run
        for line in metrics.read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            name, _, value = line.rpartition(" ")
            assert name, f"no metric name in {line!r}"
            float(value)  # raises if GitLab would have dropped this line

    def test_the_srp_backend_is_a_metric_of_its_own(self, run):
        """So a 2x jump can be told apart from a base-image change at a glance.

        Without it, the pure-Python fallback binding after an image bump looks exactly like a
        performance regression, and somebody spends a day on it.
        """
        _p, metrics, _j = run
        assert "auth_srp_backend_is_openssl" in metrics.read_text()

    def test_the_backend_labels_every_latency_metric(self, run):
        _p, metrics, _j = run
        for line in metrics.read_text().splitlines():
            if line.startswith("auth_") and "_p50_ms" in line:
                assert 'backend="' in line, f"unlabelled metric: {line!r}"

    @pytest.mark.parametrize("metric", [
        "auth_srp_begin_login_leg_1_p50_ms",
        "auth_verify_es256_provider_on_p50_ms",
        "auth_policy_eval_50_statements_p50_ms",
    ])
    def test_metric_names_are_pinned(self, run, metric):
        """Renaming a metric starts a new series in GitLab and discards its history.

        These names are therefore an interface, not an implementation detail. If a label must
        change, the old series is gone — so change it deliberately and say so.
        """
        _p, metrics, _j = run
        assert metric in metrics.read_text()
