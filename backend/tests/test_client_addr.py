# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""TC-19 and TC-20 of the Phase 1 test plan: source-address provenance.

AC-11 is the spoofing case and the one that must not regress: an operator must not be able to
attribute their actions to an address of their choosing. AC-34 closes the single misconfiguration
that would defeat it (threat T-54).

Traces to AC-11, AC-12 (FR-8) and AC-34 (NFR-14).
"""

from __future__ import annotations

import logging

import core
import logging_config as lc
import pytest
from api import _common


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("RACK_WEB_TRUSTED_PROXIES", raising=False)
    _common.reset_trusted_cache_for_tests()
    yield
    _common.reset_trusted_cache_for_tests()


# --------------------------------------------------------------------------- TC-19


class TestTC19Resolution:
    """The peer is the truth unless a configured proxy vouches otherwise (AC-11, AC-12)."""

    def test_untrusted_forwarded_header_is_not_believed(self):
        """AC-11. The spoofing case: a claimed address must not become the recorded one."""
        ip, trust = core.resolve_client_addr("127.0.0.1", "1.2.3.4", ())
        assert ip == "127.0.0.1"
        assert trust == core.TRUST_CLAIMED_UNTRUSTED
        assert ip != "1.2.3.4", "the claim must not be recorded as the source"

    def test_trusted_proxy_chain_resolves_right_to_left(self):
        """AC-12. Each proxy appends what it saw, so the client is the rightmost untrusted hop."""
        trusted, _ = core.parse_trusted_proxies("10.0.0.0/24")
        ip, trust = core.resolve_client_addr("10.0.0.5", "203.0.113.9, 10.0.0.5", trusted)
        assert (ip, trust) == ("203.0.113.9", core.TRUST_PROXY)

    def test_no_header_is_simply_the_peer(self):
        assert core.resolve_client_addr("203.0.113.7", None, ()) == (
            "203.0.113.7", core.TRUST_PEER
        )

    def test_a_forged_hop_left_of_the_real_client_is_ignored(self):
        """Right-to-left is what makes prepending junk useless.

        A client can put anything at the front of the chain; only what a trusted proxy *appended*
        carries weight, so resolution walks from the right.
        """
        trusted, _ = core.parse_trusted_proxies("10.0.0.0/24")
        ip, trust = core.resolve_client_addr(
            "10.0.0.5", "9.9.9.9, 203.0.113.9, 10.0.0.5", trusted
        )
        assert (ip, trust) == ("203.0.113.9", core.TRUST_PROXY)

    def test_chained_trusted_proxies_are_walked_past(self):
        trusted, _ = core.parse_trusted_proxies("10.0.0.0/24,10.0.1.0/24")
        ip, trust = core.resolve_client_addr(
            "10.0.0.5", "203.0.113.9, 10.0.1.7, 10.0.0.5", trusted
        )
        assert (ip, trust) == ("203.0.113.9", core.TRUST_PROXY)

    def test_an_all_proxy_chain_still_labels_honestly(self):
        trusted, _ = core.parse_trusted_proxies("10.0.0.0/24")
        ip, trust = core.resolve_client_addr("10.0.0.5", "10.0.0.6, 10.0.0.5", trusted)
        assert trust == core.TRUST_PROXY
        assert ip == "10.0.0.6"

    def test_a_trusted_peer_with_no_header_is_still_just_the_peer(self):
        trusted, _ = core.parse_trusted_proxies("10.0.0.0/24")
        assert core.resolve_client_addr("10.0.0.5", "", trusted) == (
            "10.0.0.5", core.TRUST_PEER
        )

    def test_garbage_in_the_chain_does_not_become_the_source(self):
        trusted, _ = core.parse_trusted_proxies("10.0.0.0/24")
        ip, trust = core.resolve_client_addr("10.0.0.5", "not-an-ip, 10.0.0.5", trusted)
        assert trust == core.TRUST_PROXY
        assert ip == "not-an-ip", "recorded verbatim, but only because a trusted proxy passed it"


# --------------------------------------------------------------------------- TC-20


class TestTC20OverbroadProxyRefused:
    """AC-34 / NFR-14: a wide entry would make every forged header authoritative."""

    @pytest.mark.parametrize(
        ("entry", "accepted"),
        [
            ("0.0.0.0/0", False),
            ("10.0.0.0/8", False),
            ("10.0.0.0/16", False),
            ("10.0.0.0/24", True),
            ("10.0.0.5", True),          # a bare host is /32 -- more specific, so fine
            ("::/0", False),
            ("2001:db8::/32", False),
            ("2001:db8::/64", True),
        ],
    )
    def test_prefix_floor(self, entry, accepted):
        ok, refusals = core.parse_trusted_proxies(entry)
        assert bool(ok) is accepted, f"{entry} acceptance"
        assert bool(refusals) is not accepted

    def test_a_refused_entry_does_not_take_effect(self):
        """The property that matters: refusal is not just a log line."""
        trusted, refusals = core.parse_trusted_proxies("0.0.0.0/0")
        assert refusals and trusted == ()
        ip, trust = core.resolve_client_addr("198.51.100.9", "1.2.3.4", trusted)
        assert (ip, trust) == ("198.51.100.9", core.TRUST_CLAIMED_UNTRUSTED)

    def test_valid_entries_survive_alongside_refused_ones(self):
        ok, refusals = core.parse_trusted_proxies("0.0.0.0/0, 10.0.0.0/24, nonsense")
        assert [str(n) for n in ok] == ["10.0.0.0/24"]
        assert len(refusals) == 2

    def test_refusal_is_reported_once_at_startup(self, monkeypatch, caplog):
        monkeypatch.setenv("RACK_WEB_TRUSTED_PROXIES", "0.0.0.0/0")
        _common.reset_trusted_cache_for_tests()
        with caplog.at_level(logging.WARNING):
            assert _common.trusted_networks() == ()
            _common.trusted_networks()  # cached; must not report twice
        warnings = [r for r in caplog.records if "refusing" in r.getMessage()]
        assert len(warnings) == 1
        assert "broader than /24" in warnings[0].getMessage()

    def test_setting_is_registered_so_defaults_and_docs_are_enforced(self):
        assert "RACK_WEB_TRUSTED_PROXIES" in lc.SETTINGS
        assert lc.SETTINGS["RACK_WEB_TRUSTED_PROXIES"].default == ""


# --------------------------------------------------------------------------- the Flask half


class TestFlaskWrapper:
    """The wrapper is thin, but the default it carries is the security property."""

    def test_default_believes_no_forwarded_header(self, monkeypatch):
        import app as app_module

        with app_module.app.test_request_context(
            "/api/status", headers={"X-Forwarded-For": "1.2.3.4"}, environ_base={
                "REMOTE_ADDR": "127.0.0.1"}
        ):
            assert _common.client_addr() == ("127.0.0.1", core.TRUST_CLAIMED_UNTRUSTED)

    def test_configured_proxy_is_believed(self, monkeypatch):
        import app as app_module

        monkeypatch.setenv("RACK_WEB_TRUSTED_PROXIES", "10.0.0.0/24")
        _common.reset_trusted_cache_for_tests()
        with app_module.app.test_request_context(
            "/api/status", headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.5"},
            environ_base={"REMOTE_ADDR": "10.0.0.5"},
        ):
            assert _common.client_addr() == ("203.0.113.9", core.TRUST_PROXY)
