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
"""The Rack Status report: three renderings, one dataset.

The property worth defending is that CSV, JSON and PDF all come from the same
:func:`cluster_report.build_dataset` output. Three independent generators would drift the moment
one gained a column, and a report that contradicts itself is worse than one that is missing.
"""
from __future__ import annotations

import csv
import io
import json

import cluster_report
import pytest

FACTS = "\n".join([
    "bcm.version\tCluster Manager v11.0",
    "slurm.version\t25.05.8",
    "nccl.version\t2.29.3+cuda13.1",
    "nccl.source\trun log (container image)",
    "container.image\tnvidia+nemo+26.04.01.sqsh",
    "head.hostname\tnscale-hn1.cm.cluster",
    "head.os\tUbuntu 24.04.4 LTS",
])

INVENTORY = {
    "captured_at": "2026-08-12T12:00:00Z",
    "shared_baseline": {
        "gpu": {"model": "NVIDIA GB300", "count_per_node": 4, "driver_version": "580.126.20",
                "vbios_version": "97.10.4A.00.1F", "cuda_runtime": "13.0"},
        "nic": {"model": "ConnectX-8", "firmware": "40.48.1126",
                "ofed_version": "OFED-internal-25.10", "link_speed": "800Gb/s"},
        "software": {"os": "Ubuntu 24.04.4 LTS", "kernel": "6.17.0-1014-nvidia-64k",
                     "dcgm_version": "4.6.1"},
        "bmc": {"firmware": "1.30"},
        "host": {"bios_version": "1.0.3"},
    },
    "nodes": [
        {"hostname": "n1", "serial": "BKPZPK4", "rack_id": "WH1D1", "rack_unit": "18",
         "bmc": {"ip": "10.40.128.9", "firmware": "1.30"},
         "nics": [{"device": "mlx5_0", "firmware": "40.48.1126", "link_layer": "InfiniBand"}],
         "gpus": [{"uuid": "GPU-aaa", "vbios_version": "97.10.4A.00.1F"},
                  {"uuid": "GPU-bbb", "vbios_version": "97.10.4A.00.1F"}],
         "firmware_overrides": {}},
        {"hostname": "n2", "serial": "6PNZPK4", "rack_id": "WH1D1", "rack_unit": "19",
         "bmc": {"ip": "10.40.128.10", "firmware": "1.30"},
         "nics": [{"device": "mlx5_0", "firmware": "40.48.1120", "link_layer": "InfiniBand"}],
         "gpus": [{"uuid": "GPU-ccc", "vbios_version": "97.10.4A.00.1F"}],
         "firmware_overrides": {"* carried forward from": "2026-08-10T00:00:00Z"}},
    ],
}

FLEET = {
    "columns": ["allreduce", "alltoall", "llm"],
    "metric_labels": {"allreduce": "busbw GB/s", "alltoall": "busbw GB/s", "llm": "TFLOP/s"},
    "nodes": [
        {"node": "n1", "jobs": [
            {"name": "allreduce", "status": "DONE", "metric": "916.4"},
            {"name": "alltoall", "status": "DONE", "metric": "742.1"},
            {"name": "llm", "status": "DONE", "metric": "1432.0"}]},
        {"node": "n2", "jobs": [
            {"name": "allreduce", "status": "FAIL", "metric": "2.3"},
            {"name": "alltoall", "status": "DONE", "metric": "740.0"},
            {"name": "llm", "status": "NOT_RUN", "metric": None}]},
    ],
}

ANALYTICS = {
    "single_rack": {
        "nccl_collectives": [
            {"rack": "WH1D1", "all_reduce_gbps": 916.4, "alltoall_gbps": 742.1},
            {"rack": "WH1D2", "all_reduce_gbps": 880.2, "alltoall_gbps": 730.0},
        ],
        "nccl_ar_series": {"sizes_bytes": [1048576, 16777216, 1073741824],
                           "median_bus_bw_gbps": [120.5, 640.2, 916.4]},
        "nemotron": [{"rack": "WH1D1", "tflops_per_gpu": 1432.0, "median_step_time": 5.75}],
    },
    "multirack": {
        "nemotron_scaling": [{"n_racks": 1, "mean_tflops_per_gpu": 1432.0},
                             {"n_racks": 8, "mean_tflops_per_gpu": 1401.5},
                             {"n_racks": 32, "mean_tflops_per_gpu": 1355.0}],
    },
}


@pytest.fixture
def dataset():
    return cluster_report.build_dataset(
        fleet=FLEET,
        inventory=INVENTORY,
        facts=cluster_report.parse_facts(FACTS),
        analytics=ANALYTICS,
        cluster={"name": "NscaleTX-GB300"},
        project={"customer": "NscaleTX"},
        platform={"name": "GB300 NVL72"},
    )


class TestDataset:
    def test_cluster_facts_and_fleet_baselines_are_both_present(self, dataset):
        c = dataset["cluster"]
        assert c["bcm_version"] == "Cluster Manager v11.0"   # from the head probe
        assert c["slurm_version"] == "25.05.8"
        assert c["nccl_version"] == "2.29.3+cuda13.1"        # from the run log
        assert c["gpu_model"] == "NVIDIA GB300"              # from the inventory baseline
        assert c["driver_version"] == "580.126.20"

    def test_non_compute_exclusions_are_explicit_report_metadata(self):
        fleet = json.loads(json.dumps(FLEET))
        fleet["excluded_non_compute"] = {"count": 1, "nodes": ["appserver"]}
        ds = cluster_report.build_dataset(fleet=fleet)
        assert ds["meta"]["excluded_non_compute"] == {
            "count": 1,
            "nodes": ["appserver"],
        }
        csv_text = cluster_report.to_csv(ds)
        assert "# Excluded non-compute nodes,1" in csv_text
        assert "# Excluded non-compute hostnames,appserver" in csv_text

    def test_ib_versus_roce_is_read_from_the_ports_not_guessed(self, dataset):
        assert dataset["cluster"]["fabric"] == "InfiniBand"

    def test_roce_is_named_as_such_when_the_link_layer_is_ethernet(self):
        inv = json.loads(json.dumps(INVENTORY))
        inv["nodes"][0]["nics"][0]["link_layer"] = "Ethernet"
        ds = cluster_report.build_dataset(fleet=FLEET, inventory=inv)
        assert "RoCE" in ds["cluster"]["fabric"]

    def test_the_host_ip_comes_from_bcm_since_no_other_source_has_it(self):
        """bmc_ip reaches the controller; ip is the host. Only BCM knows the second."""
        ds = cluster_report.build_dataset(
            fleet=FLEET, inventory=INVENTORY,
            bcm={"n1": {"interfaces": [
                {"name": "rf0", "kind": "NetworkBmcInterface", "ip": "10.40.128.9"},
                {"name": "bond0", "kind": "NetworkBondInterface", "ip": "10.40.0.9"},
                {"name": "enP22s2f0np0", "kind": "NetworkPhysicalInterface", "ip": "0.0.0.0"},
            ]}},
        )
        n1 = ds["nodes"][0]
        assert n1["ip"] == "10.40.0.9"       # the host's own address, not the BMC's
        assert n1["bmc_ip"] == "10.40.128.9"

    def test_each_node_row_joins_hardware_to_its_test_results(self, dataset):
        n1 = dataset["nodes"][0]
        assert n1["node"] == "n1"
        assert n1["serial"] == "BKPZPK4"
        assert n1["rack"] == "WH1D1"
        assert n1["bmc_ip"] == "10.40.128.9"
        assert n1["gpu_count"] == 2
        assert "GPU-aaa" in n1["gpu_ids"]
        assert n1["allreduce_metric"] == "916.4"
        assert n1["llm_metric"] == "1432.0"

    def test_one_failing_test_fails_the_node(self, dataset):
        by_node = {r["node"]: r for r in dataset["nodes"]}
        assert by_node["n1"]["overall"] == "PASS"
        assert by_node["n2"]["overall"] == "FAIL"

    def test_test_columns_are_whatever_the_site_ran(self, dataset):
        # Nothing hard-codes a benchmark name, so a new bring-up column needs no code change.
        for col in ("allreduce", "alltoall", "llm"):
            assert f"{col}_status" in dataset["node_columns"]
            assert f"{col}_metric" in dataset["node_columns"]

    def test_a_carried_forward_node_says_when_its_data_was_measured(self, dataset):
        by_node = {r["node"]: r for r in dataset["nodes"]}
        assert by_node["n2"]["data_captured_at"] == "2026-08-10T00:00:00Z"
        assert by_node["n1"]["data_captured_at"] == ""   # measured in this run

    def test_a_per_node_firmware_override_wins_over_the_fleet_baseline(self, dataset):
        # n2's NIC firmware differs from the baseline; the row must show n2's own value.
        by_node = {r["node"]: r for r in dataset["nodes"]}
        assert by_node["n2"]["nic_firmware"] == "40.48.1120"

    def test_summary_counts_match_the_rows(self, dataset):
        s = dataset["summary"]
        assert s["nodes_total"] == 2
        assert s["nodes_passed"] == 1
        assert s["nodes_failed"] == 1
        assert s["racks_total"] == 1

    def test_an_empty_cluster_yields_an_empty_report_not_a_crash(self):
        ds = cluster_report.build_dataset()
        assert ds["nodes"] == []
        assert ds["summary"]["nodes_total"] == 0


def _node_rows(csv_text: str) -> list[dict[str, str]]:
    """Read the node table the way a consumer must: skip ``#`` metadata, stop at the blank line.

    The blank line is the format's contract — the rack-level blocks that follow are a different
    shape, and a reader that ignores the boundary silently parses them as malformed node rows.
    """
    lines: list[str] = []
    for line in csv_text.splitlines():
        if line.startswith("#"):
            continue
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line)
    return list(csv.DictReader(io.StringIO("\n".join(lines))))


class TestCsv:
    def test_the_node_table_parses_as_csv_with_the_metadata_skipped(self, dataset):
        rows = _node_rows(cluster_report.to_csv(dataset))
        assert [r["node"] for r in rows] == ["n1", "n2"]
        assert rows[0]["allreduce_metric"] == "916.4"
        assert rows[1]["overall"] == "FAIL"

    def test_a_blank_line_ends_the_node_table_before_the_rack_blocks(self, dataset):
        text = cluster_report.to_csv(dataset)
        body = [l for l in text.splitlines() if not l.startswith("#")]
        blank = body.index("")
        # Header plus one row per node, then the boundary.
        assert blank == 1 + len(dataset["nodes"])

    def test_cluster_facts_lead_the_file_as_comments_not_repeated_per_row(self, dataset):
        text = cluster_report.to_csv(dataset)
        head = [l for l in text.splitlines() if l.startswith("#")]
        assert any("BCM version,Cluster Manager v11.0" in l for l in head)
        assert any("NCCL,2.29.3+cuda13.1" in l for l in head)
        # Repeating 25 constants on every one of 2000 rows would be noise.
        assert "bcm_version" not in (dataset["node_columns"])

    def test_rack_level_results_follow_as_their_own_labelled_block(self, dataset):
        text = cluster_report.to_csv(dataset)
        assert "# Rack-level NCCL collectives (GB/s)" in text
        assert "# Rack-level NeMo training" in text
        assert "WH1D2" in text


class TestJson:
    def test_json_is_the_dataset_itself(self, dataset):
        parsed = json.loads(cluster_report.to_json(dataset))
        assert parsed["cluster"]["slurm_version"] == "25.05.8"
        assert parsed["nodes"][0]["node"] == "n1"
        assert parsed["racks"]["nemotron_scaling"][2]["n_racks"] == 32

    def test_every_format_reports_the_same_verdicts(self, dataset):
        """The whole point of one dataset: the formats cannot disagree."""
        parsed = json.loads(cluster_report.to_json(dataset))
        rows = _node_rows(cluster_report.to_csv(dataset))
        assert [n["overall"] for n in parsed["nodes"]] == [r["overall"] for r in rows]
        assert [n["node"] for n in parsed["nodes"]] == [r["node"] for r in rows]


class TestPdf:
    def test_it_renders_a_real_pdf(self, dataset):
        pytest.importorskip("reportlab")
        blob = cluster_report.to_pdf(dataset)
        assert blob.startswith(b"%PDF-")
        assert blob.rstrip().endswith(b"%%EOF")
        # Charts and two tables make this comfortably more than an empty document.
        assert len(blob) > 5000

    def test_it_renders_without_an_analytics_aggregate(self):
        pytest.importorskip("reportlab")
        ds = cluster_report.build_dataset(fleet=FLEET, inventory=INVENTORY)
        blob = cluster_report.to_pdf(ds)
        assert blob.startswith(b"%PDF-")

    def test_it_renders_for_an_empty_cluster(self):
        pytest.importorskip("reportlab")
        blob = cluster_report.to_pdf(cluster_report.build_dataset())
        assert blob.startswith(b"%PDF-")

    def test_availability_is_reported_so_a_caller_can_offer_only_what_it_can_produce(self):
        assert cluster_report.pdf_available() is bool(
            pytest.importorskip("reportlab", reason="") or True)
