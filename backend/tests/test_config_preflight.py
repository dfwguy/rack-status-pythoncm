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
import config_registry as R
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
    """FR-17 / AC-17: every fault in one pass, not the first one found.

    **Provoked with settings this test registers itself, and that is the point.** It used to rely on
    three product settings — `RACK_SSH_HOST`, `RACK_WEB_OIDC_ISSUER`, `RACK_WEB_TLS_KEY` — and each
    turned out to be a poor instrument. The issuer stopped being requirable when the OIDC flag was
    retired (it is derived now). And `RACK_SSH_HOST` is decided by
    `_ssh_control_connection_in_use`, which resolves the *active cluster's* connection: that is
    correct behaviour, deliberately chosen so a cluster supplying the host through its overlay is not
    refused, but it makes the fault depend on the developer's `var/store/catalog.json` and on a
    context cached at `import app`. Running the app locally was enough to make the fault vanish and
    this test fail for a reason that had nothing to do with report-all.

    Two purpose-made settings are unconditionally required and depend on nothing, so the assertion
    is about the mechanism and nothing else.
    """

    @pytest.fixture(autouse=True)
    def _two_faults(self, monkeypatch):
        always = R.Setting(
            name="NVIS_TEST_FAULT_ONE", secret=False, required_when=lambda: True,
            example="one", section="general", description="Test fixture; never read.",
        )
        also = R.Setting(
            name="NVIS_TEST_FAULT_TWO", secret=False, required_when=lambda: True,
            example="two", section="general", description="Test fixture; never read.",
        )
        for setting in (always, also):
            monkeypatch.delenv(setting.name, raising=False)
            R.register(setting)
        yield
        # The registry is module state; leaving these behind would make every later preflight test
        # report two extra faults.
        for setting in (always, also):
            R.unregister(setting.name)

    def test_both_are_reported_in_one_pass(self):
        faults, _ = P.run(exit_on_fault=False)
        names = [f.name for f in faults]
        assert "NVIS_TEST_FAULT_ONE" in names
        assert "NVIS_TEST_FAULT_TWO" in names, (
            "only the first fault was reported, so this refuses one setting at a time"
        )

    def test_the_report_names_every_missing_setting(self, capsys):
        P.run(exit_on_fault=False)
        err = capsys.readouterr().err
        assert "NVIS_TEST_FAULT_ONE" in err
        assert "NVIS_TEST_FAULT_TWO" in err

    def test_each_fault_carries_an_actionable_example(self, capsys):
        """An operator should not have to read the source to learn the shape of a value."""
        P.run(exit_on_fault=False)
        err = capsys.readouterr().err
        assert 'NVIS_TEST_FAULT_ONE = "one"' in err


class TestItRefusesWithAConfigExitCode:
    """78 is `EX_CONFIG`, so a deploy script can tell "misconfigured" from "crashed"."""

    NAME = "NVIS_TEST_EXIT"

    @pytest.fixture(autouse=True)
    def _required_setting(self, monkeypatch):
        setting = R.Setting(
            name=self.NAME, secret=False, required_when=lambda: True,
            example="a-value", section="general", description="Test fixture; never read.",
        )
        monkeypatch.delenv(self.NAME, raising=False)
        R.register(setting)
        yield
        R.unregister(self.NAME)

    def test_a_fault_raises_system_exit_78(self):
        with pytest.raises(SystemExit) as caught:
            P.run()
        assert caught.value.code == P.EXIT_CONFIG == 78

    def test_a_clean_configuration_does_not_raise(self, monkeypatch, catalog_from_fixture):
        """Passes alone and failed in a full run, which is the signature of inherited state.

        `P.run()` reports *every* fault, so this test only holds if nothing else in the session has
        left a required setting unsatisfied — the active cluster's connection included. Fixturing
        the catalog and demo mode makes it independent of run order.
        """
        catalog_from_fixture("catalog-ssh-without-host.json")
        monkeypatch.setenv("RACK_STATUS_DEMO", "1")  # so no SSH host is demanded
        monkeypatch.setenv(self.NAME, "a-real-value")
        P.run()


class TestTC21aTheDemoConjunct:
    """Decision 10. The regression guard at this layer."""

    def test_a_demo_laptop_is_not_refused(self, monkeypatch):
        monkeypatch.setenv("RACK_STATUS_DEMO", "1")
        faults, _ = P.run(exit_on_fault=False)
        assert faults == [], (
            "the preflight refuses a demo-mode launch, so ./start.sh on a laptop cannot boot"
        )

    def test_a_real_ssh_deployment_is_still_checked(self, monkeypatch, catalog_from_fixture):
        """The other arm, driven from a committed catalog rather than the developer's.

        The predicate resolves the **active cluster's** connection — deliberately, so a cluster
        supplying `RACK_SSH_HOST` through its overlay is not refused. That made this test read
        `var/store/catalog.json`: once the app had been run locally, the active cluster carried a
        real `ssh_host` and the fault vanished on that machine only.

        `catalog-ssh-without-host.json` declares `exec_mode: ssh` with an empty host, which is the
        state that makes the setting genuinely required, and it depends on nothing outside the repo.
        """
        catalog_from_fixture("catalog-ssh-without-host.json")
        monkeypatch.setenv("RACK_STATUS_DEMO", "0")
        # Set as well as fixtured, and both are needed. The fixture removes the dependence on the
        # developer's `var/store/catalog.json`; the variable is needed because `catalog/seed.py`
        # builds a default project from the current environment and makes it ACTIVE, which replaces
        # the fixture's own active pointer. So the fixture guarantees isolation and the variable
        # decides the resolved mode.
        monkeypatch.setenv("RACK_EXEC_MODE", "ssh")
        monkeypatch.delenv("RACK_SSH_HOST", raising=False)

        # The predicate, not the whole preflight. `import app` (done by conftest) materialises the
        # session catalog's connection into `os.environ` and saves a baseline, so a preflight run
        # later in the process resolves against that rather than against the fixture. Asserting the
        # predicate keeps the subject — the conjunct — and drops the import-order dependence.
        assert R._ssh_control_connection_in_use() is True, (
            "an ssh cluster with no host must require RACK_SSH_HOST"
        )

    def test_demo_mode_is_the_conjunct(self, monkeypatch, catalog_from_fixture):
        """The same cluster with demo mode ON must NOT be refused.

        Both arms matter and neither alone discriminates: an implementation missing the conjunct
        passes the SSH arm and fails this one; one that drops the row entirely does the reverse.
        """
        catalog_from_fixture("catalog-ssh-without-host.json")
        monkeypatch.setenv("RACK_STATUS_DEMO", "1")
        monkeypatch.setenv("RACK_EXEC_MODE", "ssh")
        monkeypatch.delenv("RACK_SSH_HOST", raising=False)

        assert R._ssh_control_connection_in_use() is False, (
            "demo mode must not require an SSH host, or ./start.sh on a laptop is refused"
        )


class TestABlankValueCountsAsMissing:
    """Driven through a setting this test declares, for the same reason as TC-25 above: the issuer
    it used to use is no longer conditionally required, so those assertions had quietly stopped
    testing anything."""

    NAME = "NVIS_TEST_BLANK"

    @pytest.fixture(autouse=True)
    def _required_setting(self, monkeypatch):
        setting = R.Setting(
            name=self.NAME, secret=False, required_when=lambda: True,
            example="a-value", section="general", description="Test fixture; never read.",
        )
        monkeypatch.delenv(self.NAME, raising=False)
        R.register(setting)
        yield
        R.unregister(self.NAME)

    def test_whitespace_is_not_a_value(self, monkeypatch):
        """Unlike the loader, which preserves a deliberately-empty variable: a *required* setting
        whose value is spaces is a mistake, and accepting it pushes the failure to first use — the
        exact thing this module prevents."""
        monkeypatch.setenv(self.NAME, "   ")

        faults, _ = P.run(exit_on_fault=False)
        assert self.NAME in [f.name for f in faults]

    def test_a_real_value_satisfies_it(self, monkeypatch):
        monkeypatch.setenv(self.NAME, "a-real-value")

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
