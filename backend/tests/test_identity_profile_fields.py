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
"""OIDC profile fields on a user record — `.plc/briefs/oidc-directory-claims-l0.md` R1, R2.

The scope-release half (R3–R5) is exercised in `test_oidc_provider.py` and
`test_oidc_discovery.py`, where the endpoints live.
"""

from __future__ import annotations

import identity.srp as srp_mod
import pytest
from identity import bootstrap, models
from identity.models import (
    MAX_LOCALE,
    MAX_ZONEINFO,
    IdentityError,
    User,
    canonical_locale,
    canonical_zoneinfo,
)
from identity.store import IdentityStore


@pytest.fixture()
def admin_api(monkeypatch, tmp_path):
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
    srp_mod.reset_for_tests()
    s = IdentityStore(str(tmp_path / "identity.json"))
    try:
        bootstrap.ensure_admin(s, emit=False)
    finally:
        s.close()

    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    srp_mod.reset_for_tests()


def _create(client, login: str, **extra):
    body = {"login": login, "email": f"{login}@example.com"}
    body.update(extra)
    return client.post("/api/identity/users", json=body)


class TestR1TheFourFields:
    def test_they_round_trip_through_the_store(self, tmp_path):
        store = IdentityStore(str(tmp_path / "identity.json"))
        try:
            store.create_user(
                User(
                    login="ada",
                    given_name="Ada",
                    family_name="Lovelace",
                    zoneinfo="Europe/London",
                    locale="en-GB",
                )
            )
            back = store.get_user("ada")
            assert (back.given_name, back.family_name) == ("Ada", "Lovelace")
            assert (back.zoneinfo, back.locale) == ("Europe/London", "en-GB")
        finally:
            store.close()

    def test_a_row_predating_them_loads_unchanged(self):
        """R1's no-migration claim, same mechanism as the contact attributes."""
        user = User.from_dict({"login": "legacy", "display_name": "Legacy"})
        assert (user.given_name, user.family_name, user.zoneinfo, user.locale) == ("", "", "", "")

    def test_they_are_all_optional(self):
        assert User(login="nobody").zoneinfo == ""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("en-US", "en-US"),
            ("EN-us", "en-US"),
            ("en", "en"),
            ("zh-hant-tw", "zh-Hant-TW"),
            ("pt-br", "pt-BR"),
            ("es-419", "es-419"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_a_locale_is_stored_in_conventional_casing(self, raw, expected):
        """Tags are case-insensitive per RFC 5646 §2.1.1, so normalising is what stops `EN-us` and
        `en-US` becoming two stored values for one locale."""
        assert canonical_locale(raw) == expected

    @pytest.mark.parametrize("bad", ["english", "e", "en_US", "en-USA-x", "x" * (MAX_LOCALE + 1)])
    def test_a_malformed_locale_is_refused(self, bad):
        with pytest.raises(IdentityError):
            canonical_locale(bad)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("America/New_York", "America/New_York"),
            ("  Europe/London  ", "Europe/London"),
            ("UTC", "UTC"),
            ("America/Argentina/Buenos_Aires", "America/Argentina/Buenos_Aires"),
            ("", ""),
        ],
    )
    def test_a_resolvable_zone_is_accepted(self, raw, expected):
        assert canonical_zoneinfo(raw) == expected

    @pytest.mark.parametrize(
        "bad",
        [
            "Not/AZone",
            "America/New York",
            "x" * (MAX_ZONEINFO + 1),
            "../../etc/passwd",
            "/etc/passwd",
            "America/New_York\x00",
        ],
    )
    def test_an_unusable_zone_is_refused(self, bad):
        """The traversal cases matter beyond tidiness: the value is fed to `ZoneInfo`, which resolves
        keys against filesystem paths."""
        with pytest.raises(IdentityError):
            canonical_zoneinfo(bad)

    def test_the_admin_api_accepts_all_four_on_create(self, admin_api):
        got = _create(
            admin_api,
            "ada",
            given_name="Ada",
            family_name="Lovelace",
            zoneinfo="Europe/London",
            locale="en-GB",
        )
        assert got.status_code == 201, got.get_json()
        listed = next(
            u for u in admin_api.get("/api/identity/users").get_json()["users"] if u["login"] == "ada"
        )
        assert listed["given_name"] == "Ada"
        assert listed["zoneinfo"] == "Europe/London"
        assert listed["locale"] == "en-GB"

    def test_the_admin_api_edits_them(self, admin_api):
        _create(admin_api, "ada")
        got = admin_api.patch(
            "/api/identity/users/ada", json={"locale": "PT-br", "zoneinfo": "America/Sao_Paulo"}
        )
        assert got.status_code == 200, got.get_json()
        assert got.get_json()["user"]["locale"] == "pt-BR"

    def test_a_bad_zone_through_the_api_is_a_400_naming_the_field(self, admin_api):
        _create(admin_api, "ada")
        got = admin_api.patch("/api/identity/users/ada", json={"zoneinfo": "Mars/Olympus_Mons"})
        assert got.status_code == 400
        assert "zoneinfo" in got.get_json()["error"]


class TestR2ZoneValidationDegradesRatherThanRefusing:
    """The environment-dependence is the point: the zone set comes from the runtime, and measured
    2026-09-04 macOS carried 113 legacy aliases that `python:3.12-slim` does not."""

    def test_a_missing_database_falls_back_to_a_shape_check(self, monkeypatch):
        """Fail-soft on purpose. A display preference that cannot be saved because the base image
        lacks a package is a worse outcome than an unverified zone string, and this value grants
        nothing — which is exactly why the fail-closed rule for security controls does not apply.
        """
        import zoneinfo as zi

        def no_database(key, *a, **k):
            raise zi.ZoneInfoNotFoundError(key)

        # Asserted on a zone the real database does NOT hold, so this can only pass when the
        # fallback actually ran. Using a real zone here would pass identically with the patch
        # removed, which is a test that proves nothing.
        assert models.canonical_zoneinfo("Europe/London") == "Europe/London"
        with pytest.raises(IdentityError, match="not in this system"):
            models.canonical_zoneinfo("Nowhere/Fictional")

        monkeypatch.setattr(zi, "ZoneInfo", no_database)

        assert models.canonical_zoneinfo("Nowhere/Fictional") == "Nowhere/Fictional"
        # Still refused on shape, because that check needs no database at all.
        with pytest.raises(IdentityError, match="invalid zoneinfo"):
            models.canonical_zoneinfo("../../etc/passwd")

    def test_a_present_database_still_rejects_an_unknown_zone(self):
        """The complement of the above: degradation must not become blanket acceptance."""
        with pytest.raises(IdentityError, match="not in this system"):
            canonical_zoneinfo("Nowhere/Fictional")
