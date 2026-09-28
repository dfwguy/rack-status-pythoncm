# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Which backend runs work is a property of the cluster, not of the process.

`cluster_manager` already said who manages the machines; `scheduler` says where work runs,
and the two are independent -- a BCM cluster may or may not have Slurm. Putting it on the
record rather than in the environment is what lets one instance serve a Slurm site and a
native-scheduler site at once, and makes switching clusters switch the backend with it.
"""
from __future__ import annotations

import pytest
from catalog import context, models
from schedulers import factory


def _cluster(**kw):
    return models.Cluster.from_dict({"id": "c1", "name": "C1", **kw})


class TestTheField:
    def test_the_default_is_slurm(self):
        """Asymmetric on purpose. Defaulting to a native scheduler on a Slurm cluster gives a
        queue view holding only work this product launched -- plausible and wrong. Defaulting
        to Slurm where there is none fails loudly on the first submit."""
        assert _cluster().scheduler == "slurm"

    def test_it_is_case_folded(self):
        assert _cluster(scheduler="PythonCM").scheduler == "pythoncm"

    def test_a_blank_value_falls_back_to_the_default(self):
        assert _cluster(scheduler="").scheduler == "slurm"

    def test_a_known_kind_validates(self):
        for kind in models.SCHEDULERS:
            assert _cluster(scheduler=kind).validate() == [], kind

    def test_an_unknown_kind_is_refused(self):
        problems = _cluster(scheduler="sge").validate()
        assert any("unknown scheduler" in p for p in problems), problems

    def test_it_round_trips(self):
        assert _cluster(scheduler="pythoncm").to_dict()["scheduler"] == "pythoncm"

    def test_it_is_independent_of_the_cluster_manager(self):
        """A BCM cluster may or may not run Slurm; the two fields answer different questions."""
        c = _cluster(cluster_manager="bcm", scheduler="pythoncm")
        assert c.validate() == []
        assert (c.cluster_manager, c.scheduler) == ("bcm", "pythoncm")


class TestItReachesTheFactory:
    def test_the_overlay_publishes_the_name_the_factory_reads(self):
        """`build_scheduler` reads RACK_SCHEDULER. The cluster record is what sets it, so the
        selection travels with the active cluster instead of being process-global."""
        env = context.high_overlay(None, _cluster(scheduler="pythoncm"), None)
        assert env["RACK_SCHEDULER"] == "pythoncm"

    def test_every_declared_kind_is_one_the_factory_can_build(self):
        """The drift guard. A cluster naming a kind the factory cannot build is a deployment
        that comes up cleanly and then fails on its first launch -- the failure lands far from
        the cause. Catch it at the vocabulary instead."""
        buildable = set(factory._BUILDERS)
        declared = set(models.SCHEDULERS)
        assert declared <= buildable, f"declared but unbuildable: {sorted(declared - buildable)}"

    def test_every_buildable_kind_is_declared(self):
        """And the other direction, so a backend added to the factory cannot be unselectable
        from a cluster record."""
        buildable = set(factory._BUILDERS)
        declared = set(models.SCHEDULERS)
        assert buildable <= declared, f"buildable but undeclared: {sorted(buildable - declared)}"


class TestSeededProjects:
    def test_a_seeded_default_project_names_a_valid_scheduler(self):
        from catalog import seed

        project = seed.build_default_project_from_env(env={"RACK_CLUSTER_NAME": "lab"})
        for cluster in project.clusters:
            assert cluster.scheduler in models.SCHEDULERS
            assert cluster.validate() == []


@pytest.mark.parametrize("kind", ["slurm", "pythoncm"])
def test_the_two_kinds_we_actually_ship_are_selectable(kind):
    assert kind in models.SCHEDULERS
    assert _cluster(scheduler=kind).validate() == []


class TestOneListOfSchedulers:
    """The catalog, the workflow validator and the factory must agree on what a scheduler is.

    The workflow validator used to keep its own copy of the list, which predated PythonCM, so a
    cluster could be set to `pythoncm` while a workflow step naming it was rejected as invalid.
    """

    def test_the_factory_builds_every_scheduler_the_catalog_accepts(self):
        assert set(models.SCHEDULERS) == set(factory._BUILDERS)

    @pytest.mark.parametrize("kind", models.SCHEDULERS)
    def test_a_workflow_step_may_name_any_catalog_scheduler(self, kind):
        from workflow_models import validate_scheduler

        assert validate_scheduler({"kind": kind, "script": "x.sh"}) == []
