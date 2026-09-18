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

Consumers import these from ``job_registry`` under their original names so the ~40 existing
mutate-under-lock call sites did not have to change. That works only because a dict is shared by
reference: ``api.rack_locks._reset_jobs`` *is* ``job_registry._reset_jobs``.

The failure it guards against is silent. Rebinding in one module (``_reset_jobs = {}``) gives that
module a private dict while the rest of the app keeps the old one -- and the lock still protects
only the original, so it is a data race as well as a lost write. Nothing raises; jobs simply stop
appearing in ``GET /api/rack-locks`` or the ``*-status`` endpoints.

Importers are discovered rather than listed: as routes move out of ``app.py`` into ``backend/api/``,
the module holding a given registry changes, and a hard-coded list would keep passing while checking
the wrong thing.
"""
from __future__ import annotations

import sys

import app  # noqa: F401 - imported so the api/ modules are loaded and can be inspected
import job_registry as J
import pytest

REGISTRIES = [
    "_validation_jobs",
    "_stress_jobs",
    "_xrack_jobs",
    "_llama_jobs",
    "_reset_jobs",
    "_analytics_jobs",
    "_node_report_jobs",
]


def _importers(name: str) -> dict[str, object]:
    """Every loaded backend module exposing *name*, excluding job_registry itself."""
    found = {}
    for modname, mod in list(sys.modules.items()):
        if mod is None or modname == "job_registry":
            continue
        path = getattr(mod, "__file__", "") or ""
        if "/backend/" not in path:
            continue
        if hasattr(mod, name):
            found[modname] = getattr(mod, name)
    return found


@pytest.mark.parametrize("name", REGISTRIES)
def test_the_registry_is_the_same_object_everywhere(name):
    owner = getattr(J, name)
    for modname, obj in _importers(name).items():
        assert obj is owner, f"{modname}.{name} is a different object from job_registry.{name}"


@pytest.mark.parametrize("name", REGISTRIES)
def test_the_lock_is_the_same_object_everywhere(name):
    """A second lock would protect a different dict -- worse than no lock, because it looks safe."""
    lock_name = name + "_lock"
    owner = getattr(J, lock_name)
    for modname, obj in _importers(lock_name).items():
        assert obj is owner, f"{modname}.{lock_name} is a different lock from job_registry's"


def test_a_mutation_is_visible_through_either_name():
    key = "test-shared-object-probe"
    try:
        with J._reset_jobs_lock:
            J._reset_jobs[key] = {"phase": "running"}
        # Any importer will do, and it is DISCOVERED rather than named: app.py itself stopped
        # importing these once the launch routes moved, and hardcoding it made this test fail for a
        # reason that had nothing to do with the invariant it guards.
        other = next(iter(_importers("_reset_jobs").values()))
        other_lock = next(iter(_importers("_reset_jobs_lock").values()))
        assert other.get(key) == {"phase": "running"}
        with other_lock:
            other[key]["phase"] = "done"
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
        # Same entry seen through another module's alias, which is the point of the shared object.
        other = next(iter(_importers("_reset_jobs").values()))
        assert other[job_id] is row
    with J._reset_jobs_lock:
        row = J._reset_jobs.get(job_id)
    assert row is None or row["phase"] == "done"


def test_track_sync_op_still_runs_the_handler_for_an_unusable_rack():
    """A bad rack id must not break the route it wraps; it just runs untracked."""
    with J._track_sync_op("", "probe") as job_id:
        assert job_id is None
