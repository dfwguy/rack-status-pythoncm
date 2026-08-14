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
"""A multirack log's rack count must come from what the name DECLARES, not from counting ids.

run-nemo-xrack.sh compacts the rack tag once the full list would overflow the 255-byte filename
limit, keeping only the endpoints plus a count and a digest:

    root-NEMO-ink50-dh01-e01+ink50-dh01-h22+32r-a811542455-576N-140457.txt

Only the long ``36Rack`` spelling was understood, so the compacted form fell through to "count
the rack ids in the name" — which finds exactly two. A 32-rack, 576-node run was therefore filed
in the Rack=2 bucket of the analytics scaling table, alongside real 36-node pairs, and dragged
the Nodes/#GPU columns for that bucket to 576/2304.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

import nvis_id_templates as nid

COMPACT_32 = "root-NEMO-ink50-dh01-e01+ink50-dh01-h22+32r-a811542455-576N-140457.txt"
FOUR_RACK = (
    "root-NEMO-ink50-dh01-h05+ink50-dh01-h06+ink50-dh01-h07+ink50-dh01-h08-72N-131805.txt"
)
EIGHT_RACK = (
    "root-NEMO-ink50-dh01-h08+ink50-dh01-h15+ink50-dh01-h16+ink50-dh01-h17"
    "+ink50-dh01-h18+ink50-dh01-h19+ink50-dh01-h20+ink50-dh01-h21-144N-140186.txt"
)
TWO_RACK = "root-NEMO-ink50-dh01-h19+ink50-dh01-h20-36N-131582.txt"


@pytest.mark.parametrize(
    "name,expected",
    [
        (COMPACT_32, 32),
        ("cmods-NEMO-36Rack-x-9.txt", 36),
        ("cmods-NEMO-4Rack-x-9.txt", 4),
        # No declaration at all: the caller counts ids instead.
        (TWO_RACK, None),
        (FOUR_RACK, None),
        ("cmods-NEMO-1Rack-p05-r01-18N-9.txt", None),
    ],
)
def test_declared_rack_count(name, expected):
    assert nid.multirack_rack_count(name) == expected


def test_the_compacted_form_is_not_mistaken_for_a_rack_id_count():
    """The regression, stated plainly: the name carries two rack ids and means thirty-two."""
    assert nid.multirack_rack_count(COMPACT_32) == 32
    assert COMPACT_32.count("ink50-dh01-") == 2, "only the endpoints survive compaction"


def test_app_buckets_the_compacted_run_by_its_declared_count():
    """End to end through the function the analytics scaling table buckets on."""
    import app as app_module

    assert app_module._multirack_rack_count_from_basename(COMPACT_32) == 32


@pytest.mark.parametrize("name", [TWO_RACK, FOUR_RACK, EIGHT_RACK])
def test_names_that_declare_nothing_still_fall_back_to_counting_ids(name):
    """Unchanged behaviour for the uncompacted form.

    Asserted against the id extractor rather than a literal, because how many ids are recognised
    depends on the active naming templates and those are cluster configuration, not a constant.
    """
    import app as app_module

    assert nid.multirack_rack_count(name) is None, "no declared count in this form"
    expected = len(app_module._multirack_physical_rack_ids(name))
    assert app_module._multirack_rack_count_from_basename(name) == expected


def test_a_hostname_with_an_r_segment_is_not_read_as_a_count():
    """The compacted marker needs the digest, so an ordinary rack id cannot impersonate it."""
    assert nid.multirack_rack_count("root-NEMO-p05-r01+p05-r02-36N-1.txt") is None


def test_the_long_marker_still_wins_where_both_could_match():
    assert nid.multirack_rack_count("cmods-NEMO-8Rack-x-1.txt") == 8
