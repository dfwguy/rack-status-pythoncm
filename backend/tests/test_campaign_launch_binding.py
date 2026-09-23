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
"""A launch must bind to the roster the operator reviewed, not just to its name.

This is the whole point of the campaign version, and the bug it closes is subtle enough to
be worth stating: the id is a label a human chose, so a roster edited under a stable id
passed the old check and ran different hardware than was reviewed — recorded in the
activity trail under the same name. Two node sets, one identifier, no error anywhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
import validation_launch

# Two racks, because `multirack` legitimately refuses a single one -- the scope means
# "spans NVLink domains", and one rack is the `rack` scope.
_RACKS = ["ink50-dh01-h18", "ink50-dh01-h19"]
_RACK_NODES = {
    "ink50-dh01-h18": ["ink50-dh01-h18-01", "ink50-dh01-h18-02"],
    "ink50-dh01-h19": ["ink50-dh01-h19-01", "ink50-dh01-h19-02"],
}
_NODES = [*_RACK_NODES["ink50-dh01-h18"], *_RACK_NODES["ink50-dh01-h19"]]
_VERSION = "abc123def4567890"


@pytest.fixture
def prepared(monkeypatch, tmp_path):
    """A multirack preset launch whose only variable is the campaign the caller claims."""
    monkeypatch.setattr(validation_launch, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(validation_launch, "_script_exists", lambda _path: True)
    monkeypatch.setattr(validation_launch, "_all_slurm_nodenames", lambda: _NODES)
    monkeypatch.setattr(validation_launch, "_normalize_rack_id", lambda rack: rack)
    monkeypatch.setattr(
        validation_launch,
        "_validation_node_eligibility",
        lambda raw: ({node: "idle" for node in raw}, []),
    )
    monkeypatch.setattr(
        validation_launch,
        "_multirack_campaign_snapshot",
        lambda: {
            "ok": True, "active": True, "id": "wave1", "version": _VERSION, "source": "catalog",
            "racks": [
                {"rack": rack, "nodes": nodes, "node_count": len(nodes), "eligible": True,
                 "reason": "", "unavailable_nodes": []}
                for rack, nodes in _RACK_NODES.items()
            ],
        },
    )

    def run(**extra):
        body = {
            "preset_id": "mnubergemm-26.03-v1.17",
            "scope": "multirack",
            "racks": list(_RACKS),
            "storage": "nfs",
            **extra,
        }
        with app_module.app.app_context():
            return validation_launch._prepare_validation_preset(body)

    return run


def _status(err) -> int:
    return err[1]


def _error(err) -> str:
    return err[0].get_json()["error"]


class TestTheVersionIsChecked:
    def test_the_reviewed_version_is_accepted(self, prepared):
        plan, err = prepared(campaign="wave1", campaign_version=_VERSION)
        assert err is None
        assert plan["nodes"] == _NODES

    def test_a_stale_version_is_refused(self, prepared):
        # The case the id-only check could not see: same campaign name, different roster.
        plan, err = prepared(campaign="wave1", campaign_version="0000stale0000000")
        assert plan is None
        assert _status(err) == 409
        assert "has changed since this page loaded" in _error(err)

    def test_the_refusal_names_both_versions_so_it_can_be_compared_by_eye(self, prepared):
        _plan, err = prepared(campaign="wave1", campaign_version="0000stale0000000")
        message = _error(err)
        assert "0000stale0000000" in message
        assert _VERSION in message
        # And the current one is returned as a field, so the UI can re-confirm without
        # parsing prose.
        assert err[0].get_json()["campaign_version"] == _VERSION

    def test_a_wrong_id_is_still_refused(self, prepared):
        _plan, err = prepared(campaign="wave9", campaign_version=_VERSION)
        assert _status(err) == 409
        assert "stale or unknown" in _error(err)


class TestItStaysUsableForCallersThatDoNotSendOne:
    def test_omitting_the_version_is_allowed(self, prepared):
        # Backward compatible on purpose: an older bundle, a script, or `curl` can still
        # launch. The check tightens what a caller *claims*, and refusing a caller that
        # claims nothing would break the API for no security gain -- the roster it runs is
        # the current one either way.
        plan, err = prepared(campaign="wave1")
        assert err is None
        assert plan["nodes"] == _NODES

    def test_omitting_both_is_allowed(self, prepared):
        plan, err = prepared()
        assert err is None
        assert plan["nodes"] == _NODES


class TestTheRosterIsWhatRuns:
    def test_the_nodes_come_from_the_campaign_not_from_inventory(self, prepared, monkeypatch):
        # A campaign exists so that "rack h18" means the same eighteen trays every time. If
        # inventory could supply them, a later refresh would silently change what acceptance
        # covered.
        def must_not_be_called(_rack):
            raise AssertionError("an active campaign must not fall back to live inventory")

        monkeypatch.setattr(
            validation_launch, "_rack_node_hostnames_from_inventory", must_not_be_called
        )
        plan, err = prepared(campaign="wave1", campaign_version=_VERSION)
        assert err is None
        assert plan["nodes"] == _NODES

    def test_a_rack_outside_the_campaign_is_refused(self, prepared):
        _plan, err = prepared(racks=[_RACKS[0], "ink50-dh01-h99"], campaign_version=_VERSION)
        assert _status(err) == 409
        assert "outside exact-node campaign" in _error(err)
