# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The deployment tracker folds what Rack Status knows into one state per node, on the floor.

Asked for by management on 2026-10-13 as an NC counterpart to the Bldg4/5 Linkify tracker: KPI
tiles, per-SU and per-row progress, a node heatmap and a drill-down table. These tests pin the
classifier's order (a live retry beats an old failure; a drained node is not "completed"), the
SU -> row -> rack grouping, that handed-over SUs leave every step's denominator, and that the
static renderer embeds the dataset as data a node name cannot break out of.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import PurePosixPath

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import deployment_tracker as dt
import pytest
from catalog import models

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- the classifier


@pytest.mark.parametrize(
    ("s1", "s2", "sched", "inv", "handed", "want"),
    [
        ("pending", "pending", "idle", "up", False, "not_started"),
        ("done", "pending", "idle", "up", False, "in_progress"),
        ("done", "done", "idle", "up", False, "completed"),
        ("fail", "pending", "idle", "up", False, "remediation"),
        # A live retry outranks the failure it is retrying.
        ("fail", "ongoing", "running", "up", False, "in_progress"),
        # Passed both, drained since: readiness is now, so it is back on the list to chase.
        ("done", "done", "drained", "up", False, "remediation"),
        ("pending", "pending", "draining", "up", False, "remediation"),
        ("pending", "pending", "", "down", False, "remediation"),
        # Handed over wins over everything: we no longer run on it.
        ("fail", "pending", "down", "down", True, "handed_over"),
    ],
)
def test_classify(s1, s2, sched, inv, handed, want):
    assert dt.classify(s1, s2, scheduler_status=sched, inventory_status=inv, handed_over=handed) == want


# ---------------------------------------------------------------- the dataset


def _node(name, rack, *, unit=None, sched="idle", status="up", raw=""):
    return {
        "id": name, "rack": rack, "rack_unit": unit, "status": status,
        "scheduler_status": sched, "scheduler_state": raw or sched,
    }


def _stage(name, s1, s2, reason=""):
    stage1 = {"state": s1, **({"reason": reason} if reason else {})}
    return {"node": name, "rack": "", "stage1": stage1, "stage2": {"state": s2}}


@pytest.fixture
def floor():
    """Two SUs; SU19 is handed over. SU20 spans rows a and b; rack V22 is in no SU."""
    payload = {
        "updated_at": "2026-10-13T11:59:00Z",
        "nodes": [
            _node("gpu2", "V20", unit=2),
            _node("gpu1", "V20", unit=1),
            _node("gpu3", "V21", unit=1, sched="drained", raw="IDLE+DRAIN"),
            _node("gpu4", "U19", unit=1),
            _node("gpu5", "V22", unit=1),
            _node("loose", None),
        ],
        "rows": [
            {"rack": "V20", "stage1": "done", "stage2": "pending"},
            {"rack": "V21", "stage1": "fail", "stage2": "pending"},
            {"rack": "U19", "stage1": "done", "stage2": "done"},
            {"rack": "V22", "stage1": "pending", "stage2": "pending"},
        ],
        "hierarchy": {
            "levels": ["row", "rack"],
            "sections": [{"key": "", "label": "", "rows": [
                {"key": "b", "label": "B", "racks": [{"rack": "V21"}]},
                {"key": "a", "label": "Row A", "racks": [{"rack": "V20"}, {"rack": "U19"}]},
            ]}],
            "unmatched": [],
        },
        "scalable_units": [
            {"id": "su20", "label": "SU20", "racks": ["V20", "V21"], "handed_over_at": ""},
            {"id": "su19", "label": "SU19", "racks": ["U19"], "handed_over_at": "2026-10-13"},
        ],
    }
    stages = {
        "nodes": [
            _stage("gpu1", "done", "done"),
            _stage("gpu2", "done", "ongoing"),
            _stage("gpu3", "fail", "pending", reason="fabric not up"),
            _stage("gpu4", "done", "done"),
            _stage("gpu5", "pending", "pending"),
            _stage("loose", "pending", "unknown"),
        ],
        "stage2_source": "bringup:llm-pretrain-solo",
        "stage2_loading": False,
    }
    return dt.build_dataset(payload, stages, cluster_name="Nscale NC", now=NOW)


def test_fleet_summary_counts_every_node_once(floor):
    s = floor["summary"]
    assert s["total"] == 5
    assert s["counts"] == {
        "completed": 1, "in_progress": 1, "remediation": 1, "not_started": 1, "handed_over": 1,
    }
    # Handed-over nodes finished before they left us, so they count towards done.
    assert s["percent_complete"] == pytest.approx(40.0)
    assert floor["meta"]["cluster"] == "Nscale NC"
    assert floor["meta"]["generated_at"] == "2026-10-13T12:00:00+00:00"
    assert set(floor["meta"]["definitions"]) == set(dt.STATES)


def test_units_follow_the_record_then_the_leftovers(floor):
    assert [u["id"] for u in floor["units"]] == ["su20", "su19", dt.UNASSIGNED_UNIT]
    su19 = floor["units"][1]
    assert su19["status"] == "handed_over" and su19["handed_over_at"] == "2026-10-13"


def test_rows_follow_floor_order_and_racks_hold_sorted_nodes(floor):
    su20 = floor["units"][0]
    # The hierarchy lists row b before row a, and the floor order is what the tracker draws.
    assert [r["label"] for r in su20["rows"]] == ["Row B", "Row A"]
    row_a = su20["rows"][1]
    v20 = row_a["racks"][0]
    assert v20["rack"] == "V20" and v20["stage1"] == "done" and v20["stage2"] == "pending"
    assert [n["node"] for n in v20["nodes"]] == ["gpu1", "gpu2"]  # by rack unit
    assert v20["counts"]["completed"] == 1 and v20["counts"]["in_progress"] == 1


def test_a_node_needing_attention_says_why(floor):
    gpu3 = floor["units"][0]["rows"][0]["racks"][0]["nodes"][0]
    assert gpu3["state"] == "remediation"
    assert gpu3["reason"] == "Stage 1 failed: fabric not up"
    assert gpu3["slurm"] == "IDLE+DRAIN"


def test_a_host_placed_nowhere_is_named_not_counted(floor):
    # On NC this is the app server, which the inventory lists beside the trays: it is not on the
    # floor, so it is out of every total -- and listed, so the omission is visible.
    assert floor["unplaced"] == ["loose"]
    names = [n["node"] for u in floor["units"] for r in u["rows"] for k in r["racks"] for n in k["nodes"]]
    assert "loose" not in names


def test_stage2_still_loading_reads_as_not_yet_run():
    payload = {"nodes": [_node("n1", "r01")], "rows": [{"rack": "r01"}]}
    out = dt.build_dataset(payload, {"nodes": [_stage("n1", "done", "unknown")]}, now=NOW)
    node = out["units"][0]["rows"][0]["racks"][0]["nodes"][0]
    assert node["stage2"] == "pending" and node["state"] == "in_progress"


def test_handed_over_sus_leave_every_step_denominator(floor):
    steps = {s["id"]: s for s in floor["steps"]}
    assert steps["node_stage1"]["total"] == 4  # gpu4 is in handed-over SU19; "loose" is off the floor
    assert steps["node_stage1"]["done"] == 2 and steps["node_stage1"]["fail"] == 1
    assert steps["rack_stage1"]["total"] == 3  # U19 is out
    assert steps["rack_stage1"]["done"] == 1 and steps["rack_stage1"]["fail"] == 1
    assert steps["rack_stage2"]["unit"] == "racks"


def test_a_pool_cluster_is_one_pool():
    payload = {"rack_mapping_mode": "pool", "nodes": [_node("n1", None), _node("n2", None)]}
    out = dt.build_dataset(payload, {"nodes": []}, now=NOW)
    assert [u["label"] for u in out["units"]] == ["Pool"]
    assert out["units"][0]["total"] == 2 and out["unplaced"] == []


def test_a_cluster_without_an_su_model_is_one_group():
    payload = {"nodes": [_node("n1", "r01")], "rows": [{"rack": "r01"}]}
    out = dt.build_dataset(payload, {"nodes": []}, now=NOW)
    assert [u["label"] for u in out["units"]] == ["All racks"]


# ---------------------------------------------------------------- the static renderer


def test_embed_keeps_the_dataset_data(floor):
    page = f"<html><body>{dt.DATA_ELEMENT}<div id=root></div></body></html>"
    hostile = {**floor, "meta": {**floor["meta"], "cluster": "</script><script>alert(1)</script>"}}
    out = dt.embed(page, hostile)
    assert out.count("</script>") == 1  # only the data element's own close tag
    body = out.split('type="application/json">', 1)[1].rsplit("</script>", 1)[0]
    assert json.loads(body)["meta"]["cluster"] == "</script><script>alert(1)</script>"


def test_embed_refuses_a_page_without_the_element():
    with pytest.raises(dt.SiteBundleMissing):
        dt.embed("<html></html>", {})


def test_render_site_is_one_page_with_the_data(tmp_path, floor):
    (tmp_path / "index.html").write_text(f"<html>{dt.DATA_ELEMENT}</html>")
    page = dt.render_site(floor, tmp_path)
    assert '"cluster":"Nscale NC"' in page and "null</script>" not in page


def test_render_site_says_when_the_bundle_was_not_built(tmp_path, floor):
    with pytest.raises(dt.SiteBundleMissing):
        dt.render_site(floor, tmp_path)


# ---------------------------------------------------------------- the handover date is on the record


def test_handed_over_at_round_trips_and_must_be_a_date():
    su = models.ClusterScalableUnit.from_dict({"id": "su19", "racks": ["U19"], "handed_over_at": " 2026-10-13 "})
    assert su.handed_over_at == "2026-10-13" and su.validate() == []
    bad = models.ClusterScalableUnit.from_dict({"id": "su19", "racks": ["U19"], "handed_over_at": "last week"})
    assert bad.validate() and "YYYY-MM-DD" in bad.validate()[0]
    cluster = models.Cluster.from_dict({
        "id": "c", "name": "C", "scalable_units": [{"id": "su19", "racks": ["U19"], "handed_over_at": "13/10"}],
    })
    assert any("handed_over_at" in p for p in cluster.validate())


def test_status_payload_carries_the_handover(monkeypatch):
    from api import dashboard

    cluster = models.Cluster.from_dict({
        "id": "c", "name": "C",
        "scalable_units": [{"id": "su19", "label": "SU19", "racks": ["U19"], "handed_over_at": "2026-10-13"}],
    })
    platform = models.Platform.from_dict({"id": "p1", "name": "P1"})
    monkeypatch.setattr("catalog.context.resolve", lambda *a, **k: (None, cluster, platform))
    payload = {"rows": [{"rack": "U19"}]}
    dashboard._merge_scalable_units_into_status_payload(payload)
    assert payload["scalable_units"][0]["handed_over_at"] == "2026-10-13"


# ---------------------------------------------------------------- the routes


class _Snap:
    def __init__(self, payload):
        self.payload = payload


def _quiet(monkeypatch, route, payload):
    monkeypatch.setattr(route, "get_cluster_snapshot", lambda _d, wait=False: _Snap(payload))
    monkeypatch.setattr(route, "_ensure_rack_status_background_thread", lambda: None)
    monkeypatch.setattr(route, "_start_cmsh_rack_list_warm_thread", lambda: None)
    monkeypatch.setattr(route, "_resolve_log_dir", lambda _raw: PurePosixPath("/logs"))
    monkeypatch.setattr(route, "_merge_naming_into_status_payload", lambda _p: None)
    monkeypatch.setattr(
        route.node_stages, "node_stages",
        lambda pairs, logs, wait_for_bringup=False: {"nodes": [_stage(n, "done", "done") for n, _r in pairs]},
    )


def test_route_serves_the_dataset(monkeypatch):
    import app as app_module
    from api import deployment_tracker as route

    _quiet(monkeypatch, route, {"nodes": [_node("gpu1", "V20")], "rows": [{"rack": "V20"}]})
    r = app_module.app.test_client().get("/api/deployment-tracker")
    if r.status_code in (401, 403):
        pytest.skip("route requires a session in this app configuration")
    body = r.get_json()
    assert r.status_code == 200 and body["ok"]
    assert body["summary"]["counts"]["completed"] == 1


def test_route_says_retry_while_the_inventory_loads(monkeypatch):
    import app as app_module
    from api import deployment_tracker as route

    _quiet(monkeypatch, route, {})
    r = app_module.app.test_client().get("/api/deployment-tracker")
    if r.status_code in (401, 403):
        pytest.skip("route requires a session in this app configuration")
    assert r.status_code == 503 and r.headers.get("Retry-After") == "30"


def test_site_route_serves_the_page_as_a_download(monkeypatch, tmp_path):
    import app as app_module
    from api import deployment_tracker as route

    (tmp_path / "index.html").write_text(f"<html>{dt.DATA_ELEMENT}</html>")
    monkeypatch.setattr(dt, "SITE_DIR", tmp_path)
    _quiet(monkeypatch, route, {"nodes": [_node("gpu1", "V20")], "rows": [{"rack": "V20"}]})
    r = app_module.app.test_client().get("/api/deployment-tracker/site.html")
    if r.status_code in (401, 403):
        pytest.skip("route requires a session in this app configuration")
    assert r.status_code == 200 and r.mimetype == "text/html"
    assert "deployment-tracker.html" in r.headers["Content-Disposition"]
    assert "gpu1" in r.get_data(as_text=True)


def test_the_built_page_is_a_single_file():
    """When the frontend has been built, the page must stand alone -- sibling assets render white
    from ``file://``. Skipped in CI, which does not build the frontend."""
    index = dt.SITE_DIR / "index.html"
    if not index.is_file():
        pytest.skip("tracker page not built in this tree")
    html = index.read_text(encoding="utf-8")
    assert dt.DATA_ELEMENT in html
    assert 'src="./assets' not in html and 'href="./assets' not in html
    assert [p.name for p in dt.SITE_DIR.iterdir()] == ["index.html"]
