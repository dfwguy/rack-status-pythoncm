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
"""The required-setting preflight — DC-4 (FR-16..FR-22, AC-17, AC-20, AC-21a).

`TestTC25ItReportsEveryFaultAtOnce` uses **three simultaneous faults**, and that is the whole
design of the test: with one fault, report-all and fail-on-first produce identical output, so a
single-fault test cannot tell a correct implementation from the one an author would write first.

`TestTC21aTheDemoConjunct` is decision 10's guard at this layer. Without the conjunct the preflight
refuses `./start.sh` on a macOS laptop and every genuinely fresh first boot at all five sites —
worse than the defect the preflight exists to prevent.
"""

from __future__ import annotations

import config_preflight as P
import pytest


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (
        "RACK_STATUS_DEMO",
        "RACK_EXEC_MODE",
        "RACK_WEB_OIDC_ENABLED",
        "RACK_WEB_OIDC_ISSUER",
        "RACK_SSH_HOST",
        "RACK_WEB_TLS_CERT",
        "RACK_WEB_TLS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


class TestTC25ItReportsEveryFaultAtOnce:
    """FR-17 / AC-17. Three faults, because one cannot discriminate."""

    def test_all_three_are_reported_in_one_pass(self, monkeypatch):
        monkeypatch.setenv("RACK_EXEC_MODE", "ssh")
        monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")
        monkeypatch.setenv("RACK_WEB_TLS_CERT", "/tmp/x.crt")

        faults, _ = P.run(exit_on_fault=False)

        assert [f.name for f in faults] == [
            "RACK_SSH_HOST",
            "RACK_WEB_OIDC_ISSUER",
            "RACK_WEB_TLS_KEY",
        ], "fewer than three faults reported, so this cannot distinguish report-all from fail-first"

    def test_the_report_names_every_missing_setting(self, monkeypatch, capsys):
        monkeypatch.setenv("RACK_EXEC_MODE", "ssh")
        monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")

        P.run(exit_on_fault=False)
        err = capsys.readouterr().err

        assert "RACK_SSH_HOST" in err
        assert "RACK_WEB_OIDC_ISSUER" in err

    def test_each_fault_carries_an_actionable_example(self, monkeypatch, capsys):
        """An operator should not have to read the source to learn the shape of a value."""
        monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")
        monkeypatch.setenv("RACK_EXEC_MODE", "local")

        P.run(exit_on_fault=False)
        assert 'RACK_WEB_OIDC_ISSUER = "https://' in capsys.readouterr().err

    def test_it_is_ordered_so_two_runs_read_the_same(self, monkeypatch):
        """Dict order would make the report shuffle between restarts, which is miserable to diff."""
        monkeypatch.setenv("RACK_EXEC_MODE", "ssh")
        monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")

        first, _ = P.run(exit_on_fault=False)
        second, _ = P.run(exit_on_fault=False)
        assert [f.name for f in first] == [f.name for f in second]


class TestItRefusesWithAConfigExitCode:
    def test_a_fault_raises_system_exit_78(self, monkeypatch):
        monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")
        monkeypatch.setenv("RACK_EXEC_MODE", "local")

        with pytest.raises(SystemExit) as caught:
            P.run()
        assert caught.value.code == P.EXIT_CONFIG == 78

    def test_a_clean_launch_does_not_exit(self, monkeypatch):
        monkeypatch.setenv("RACK_STATUS_DEMO", "1")
        assert P.run() == ([], [])


class TestTC21aTheDemoConjunct:
    """Decision 10. The regression guard at this layer."""

    def test_a_demo_laptop_is_not_refused(self, monkeypatch):
        monkeypatch.setenv("RACK_STATUS_DEMO", "1")
        faults, _ = P.run(exit_on_fault=False)
        assert faults == [], (
            "the preflight refuses a demo-mode launch, so ./start.sh on a laptop cannot boot"
        )

    def test_a_real_ssh_deployment_is_still_checked(self, monkeypatch):
        """The other arm. An implementation missing the conjunct passes the first test and fails
        this one; one that drops the row entirely does the reverse — so neither arm alone tells a
        correct predicate from a broken one."""
        monkeypatch.setenv("RACK_EXEC_MODE", "ssh")
        faults, _ = P.run(exit_on_fault=False)
        assert "RACK_SSH_HOST" in [f.name for f in faults]


class TestABlankValueCountsAsMissing:
    def test_whitespace_is_not_a_value(self, monkeypatch):
        """Unlike the loader, which preserves a deliberately-empty variable: a *required* setting
        whose value is spaces is a mistake, and accepting it pushes the failure to first use — the
        exact thing this module prevents."""
        monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")
        monkeypatch.setenv("RACK_EXEC_MODE", "local")
        monkeypatch.setenv("RACK_WEB_OIDC_ISSUER", "   ")

        faults, _ = P.run(exit_on_fault=False)
        assert "RACK_WEB_OIDC_ISSUER" in [f.name for f in faults]

    def test_a_real_value_satisfies_it(self, monkeypatch):
        monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")
        monkeypatch.setenv("RACK_EXEC_MODE", "local")
        monkeypatch.setenv("RACK_WEB_OIDC_ISSUER", "https://x.example.com")

        faults, _ = P.run(exit_on_fault=False)
        assert faults == []


class TestAnUnevaluablePredicateIsReportedNotSwallowed:
    """The third fault class. The registry's first version collapsed this into 'not required',
    which is fail-open on a fail-closed control (`identity-auth-traps.mdc` trap 7)."""

    def test_it_warns_without_refusing(self, monkeypatch):
        import config_registry as R

        def boom() -> bool:
            raise RuntimeError("catalog unavailable")

        setting = R.Setting(name="TEST_UNEVALUABLE", required_when=boom)
        R.register(setting)
        try:
            monkeypatch.setenv("RACK_STATUS_DEMO", "1")
            faults, warnings = P.run(exit_on_fault=False)

            assert faults == [], "an unevaluable predicate refused the boot"
            assert "TEST_UNEVALUABLE" in [w.name for w in warnings], (
                "an unevaluable predicate was silently treated as not required"
            )
            assert "catalog unavailable" in warnings[0].detail
        finally:
            R._SETTINGS.pop("TEST_UNEVALUABLE", None)


class TestTC28ThePreflightIsTheLivePath:
    """FR-20 / AC-20. The mutation half is what distinguishes "the preflight runs" from "the
    preflight exists beside the old guard"."""

    #: Read through the AST, never by substring. The first version of these two tests asserted
    #: `"config_preflight.run()" in source` and compared `str.index` positions — and a mutation
    #: that replaced the call with `pass  # config_preflight.run()` **passed both**, because the
    #: text was still there. That is the second time today a source grep could not tell code from
    #: the comment beside it. A commented-out call is not an AST node, so it cannot survive this.
    @staticmethod
    def _startup_calls() -> list[tuple[int, str]]:
        import ast
        from pathlib import Path

        tree = ast.parse((Path(__file__).resolve().parent.parent / "app.py").read_text())
        found: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                owner = func.value
                if isinstance(owner, ast.Name):
                    found.append((node.lineno, f"{owner.id}.{func.attr}"))
            elif isinstance(func, ast.Name):
                found.append((node.lineno, func.id))
        return sorted(found)

    def _line_of(self, name: str) -> int:
        hits = [lineno for lineno, called in self._startup_calls() if called == name]
        assert hits, f"app.py never calls {name}()"
        return hits[-1]

    def test_app_calls_the_preflight_at_startup(self):
        assert self._line_of("config_preflight.run"), (
            "app.py does not call the preflight, so every required-setting check is dead code"
        )

    def test_it_runs_after_the_catalog_and_before_identity(self):
        """Both bounds. After the catalog because predicates resolve through the cluster overlay
        and the catalog bootstrap creates the default project on a first boot; before identity
        because that is the first consumer of a required setting."""
        catalog = self._line_of("_bootstrap_catalog_at_startup")
        preflight = self._line_of("config_preflight.run")
        identity = self._line_of("_bootstrap_identity_at_startup")

        assert catalog < preflight < identity, (
            f"startup order is catalog@{catalog}, preflight@{preflight}, identity@{identity}"
        )
