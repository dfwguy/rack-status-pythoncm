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
"""The settings registry — DC-2 (FR-7, FR-8, FR-16, FR-21, FR-26, FR-26a, NFR-3; TC-9, TC-24).

Two groups matter more than the rest.

`TestTC9SecrecyResolvesInThreeDirections` covers the **false positive**, and that direction is the
one to keep. `RACK_PROJECT_TOKEN` matches `SENSITIVE_NAME_PATTERN` on `TOKEN` but is an NVFTP
folder name. Under NFR-3 a false positive is not an over-redaction — it is a *refusal to boot on a
legitimate setting*. An implementation that marks everything secret passes every criterion written
from the true-positive side, which is why the middle case is asserted explicitly.

`TestTheRequiredSetDoesNotRefuseAFreshDeployment` is the regression guard for decision 10. The
predicate as first designed had no demo conjunct, and `./start.sh` on macOS is demo mode — so it
would have refused every developer laptop and every genuinely-fresh first boot at all five sites.
The test plan caught that in review; this keeps it caught.
"""

from __future__ import annotations

from typing import ClassVar

import config_registry as R
import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """The predicates read the live environment, so a leaked variable from another test would
    change what "required" means here."""
    for name in ("RACK_STATUS_DEMO", "RACK_EXEC_MODE", "RACK_WEB_OIDC_ENABLED"):
        monkeypatch.delenv(name, raising=False)


class TestTC9SecrecyResolvesInThreeDirections:
    """FR-26a. Explicit where declared, pattern otherwise."""

    def test_a_true_positive_is_secret(self):
        assert R.is_secret("RACK_SUDO_PASSWORD")
        assert R.is_secret("NGC_API_KEY")

    def test_the_false_positive_is_overridden_to_not_secret(self):
        """The case the tri-state exists for. Pattern alone returns True here — verified before
        the declaration was added — and under NFR-3 that is a boot refusal, not a redaction."""
        assert R.is_secret("RACK_PROJECT_TOKEN") is False, (
            "RACK_PROJECT_TOKEN resolved as secret; it is an NVFTP folder name, and NFR-3 turns "
            "that false positive into a refusal to start on a legitimate setting"
        )

    def test_the_pattern_still_applies_to_undeclared_names(self):
        """An undeclared name must still be redacted. Refusing to redact something merely because
        nobody declared it would leak exactly the value a typo'd secret key holds."""
        assert R.is_secret("SOME_UNDECLARED_PASSWORD")
        assert R.is_secret("WHATEVER_CREDENTIAL")

    def test_a_path_shaped_key_is_not_secret(self):
        """`SENSITIVE_NAME_PATTERN` deliberately omits a bare `KEY`, because these hold paths and
        redacting a path protects nothing while making logs unreadable."""
        assert R.is_secret("RACK_SSH_KEY") is False
        assert R.is_secret("RACK_WEB_TLS_KEY") is False

    def test_an_explicit_true_beats_a_non_matching_name(self):
        s = R.Setting(name="HARMLESS_LOOKING_NAME", secret=True)
        assert s.is_secret is True


class TestTheRequiredSetDoesNotRefuseAFreshDeployment:
    """Decision 10 — the regression guard.

    `DEFAULT_EXEC_MODE = "ssh"` (`transport/factory.py:36`) means the SSH predicate fires by
    default, and `catalog/seed.py:577` stores an empty `ssh_host` when the variable is unset. So
    without the demo conjunct the preflight refuses a laptop and every first boot.
    """

    def test_demo_mode_requires_no_connection_settings(self, monkeypatch):
        monkeypatch.setenv("RACK_STATUS_DEMO", "1")
        assert [s.name for s in R.required_now()] == [], (
            "demo mode requires connection settings, so ./start.sh on a laptop would be refused"
        )

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "TRUE", "On"])
    def test_every_demo_spelling_is_honoured(self, monkeypatch, truthy):
        """The spellings operators actually write, per `core.truthy_env`. Honouring only `"1"`
        would refuse a laptop configured with `true`."""
        monkeypatch.setenv("RACK_STATUS_DEMO", truthy)
        assert R.required_now() == ()

    def test_local_exec_mode_requires_no_connection_settings(self, monkeypatch):
        monkeypatch.setenv("RACK_EXEC_MODE", "local")
        assert R.required_now() == ()

    def test_a_real_ssh_deployment_does_require_the_host(self, monkeypatch, catalog_from_fixture):
        """The other half — the predicate must still fire where it matters, or the feature is
        decoration.

        This asserted `RACK_REMOTE_BASE_PATH` too, and in doing so **pinned a defect**: that
        setting has a working default (`paths.py:37-43`) and a topology override (`paths.py:56`),
        so requiring it would refuse a legitimately-configured deployment. The one test touching
        required-set membership was asserting the divergence rather than reporting it, which is
        why the parity test below now exists.
        """
        catalog_from_fixture("catalog-ssh-without-host.json")
        monkeypatch.setenv("RACK_STATUS_DEMO", "0")
        monkeypatch.setenv("RACK_EXEC_MODE", "ssh")
        monkeypatch.delenv("RACK_SSH_HOST", raising=False)

        names = {s.name for s in R.required_now()}
        assert "RACK_SSH_HOST" in names
        assert "RACK_REMOTE_BASE_PATH" not in names, (
            "a setting with a working default is in the required set"
        )

    def test_ssh_is_the_default_when_nothing_is_set(self, monkeypatch, catalog_from_fixture):
        """`RACK_EXEC_MODE` is deprecated and SSH is the default, so an unset value must NOT read
        as 'no SSH' — that would silently drop the check on every deployment.

        The catalog is fixtured for the same reason as its neighbour: the predicate resolves the
        active cluster's connection, so without isolation this read the developer's own store and
        the answer depended on whether the app had been run locally.
        """
        catalog_from_fixture("catalog-ssh-without-host.json")
        monkeypatch.setenv("RACK_STATUS_DEMO", "0")
        monkeypatch.delenv("RACK_EXEC_MODE", raising=False)
        monkeypatch.delenv("RACK_SSH_HOST", raising=False)

        names = {s.name for s in R.required_now()}
        assert "RACK_SSH_HOST" in names


# `TestTheOidcIssuerPredicate` removed 2026-09-08: its subject was
# `_oidc_provider_enabled`, which asked whether the OIDC provider was on — a question with only one
# answer now. The issuer is no longer conditionally required; it is derived when unset.


class TestAPredicateThatRaisesIsReportedNotSwallowed:
    def test_it_is_undetermined_rather_than_not_required(self):
        """The first version answered `False` here — fail-open on a fail-closed control, which
        `identity-auth-traps.mdc` trap 7 names exactly. "Cannot tell" is now its own state, so DC-4
        can report it in the report-all diagnostic instead of booting a deployment that may be
        missing a required setting."""

        def boom() -> bool:
            raise RuntimeError("catalog unavailable")

        result = R.Setting(name="X", required_when=boom).requirement()
        assert result.undetermined, "a failing predicate resolved to a definite answer"
        assert result.required is None
        assert "catalog unavailable" in (result.error or ""), (
            "the predicate's error text is lost, so DC-4 has nothing to report"
        )


class TestTC24Declarations:
    def test_a_duplicate_differing_declaration_is_refused(self):
        """Last-writer-wins would make the registry depend on import order, and the symptom — one
        of two declarations winning at random — reproduces on one machine and not another."""
        R.register(R.Setting(name="TEST_DUP", toml_type=R.TOML_STR))
        try:
            with pytest.raises(R.RegistryError, match="already declared"):
                R.register(R.Setting(name="TEST_DUP", toml_type=R.TOML_INT))
        finally:
            R.declared().pop("TEST_DUP", None)
            R._SETTINGS.pop("TEST_DUP", None)

    def test_re_registering_an_identical_declaration_is_fine(self):
        s = R.Setting(name="TEST_IDEMPOTENT", toml_type=R.TOML_BOOL)
        R.register(s)
        try:
            R.register(R.Setting(name="TEST_IDEMPOTENT", toml_type=R.TOML_BOOL))
        finally:
            R._SETTINGS.pop("TEST_IDEMPOTENT", None)

    def test_an_unknown_toml_type_is_refused(self):
        with pytest.raises(R.RegistryError, match="toml_type"):
            R.Setting(name="X", toml_type="yaml-ish")

    def test_a_nameless_setting_is_refused(self):
        with pytest.raises(R.RegistryError, match="needs a name"):
            R.Setting(name="   ")

    def test_declared_returns_a_copy(self):
        """A caller mutating the registry by accident would change what the preflight requires."""
        R.declared()["INJECTED"] = R.Setting(name="INJECTED")
        assert R.get("INJECTED") is None

    def test_every_declaration_carries_what_the_example_generator_needs(self):
        """DC-5 renders `rackstatus.toml.example` from this registry, so a declaration with no
        description or example produces a file that documents nothing (FR-1b)."""
        thin = [
            s.name for s in R.declared().values() if not s.description.strip()
        ]
        assert not thin, f"these settings have no description for the example file: {thin}"


class TestItDoesNotDragIdentityIntoThePreLoadPhase:
    """DD-1. DC-3 loads before the first first-party import and this module is on that path, so a
    module-level `identity` import here would pull `identity/` into the pre-load phase."""

    def test_identity_is_not_imported_at_module_level(self):
        import ast
        from pathlib import Path

        tree = ast.parse((Path(R.__file__)).read_text())
        offenders: list[str] = []
        for node in tree.body:  # module level only
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            offenders += [n for n in names if n.split(".")[0] in ("identity", "config_env", "app")]

        assert not offenders, f"module-level imports that belong after the loader: {offenders}"


class TestTC10TheRequiredSetMatchesTheReviewedTable:
    """The parity control, and the reason it exists is that its absence already cost something.

    FR-21 says the preflight evaluates the predicates in the brief's *Initial required-setting set*
    table, and "a row without file:line evidence is not in the set". Without a parity check the
    registry drifted immediately: `RACK_REMOTE_BASE_PATH` was registered as required despite having
    a working default, and the only test touching membership asserted that drift rather than
    catching it.

    The table is transcribed here deliberately rather than derived from the registry. Deriving it
    would make the assertion vacuous — the registry would define its own correctness — which is the
    same trap as a coverage gate whose only tests assert "no findings".
    """

    #: The brief's *Initial required-setting set*, by name. Adding a row here without adding it to
    #: the brief (with file:line evidence for why its absence is undiagnosable) is the drift this
    #: guards; so is the reverse.
    #: `RACK_WEB_OIDC_ISSUER` left this set on 2026-09-08. It was conditionally required while the
    #: OIDC provider was a flag; the provider is unconditional now and the issuer is *derived* from
    #: the bind address when unset, so it carries no predicate and cannot refuse a launch.
    REVIEWED: ClassVar[set[str]] = {"RACK_SSH_HOST", "RACK_WEB_TLS_KEY"}

    def test_every_predicate_carrying_setting_is_in_the_reviewed_table(self):
        carrying = {s.name for s in R.declared().values() if s.required_when is not None}
        unreviewed = sorted(carrying - self.REVIEWED)
        assert not unreviewed, (
            "these settings can refuse a startup but appear in no reviewed table row:\n  "
            + "\n  ".join(unreviewed)
            + "\n\nEither drop the predicate or add a row to the brief's Initial required-setting "
            "set with file:line evidence that its absence is undiagnosable (FR-21)."
        )

    def test_every_reviewed_row_is_actually_registered(self):
        """The other direction. `RACK_WEB_TLS_KEY` was in the table and registered nowhere, so a
        cert with no key would have passed the preflight it was written for."""
        carrying = {s.name for s in R.declared().values() if s.required_when is not None}
        missing = sorted(self.REVIEWED - carrying)
        assert not missing, f"reviewed required settings with no predicate registered: {missing}"


class TestFloatsAreRefusedRatherThanRendered:
    """Both float-shaped settings in this tree are version strings, where rendering is unsafe.

    `bin/scripts/nccl_allreduce.sbatch:114` compares the CUDA version in awk by splitting on `.`,
    and `tls_server.py:60-66` matches the TLS version as a string and silently keeps the 1.2 floor
    on an unrecognised value. `repr()` survives both, but `13.30` still renormalises to `"13.3"` —
    a different value than the operator typed. Refusing costs nothing, since nothing declares one.
    """

    def test_float_is_not_an_accepted_toml_type(self):
        assert "float" not in R.TOML_TYPES

    def test_declaring_a_float_setting_is_refused(self):
        with pytest.raises(R.RegistryError, match="toml_type"):
            R.Setting(name="SOME_VERSION", toml_type="float")
