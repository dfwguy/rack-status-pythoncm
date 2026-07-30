# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""The daily node report must satisfy the customer's contract, not our reading of it.

The point of these tests is the schema check: ``result_schema.json`` is vendored from
``nvis-ova/nscale`` and is strict (``additionalProperties: false`` everywhere, pattern-
constrained ``test_id``, non-empty ``firmware_inventory.nodes``). Generating the document in
Python only helps if something proves it validates, so that assertion comes first.
"""
import json

import node_report
import pytest

PLATFORM = {
    "id": "gb300-nvl72",
    "name": "GB300 NVL72",
    "generation": "GB300",
    "gpu_model": "NVIDIA Blackwell Ultra B300",
    "cpu_vendor": "NVIDIA Grace",
    "cpu_arch": "arm64",
    "system_vendor": "NVIDIA",
    "form_factor": "NVL72",
    "geometry": {"gpus_per_node": 4, "trays_per_rack": 18, "nvlink_domain": 72},
    "fabric": {"scaleout": "ib", "rails_per_gpu": 1, "rail_rate_gbps": 400},
    "interconnect": {"scaleup": "NVLink 5"},
}


def _job(name, status, metric=None, label=None, reason=""):
    return {
        "name": name, "base": name, "status": status,
        "reason": reason, "metric": metric, "metric_label": label,
    }


def _fleet(nodes):
    cols = ["dcgmi-r1", "nvbandwidth", "allreduce"]
    return {
        "columns": cols,
        "metric_labels": {"nvbandwidth": "min GB/s", "allreduce": "busbw GB/s"},
        "nodes": nodes,
    }


def _node(name, *, allreduce="DONE", metric="688.68", slurm="idle"):
    return {
        "node": name,
        "slurm_state": slurm,
        "jobs": [
            _job("dcgmi-r1", "DONE"),
            _job("nvbandwidth", "DONE", "357.23", "min GB/s"),
            _job("allreduce", allreduce, metric, "busbw GB/s",
                 reason="below floor" if allreduce == "FAIL" else ""),
        ],
    }


FLEET = _fleet([
    _node("ink50-dh01-h01-11"),
    _node("ink50-dh01-h01-12", allreduce="FAIL", metric="2.28"),
    _node("ink50-dh01-h02-11", allreduce="NOT_RUN", metric=None),
])


@pytest.fixture
def report():
    return node_report.build_report(FLEET, platform=PLATFORM, cluster={"name": "Nscale TX"})


def test_report_validates_against_the_vendored_schema(report):
    assert node_report.validate(report) == []


def test_a_fleet_with_no_results_cannot_be_a_valid_report():
    """The schema requires at least one test_result, and a report with none says nothing.

    The generator stays pure and simply produces an empty set; ``/api/node-report`` is what
    turns that into an honest 409 rather than shipping an invalid document.
    """
    empty = node_report.build_report({"columns": [], "nodes": []}, platform=PLATFORM)
    assert empty["summary"]["total_tests"] == 0
    assert any("test_results" in e for e in node_report.validate(empty))


class TestTestResults:
    def test_ids_follow_the_schema_pattern_per_category(self, report):
        ids = [t["test_id"] for t in report["test_results"]]
        assert ids == ["CH-01", "GPU-01", "NET-01"]

    def test_column_name_stays_traceable_from_the_id(self, report):
        """A reader must be able to get from NET-01 back to the bring-up column."""
        net = next(t for t in report["test_results"] if t["test_id"] == "NET-01")
        assert "allreduce" in net["execution"]["command"]

    def test_bringup_verdicts_map_onto_the_status_enum(self, report):
        net = next(t for t in report["test_results"] if t["test_id"] == "NET-01")
        m = net["measurements"]
        assert (m["nodes_passed"], m["nodes_failed"], m["nodes_not_run"]) == (1, 1, 1)
        # 1 of 2 judged nodes passed, below the default 100% gate.
        assert net["status"] == "FAIL"

    def test_metric_aggregates_are_named_with_their_unit(self, report):
        net = next(t for t in report["test_results"] if t["test_id"] == "NET-01")
        m = net["measurements"]
        assert m["busbw_gb_s_min"] == 2.28
        assert m["busbw_gb_s_max"] == 688.68
        assert m["nodes_measured"] == 2  # the NOT_RUN node contributed no number

    def test_published_threshold_drives_the_rollup(self):
        """Relaxing the gate must change the verdict and say so in the document."""
        relaxed = node_report.build_report(
            FLEET, platform=PLATFORM, overrides={"node_pass_rate_min_pct": 50.0}
        )
        net = next(t for t in relaxed["test_results"] if t["test_id"] == "NET-01")
        assert net["status"] == "PASS"
        assert net["acceptance_criteria"]["thresholds"]["node_pass_rate_min_pct"] == 50.0
        assert node_report.validate(relaxed) == []

    def test_a_column_nobody_ran_is_not_a_pass(self):
        none_run = _fleet([_node("n1", allreduce="NOT_RUN", metric=None)])
        for job in none_run["nodes"][0]["jobs"]:
            job["status"] = "NOT_RUN"
        built = node_report.build_report(none_run, platform=PLATFORM)
        assert {t["status"] for t in built["test_results"]} == {"NOT_RUN"}
        assert built["summary"]["passed"] == 0


class TestDerivedAndAssumed:
    def test_compute_model_comes_from_the_catalog_platform(self, report):
        cm = report["system_under_test"]["compute_model"]
        assert cm["platform"] == "GB300 NVL72"
        assert cm["gpu"]["count_per_node"] == 4  # geometry.gpus_per_node
        assert cm["interconnect"]["ib_devices_per_node"] == 4  # rails_per_gpu x gpus_per_node
        assert cm["interconnect"]["intra_node"] == "NVLink 5"

    def test_unmeasured_firmware_is_marked_not_faked(self, report):
        baseline = report["firmware_inventory"]["shared_baseline"]
        assert baseline["host"]["bios_version"] == node_report.TBD
        assert baseline["bmc"]["firmware"] == node_report.TBD
        # What we do know still comes through.
        assert baseline["gpu"]["model"] == "NVIDIA Blackwell Ultra B300"
        assert baseline["gpu"]["count_per_node"] == 4

    def test_placeholder_node_entry_is_obviously_a_placeholder(self, report):
        """The schema needs a node entry; it must not look like measured hardware."""
        node = report["firmware_inventory"]["nodes"][0]
        assert node["hostname"] == "ink50-dh01-h01-11"  # real and traceable
        assert node["bmc"]["mac"] == "00:00:00:00:00:00"
        assert node["gpus"][0]["uuid"] == node_report.PLACEHOLDER_GPU_UUID
        assert node["serial"] == node_report.TBD

    def test_overrides_replace_assumptions_without_touching_the_module(self):
        built = node_report.build_report(
            FLEET,
            platform=PLATFORM,
            overrides={
                "operator": {"name": "Bryan Reese", "organization": "NVIDIA"},
                "firmware": {"software": {"os": "Ubuntu 24.04 LTS", "kernel": "6.14.0-1008"}},
            },
        )
        assert built["report_metadata"]["operator"]["name"] == "Bryan Reese"
        sw = built["firmware_inventory"]["shared_baseline"]["software"]
        assert sw["os"] == "Ubuntu 24.04 LTS"
        assert sw["kernel"] == "6.14.0-1008"
        # Un-overridden assumptions survive the merge.
        assert built["firmware_inventory"]["shared_baseline"]["bmc"]["firmware"] == node_report.TBD
        assert node_report.validate(built) == []

    def test_report_id_is_dated_and_slugged_from_the_cluster(self, report):
        rid = report["report_metadata"]["report_id"]
        assert rid.startswith("Nscale-TX-")
        assert rid.endswith("-001")

    def test_racks_are_attributed_from_node_names(self, report):
        assert report["system_under_test"]["scope"]["rack_ids"] == [
            "ink50-dh01-h01", "ink50-dh01-h02",
        ]


def test_nulls_are_omitted_rather_than_emitted(report):
    """Optional fields are plain strings in the schema, so null would fail validation."""
    blob = json.dumps(report)
    assert ": null" not in blob
    assert "null," not in blob
