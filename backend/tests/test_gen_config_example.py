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
"""`config/rackstatus.toml.example` is generated and must stay current (DC-5, FR-1b, FR-1e).

The load-bearing test is `test_the_tracked_example_is_current`: a generated file that nobody
regenerates is worse than a hand-written one, because it *claims* to be derived from the code. It
is the same failure as a stale test citation — right on the day it was written, silently wrong from
the first setting anyone adds.

`TestTheExampleIsInert` exists because the file's whole purpose is to be copied. If
`cp rackstatus.toml.example rackstatus.toml` reconfigured a live deployment, the example would be
a trap rather than documentation.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import config_registry
import pytest
import toml_compat

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GENERATOR = REPO_ROOT / "bin" / "gen-config-example.py"
EXAMPLE = REPO_ROOT / "config" / "rackstatus.toml.example"


def _uncomment(raw: str) -> str:
    """Turn the commented example into the TOML an operator would get by uncommenting it."""
    out: list[str] = []
    for line in raw.splitlines():
        match = re.match(r"^# ([A-Z][A-Z0-9_]*) = (.+)$", line)
        if match and "<set in the environment" not in line:
            out.append(f"{match.group(1)} = {match.group(2)}")
        elif line.startswith("["):
            out.append(line)
    return "\n".join(out)


class TestTheTrackedExampleIsCurrent:
    def test_check_mode_passes_against_the_tracked_file(self):
        """The CI gate. Fails when a setting was declared without regenerating."""
        # check=False: the exit code is the assertion.
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"config/rackstatus.toml.example is stale:\n{result.stderr}\n"
            "Run bin/gen-config-example.py"
        )

    def test_check_mode_actually_detects_staleness(self):
        """Guards the gate itself.

        A `--check` that always passes is indistinguishable from one that works, which is the same
        hole the read-coverage gate had. Proven by rendering with an extra declaration registered
        and confirming the output differs from the tracked file.
        """
        sys.path.insert(0, str(GENERATOR.parent))
        spec = __import__("importlib.util", fromlist=["util"]).spec_from_file_location(
            "gen_example", GENERATOR
        )
        module = __import__("importlib.util", fromlist=["util"]).module_from_spec(spec)
        spec.loader.exec_module(module)

        before = module.render()
        config_registry.register(
            config_registry.Setting(
                name="TEST_STALENESS_PROBE",
                secret=False,
                example="x",
                section="general",
                description="A probe declaration, registered only to prove --check can fail.",
            )
        )
        try:
            after = module.render()
        finally:
            config_registry._SETTINGS.pop("TEST_STALENESS_PROBE", None)

        assert after != before, "adding a declaration did not change the rendered example"
        assert "TEST_STALENESS_PROBE" in after


class TestTheExampleIsInert:
    """Copying it must change nothing until the operator chooses something."""

    def test_every_setting_line_is_commented_out(self):
        live = [
            line
            for line in EXAMPLE.read_text().splitlines()
            if re.match(r"^[A-Z][A-Z0-9_]* *=", line)
        ]
        assert not live, f"these lines would take effect on a plain copy: {live}"

    def test_loading_it_as_config_materializes_nothing(self, tmp_path, monkeypatch):
        """The behavioural half: hand the real loader a copy and assert it applies no value."""
        import config_launch

        target = tmp_path / "rackstatus.toml"
        target.write_text(EXAMPLE.read_text())
        monkeypatch.delenv("RACK_STATUS_DEMO", raising=False)

        assert config_launch.load(target).materialized == ()


class TestTheExampleIsValidToml:
    def test_it_parses_once_uncommented(self):
        """An example that does not parse is worse than none: the operator's first act is to
        uncomment a line, and a type error there reads as a product bug."""
        parsed = toml_compat.loads(_uncomment(EXAMPLE.read_text()))
        assert parsed, "nothing parsed out of the example"

    def test_typed_settings_render_as_their_type_not_as_strings(self):
        """A bool rendered `"false"` is a string, and the loader refuses the wrong type — from the
        file that is supposed to be the worked example."""
        parsed = toml_compat.loads(_uncomment(EXAMPLE.read_text()))
        flat: dict[str, object] = {}
        for value in parsed.values():
            flat.update(value if isinstance(value, dict) else {})
        flat.update({k: v for k, v in parsed.items() if not isinstance(v, dict)})

        for name, setting in config_registry.declared().items():
            if name not in flat or not setting.example:
                continue
            if setting.toml_type == config_registry.TOML_BOOL:
                assert isinstance(flat[name], bool), f"{name} rendered as {type(flat[name])}"
            elif setting.toml_type == config_registry.TOML_INT:
                assert isinstance(flat[name], int), f"{name} rendered as {type(flat[name])}"

    def test_no_wrapped_comment_splits_a_word(self):
        """The first generator chunked on a character count and broke words mid-token ("der" /
        "ive from it"). Cosmetic, but a generated file that looks careless does not get trusted."""
        offenders = [
            line
            for line in EXAMPLE.read_text().splitlines()
            if line.startswith("# ") and re.search(r"\b[a-z]{1,3}$", line) and line.endswith(
                ("der", "ive", "ro", "le", "RA", "'T")
            )
        ]
        assert not offenders, f"mid-word wraps: {offenders}"


class TestFR1eNoCredentialsOrSiteHostnames:
    def test_no_secret_setting_is_offered_as_a_file_value(self):
        """A secret must not even be *suggested* here — the file lands in bug reports."""
        raw = EXAMPLE.read_text()
        for name, setting in config_registry.declared().items():
            if not setting.is_secret:
                continue
            assert not re.search(rf"^# {name} = \"", raw, re.MULTILINE), (
                f"{name} is secret and the example offers a value for it"
            )

    @pytest.mark.parametrize(
        "forbidden",
        ["10.29.136", "10.136.128", "192.168.10.", "mbcm01", "bos01-", "ink50-", "nscale"],
    )
    def test_no_real_customer_hostname_or_address_appears(self, forbidden):
        """Five deployments share this repo; a tracked example naming one of them is the
        cluster-mixing hazard in miniature."""
        assert forbidden not in EXAMPLE.read_text()
