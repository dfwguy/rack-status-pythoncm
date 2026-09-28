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
"""`.env.example` must offer exactly the keys Compose interpolates — no more, no fewer.

Written after finding that 8 of its 11 keys were wrong. Compose reads `.env` *only* to expand
`${...}` inside `docker-compose.yml`, so a key it does not interpolate does nothing there and fails
silently:

* six keys were inert (`RACK_EXEC_MODE`, `RACK_PLATFORM`, `RACK_SSH_USER`, `RACK_SSH_PORT`,
  `RACK_LOG_DIR`, `RACK_STATUS_DEMO`) — an operator setting `RACK_STATUS_DEMO=1` there got no demo
  mode and no error;
* two were **deleted variables** (`RACK_WEB_AUTH_MODE`, `RACK_WEB_AUTH_TOKEN`), removed with the
  shared-token and PAM paths, so the file configured authentication that no longer exists;
* three of the six real keys were **missing**, including `RACK_WEB_BIND_ADDR` — the one whose
  absence made the dashboard answer plaintext HTTP to the whole management segment on the Nscale TX
  OVA, per `docker-compose.yml`'s own comment.

Derived from `docker-compose.yml` rather than transcribed, because that file is the thing that
decides which keys matter. A hand-kept list here would drift exactly the way the file it guards
did.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: `bin/check-tracked-tree.sh` exports tracked files but **strips dotfiles**, so `.env.example` is
#: absent there and all six cases below failed on a file that was simply not present. That is a
#: false alarm from the tool whose whole job is to catch local/CI divergence — and six standing
#: failures teach everyone to stop reading its output, which is worse than not having it. A real
#: runner does a full clone and always has the file, so skipping is right in the one tree that
#: does not. (Same treatment in `test_ci_job_tooling.py`, for `.gitlab-ci.yml`.)
pytestmark = pytest.mark.skipif(
    not ENV_EXAMPLE.is_file(), reason=f"{ENV_EXAMPLE.name} absent (dotfile-stripped tree)"
)


def interpolated_by_compose() -> set[str]:
    """Every `${VAR}` in docker-compose.yml — the only keys `.env` can affect."""
    return set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", COMPOSE.read_text()))


def offered_by_example() -> set[str]:
    """Assignments in `.env.example`, ignoring comments."""
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        match = re.match(r"^([A-Z_][A-Z0-9_]*)=", line.strip())
        if match:
            keys.add(match.group(1))
    return keys


class TestItOffersExactlyWhatComposeUses:
    def test_no_inert_key_is_offered(self):
        """A key Compose does not interpolate does nothing in `.env`, and does it silently."""
        inert = sorted(offered_by_example() - interpolated_by_compose())
        assert not inert, (
            "these keys do nothing in .env because docker-compose.yml never interpolates them — "
            "they belong in config/rackstatus.toml:\n  " + "\n  ".join(inert)
        )

    def test_every_interpolated_key_is_offered(self):
        """The other direction, and the one that hid `RACK_WEB_BIND_ADDR`: a key Compose needs but
        the example never mentions gets whatever default the compose file falls back to, which for
        the bind address was `0.0.0.0`."""
        missing = sorted(interpolated_by_compose() - offered_by_example())
        assert not missing, (
            "docker-compose.yml interpolates these and .env.example does not offer them:\n  "
            + "\n  ".join(missing)
        )


class TestItDoesNotAdvertiseRemovedVariables:
    def test_the_deleted_auth_variables_are_gone(self):
        """`RACK_WEB_AUTH_MODE` and `RACK_WEB_AUTH_TOKEN` went with the shared-token and PAM login
        paths. The only mention left in backend code is a comment saying so
        (`web_auth.py:126`), so anyone following the old example configured nothing."""
        raw = ENV_EXAMPLE.read_text()
        for dead in ("RACK_WEB_AUTH_MODE=", "RACK_WEB_AUTH_TOKEN=", "RACK_WEB_PAM_SERVICE="):
            assert dead not in raw, f"{dead} is offered but the variable no longer exists"

    def test_no_backend_module_reads_them(self):
        """Guards the premise. If one of these came back, the test above would be wrong to refuse
        it — so assert the deletion at the source rather than trusting my reading of it."""
        backend = REPO_ROOT / "backend"
        offenders: list[str] = []
        for source in backend.rglob("*.py"):
            if "tests" in source.parts:
                continue
            for line_no, line in enumerate(source.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue  # the comment recording the removal is not a read
                if "RACK_WEB_AUTH_MODE" in line or "RACK_WEB_AUTH_TOKEN" in line:
                    offenders.append(f"{source.relative_to(REPO_ROOT)}:{line_no}")

        assert not offenders, f"these variables are still read: {offenders}"


class TestItNamesTheProductCorrectly:
    def test_it_says_rack_status(self):
        """The Greenlight name was dropped; a copied `.env` should not reintroduce it."""
        raw = ENV_EXAMPLE.read_text()
        assert "NVIS Rack Status" in raw
        assert "Greenlight" not in raw


class TestItPointsAtTheConfigFile:
    """Two files with two jobs. Undocumented, the split is how six inert keys once survived in the
    file an operator copies first."""

    def test_it_explains_where_other_settings_go(self):
        raw = ENV_EXAMPLE.read_text()
        assert "config/rackstatus.toml" in raw
        assert "init-config" in raw

    def test_it_does_not_point_at_the_removed_env_file(self):
        """`rack-config.env` was folded into `config/rackstatus.toml`; naming it sends an operator
        to a file that no longer exists."""
        assert "rack-config.env" not in ENV_EXAMPLE.read_text()

    def test_it_holds_no_key_material(self):
        """It offers a key *path*; the key itself must never be here."""
        raw = ENV_EXAMPLE.read_text()
        assert "BEGIN OPENSSH PRIVATE KEY" not in raw
        assert "BEGIN RSA PRIVATE KEY" not in raw
