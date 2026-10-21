# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The multirack log table places a run's nodes the way the rack table does.

On Nscale NC the hostnames name no rack (rack U20 holds sys1-gso1-p-phy-gpu19..36-su19), so the
multirack worker's hostname parse found no rack for a run and no Slurm tally for any rack. Where the
mapping mode is not ``direct`` the table now takes a run's racks from the hosts its log names,
placed through the dashboard's node inventory, and the per-rack counts from that same inventory.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import native_status as ns
import pytest
from native_status import MultirackTableState

U20 = [f"sys1-gso1-p-phy-gpu{n}-su19" for n in range(19, 37)]
U21 = [f"sys1-gso1-p-phy-gpu{n}-su19" for n in range(37, 55)]
LOG = PurePosixPath("/home/cmods/nvis/logs/cmods-NEMO-2Rack-36N-130001.txt")


def _inventory(down: int = 0) -> list[dict]:
    rows = [{"id": h, "rack": "U20", "status": "up", "scheduler_state": "idle", "ip": ""} for h in U20]
    rows += [{"id": h, "rack": "U21", "status": "down" if i < down else "up",
              "scheduler_state": "idle", "ip": ""} for i, h in enumerate(U21)]
    rows.append({"id": "appserver-nc", "rack": None, "status": "up", "scheduler_state": "idle", "ip": ""})
    return rows


@pytest.fixture
def no_squeue(monkeypatch):
    monkeypatch.setattr(ns, "load_squeue_jobs_map", lambda: (set(), {}))
    monkeypatch.setattr(ns, "augment_running_set_for_job_ids", lambda *a, **k: None)


def _state(probe: dict) -> MultirackTableState:
    st = MultirackTableState("test-mr-translate", Path("/tmp"))
    st.log_paths = [(LOG, 1, 100)]
    st.per_path_probe = {str(LOG): probe}
    st.log_done = True
    st.sinfo_done = True
    return st


DONE = {"ok": True, "nemo_state": "done", "nccl_ok": True, "llm_ok": True, "jid": 130001}


class TestHostnameTally:
    def test_translate_parses_no_hostname(self, monkeypatch):
        monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "translate")
        assert ns.sinfo_counts_for_nodes({h: "idle" for h in U20}) == {}

    def test_pool_parses_no_hostname(self, monkeypatch):
        monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "pool")
        assert ns.sinfo_counts_for_nodes({"ink50-dh01-h02-11": "idle"}) == {}


class TestTranslateRows:
    def test_racks_come_from_the_logs_hosts_through_the_inventory(self, monkeypatch, no_squeue):
        monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "translate")
        st = _state({**DONE, "hosts": U21[:2] + U20[:2] + ["not-in-inventory"]})
        rows = st.build_rows(lambda bn: bn, lambda bn: [], _inventory(down=2))
        assert rows[0]["rack"] == "U20+U21 (36N)"
        assert rows[0]["nemo"] == "done"
        # Counts are the inventory's, as on the rack table: U20 all up, U21 two down.
        assert rows[0]["sinfo"] == "U20 up=18; U21 up=16,down=2"

    def test_a_stale_hostname_parse_is_not_trusted_in_translate(self, monkeypatch, no_squeue):
        monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "translate")
        st = _state({**DONE, "physical_racks": ["p1-r19"], "hosts": U20[:1]})
        rows = st.build_rows(lambda bn: bn, lambda bn: [], _inventory())
        assert rows[0]["rack"].startswith("U20")
        assert "p1-r19" not in rows[0]["rack"]

    def test_without_an_inventory_the_basename_label_stands(self, monkeypatch, no_squeue):
        monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "translate")
        st = _state({**DONE, "hosts": U20[:1]})
        rows = st.build_rows(lambda bn: "from-basename", lambda bn: [], None)
        assert rows[0]["rack"] == "from-basename"


class TestDirectRowsUnchanged:
    def test_direct_keeps_the_logs_parsed_racks(self, monkeypatch, no_squeue):
        monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "direct")
        st = _state({**DONE, "physical_racks": ["ink50-dh01-h02"], "hosts": U20[:1]})
        rows = st.build_rows(lambda bn: bn, lambda bn: [], _inventory())
        assert rows[0]["rack"].startswith("ink50-dh01-h02")


class TestWorkerCachesHosts:
    def test_translate_worker_reads_hosts_not_racks(self, monkeypatch):
        monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "translate")
        monkeypatch.setattr(ns, "_load_log_cache", lambda _p: {})
        saved: dict = {}
        monkeypatch.setattr(ns, "_save_log_cache", lambda _p, disk, _alive: saved.update(disk))
        monkeypatch.setattr(ns, "_hosts_in_partial_log", lambda _p: list(U20))
        monkeypatch.setattr(ns, "extract_physical_rack_ids_from_multirack_log",
                            lambda _p: pytest.fail("translate must not parse racks out of hostnames"))
        st = MultirackTableState("test-mr-worker", Path("/tmp"))
        st._worker_logs_m(lambda _p: dict(DONE), [(LOG, 1, 100)])
        assert st.per_path_probe[str(LOG)]["hosts"] == U20
        assert saved[str(LOG)]["probe"]["hosts"] == U20


class TestCompressedNodelist:
    def test_a_slurm_hostlist_on_the_nodelist_line_is_expanded(self, monkeypatch):
        text = "NODELIST: sys1-gso1-p-phy-gpu[19-20]-su19,sys1-gso1-p-phy-gpu37-su19\n"
        monkeypatch.setattr(ns.host_exec, "read_text", lambda *a, **k: (text, {}))
        assert ns._hosts_in_partial_log(LOG) == [
            "sys1-gso1-p-phy-gpu19-su19", "sys1-gso1-p-phy-gpu20-su19", "sys1-gso1-p-phy-gpu37-su19",
        ]


def test_no_dashboard_state_means_no_inventory(monkeypatch):
    monkeypatch.setattr(ns, "_states", {})
    assert ns._dashboard_node_inventory(Path("/nonexistent-log-dir")) is None


class TestProviderSlurmStates:
    """A provider-inventoried cluster (NC: scheduler pythoncm) still learns each node's Slurm state."""

    @staticmethod
    def _state(monkeypatch, records, sinfo):
        from types import SimpleNamespace
        from unittest.mock import Mock

        provider = SimpleNamespace(compute_node_inventory=Mock(return_value=records))
        monkeypatch.setattr(ns.config_env, "_eget", lambda key, default="": (
            "pythoncm" if key == "RACK_SCHEDULER" else default
        ))
        monkeypatch.setattr(ns, "_get_platform", lambda: provider)
        monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "translate")
        monkeypatch.setattr(ns, "collect_sinfo_nodes_and_shapes", sinfo)
        st = ns.NativeRackTableState("provider-slurm", Path("/logs"))
        st.racks = ["U20"]
        return st

    def test_slurm_states_ride_the_providers_roster(self, monkeypatch):
        records = [{"id": h, "rack": "U20", "ip": "", "scheduler_state": ""} for h in U20[:2]]
        sinfo = lambda: ({U20[0]: "idle", U20[1]: "drain", "not-in-bcm": "idle"}, {})  # noqa: E731
        st = self._state(monkeypatch, records, sinfo)
        st._worker_sinfo()
        nodes = st.node_inventory_snapshot()["nodes"]
        assert [(n["id"], n["scheduler_state"], n["rack"]) for n in nodes] == [
            (U20[0], "idle", "U20"), (U20[1], "drain", "U20"),
        ]
        # The floor view's scheduler mode reads the normalized word, and names Slurm as its source
        # although the cluster's scheduler is recorded as pythoncm: Slurm is who answered.
        assert [n["scheduler_status"] for n in nodes] == ["idle", "drained"]
        assert st.node_inventory_snapshot()["node_sources"]["scheduler"] == "slurm"

    def test_a_cluster_without_slurm_backs_off_and_keeps_its_roster(self, monkeypatch):
        records = [{"id": U20[0], "rack": "U20", "ip": "", "scheduler_state": ""}]
        calls = []

        def sinfo():
            calls.append(1)
            raise RuntimeError("Slurm node inventory unavailable")

        st = self._state(monkeypatch, records, sinfo)
        st._worker_sinfo()
        st._worker_sinfo()
        assert len(calls) == 1
        nodes = st.node_inventory_snapshot()["nodes"]
        assert [(n["id"], n["scheduler_state"]) for n in nodes] == [(U20[0], "")]
        assert st.node_inventory_snapshot()["node_inventory_error"] == ""
        # Nobody answered for the scheduler: no scheduler view, rather than a floor of "unknown".
        assert nodes[0]["scheduler_status"] == "unknown"
        assert st.node_inventory_snapshot()["node_sources"]["scheduler"] == ""
