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
"""The job registries must stay ONE object each, however many modules import them.

``app.py`` imports these from ``job_registry`` under their original names so ~40 existing
mutate-under-lock call sites did not have to change. That works only because a dict is shared by
reference: ``app._reset_jobs`` *is* ``job_registry._reset_jobs``.

The failure it guards against is silent. Rebinding in one module (``_reset_jobs = {}``) gives that
module a private dict while the rest of the app keeps the old one -- and the lock still protects
only the original, so it is a data race as well as a lost write. Nothing raises; jobs simply stop
appearing in ``GET /api/rack-locks`` or the ``*-status`` endpoints.
"""
from __future__ import annotations

import pytest

import app
import job_registry as J

REGISTRIES = [
    "_validation_jobs",
    "_stress_jobs",
    "_xrack_jobs",
    "_llama_jobs",
    "_reset_jobs",
    "_analytics_jobs",
    "_node_report_jobs",
]


@pytest.mark.parametrize("name", REGISTRIES)
def test_the_registry_is_the_same_object_in_both_modules(name):
    assert getattr(app, name) is getattr(J, name)


@pytest.mark.parametrize("name", REGISTRIES)
def test_the_lock_is_the_same_object_in_both_modules(name):
    """A second lock would protect a different dict -- worse than no lock, because it looks safe."""
    assert getattr(app, name + "_lock") is getattr(J, name + "_lock")


def test_a_mutation_is_visible_through_either_name():
    key = "test-shared-object-probe"
    try:
        with J._reset_jobs_lock:
            J._reset_jobs[key] = {"phase": "running"}
        assert app._reset_jobs.get(key) == {"phase": "running"}
        with app._reset_jobs_lock:
            app._reset_jobs[key]["phase"] = "done"
        assert J._reset_jobs[key]["phase"] == "done"
    finally:
        with J._reset_jobs_lock:
            J._reset_jobs.pop(key, None)


def test_track_sync_op_registers_and_finishes_a_synthetic_entry():
    """It exists so long sync handlers show up in /api/rack-locks for their duration.

    ``p17-r01`` is the shape the default naming template accepts (it is the one quoted in
    ``rack_locks``' own error message), so this does not depend on a cluster's NVIS_RACK_ID.
    """
    rack = "p17-r01"
    with J._track_sync_op(rack, "probe", operator="tester") as job_id:
        assert job_id and job_id.startswith("sync-")
        with J._reset_jobs_lock:
            row = J._reset_jobs[job_id]
        assert row["phase"] == "running"
        assert row["sync"] is True
        assert row["racks"] == [rack]
        # Same entry seen through app's alias, which is the point of the shared object.
        assert app._reset_jobs[job_id] is row
    with J._reset_jobs_lock:
        row = J._reset_jobs.get(job_id)
    assert row is None or row["phase"] == "done"


def test_track_sync_op_still_runs_the_handler_for_an_unusable_rack():
    """A bad rack id must not break the route it wraps; it just runs untracked."""
    with J._track_sync_op("", "probe") as job_id:
        assert job_id is None
