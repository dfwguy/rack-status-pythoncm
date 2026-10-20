# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The launch binding that turns a catalog Tool into a widget on a launch surface.

A widget renders from a ``Tool`` document, so these tests guard the two things a document
can get wrong in a way no component would notice: a launcher that does not exist, and an
endpoint that is not ours. Both are operator-editable, which is the point of the feature and
also the reason the shape is pinned here rather than trusted.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import validation_presets
from catalog import models, seed

VALIDATION_SURFACE = "rackDetail.validation"

# The generic poll route every non-`run-*` async action shares. Mirrored by
# GENERIC_JOB_STATUS in frontend/src/lib/toolLaunch.ts.
GENERIC_JOB_STATUS = "/api/reset-job-status"


def _tools() -> list[models.Tool]:
    return [models.Tool.from_dict(raw) for raw in seed.SEED_TOOLS]


def _bound() -> list[models.Tool]:
    return [t for t in _tools() if t.launch.kind != "none"]


# --- the document contract -------------------------------------------------------------


def test_every_seeded_tool_validates():
    for tool in _tools():
        assert tool.validate() == [], f"{tool.id} is not a valid Tool document"


def test_a_document_without_a_launch_block_is_still_valid():
    """Back-compat: a tool written before widgets existed is catalogued, just not launchable."""
    tool = models.Tool.from_dict({"id": "legacy", "name": "Legacy", "script": "legacy.sh"})
    assert tool.validate() == []
    assert tool.launch.kind == "none"


def test_unknown_launch_kind_is_refused():
    tool = models.Tool.from_dict(
        {"id": "x", "name": "X", "script": "x.sh", "launch": {"kind": "curl"}}
    )
    assert any("unknown launch.kind" in p for p in tool.validate())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example/api/run-x-async",  # absolute URL: another origin entirely
        "//evil.example/api/x",  # protocol-relative, still another origin
        "/api/../../etc/passwd",  # traversal out of the API namespace
        "/run-x-async",  # outside /api
        "",  # missing
    ],
)
def test_an_endpoint_that_is_not_ours_is_refused(endpoint):
    """A catalog edit must not be able to aim a button at something we do not serve."""
    tool = models.Tool.from_dict(
        {
            "id": "x",
            "name": "X",
            "script": "x.sh",
            "launch": {"kind": "async-job", "endpoint": endpoint},
        }
    )
    assert any("launch.endpoint" in p for p in tool.validate()), endpoint


def test_a_preset_launcher_needs_a_preset_id():
    tool = models.Tool.from_dict(
        {"id": "x", "name": "X", "script": "x.sh", "launch": {"kind": "validation-preset"}}
    )
    assert any("preset_id" in p for p in tool.validate())


def test_order_ignores_a_boolean():
    """``True`` is an int subclass; without a guard it silently becomes position 1."""
    assert models.Tool.from_dict({"id": "x", "name": "X", "script": "x.sh", "order": True}).order == 100
    assert models.Tool.from_dict({"id": "x", "name": "X", "script": "x.sh", "order": 5}).order == 5


# --- the seeded board ------------------------------------------------------------------


def test_every_preset_binding_names_a_real_recipe():
    """The folded documents carry ids, not copies of the recipe.

    Read the preset ids off the engine rather than typing them: a literal here would pass
    while naming a preset that was renamed, which is the drift this fold exists to remove.
    """
    known = {preset.id for preset in validation_presets.PRESETS}
    bound = {t.launch.preset_id for t in _bound() if t.launch.kind == "validation-preset"}
    assert bound, "no preset-backed tools were seeded"
    assert bound <= known, f"catalog references presets the engine does not define: {bound - known}"


@pytest.mark.parametrize("preset_id", [preset.id for preset in validation_presets.PRESETS])
def test_every_audited_preset_is_reachable_from_some_surface(preset_id):
    """A recipe nobody can press is a recipe that silently stops being run."""
    bound = {t.launch.preset_id for t in _bound() if t.launch.kind == "validation-preset" and t.surfaces}
    assert preset_id in bound, f"preset with no seeded catalog widget: {preset_id}"


def test_the_validation_board_reproduces_the_hand_built_tab():
    """The default board is the buttons the tab already had, in the order it had them.

    This is the migration's contract: turning fixed cards into widgets must not quietly add
    or drop an action. Stage 1 before Stage 2 is operational, not cosmetic.
    """
    board = [
        t.id
        for t in sorted(
            (t for t in _bound() if VALIDATION_SURFACE in t.surfaces),
            key=lambda t: (t.order, t.name),
        )
    ]
    assert board == [
        "preset-nccl",
        "nccl-fullrack-rounds",
        "preset-node-gate",
        "validation-singlerack",
        "preset-nemo",
        "preset-gpt-oss-node",
        "preset-mnubergemm",
        "mnubergemm-stress",
        "nvloom",
        "clusterkit",
        "llmb-nccl",
    ]


def test_a_tool_with_no_surface_is_still_addable_but_off_every_default_board():
    """`surfaces: []` is how a launchable tool stays available without claiming a slot.

    Preflight uses it: the Validation tab devotes a dedicated panel to it because its verdict
    is the output, so a widget there would be a second, poorer button for the same action.
    It stays in the catalog and can be added to any board by hand.
    """
    off_board = {t.id for t in _bound() if not t.surfaces}
    assert "nvlink-preflight" in off_board
    for tool in _bound():
        if not tool.surfaces:
            assert tool.launch.kind != "none"


def test_llama_is_catalogued_but_deliberately_unbound():
    """The rack tab has only ever shown a *disabled* Llama card pointing at multirack.

    Binding it would add a working button under cover of a refactor. Unbound keeps a fresh
    install and an upgraded deployment identical, which is the property that matters.
    """
    llama = next(models.Tool.from_dict(r) for r in seed.SEED_TOOLS if r["id"] == "llama")
    assert llama.launch.kind == "none"
    assert llama not in _bound()


# --- upgrading a catalog that was seeded before widgets existed -------------------------


@pytest.fixture
def repo(tmp_path):
    from catalog.store import TinyDBRepo, set_repo

    r = TinyDBRepo(tmp_path / "catalog.json")
    set_repo(r)
    yield r
    set_repo(None)


def _pre_widget_doc(tool_id: str) -> dict:
    """A Tool document in the shape the store held before launch bindings existed."""
    raw = next(t for t in seed.SEED_TOOLS if t["id"] == tool_id)
    return {k: v for k, v in raw.items() if k not in ("subtitle", "button_label", "icon", "destructive", "order", "launch")}


def test_an_already_seeded_deployment_gains_launch_bindings(repo):
    """Without this, every existing cluster renders an empty board.

    ``migrate`` only inserts tools that do not exist, which is what protects operator edits
    across a boot -- and also what would stop a schema addition ever reaching a deployment
    that has already been seeded. Which is all of them.
    """
    from catalog.store import TABLE_TOOLS

    repo.upsert(TABLE_TOOLS, _pre_widget_doc("nccl-fullrack-rounds"))
    assert models.Tool.from_dict(repo.get(TABLE_TOOLS, "nccl-fullrack-rounds")).launch.kind == "none"

    summary = seed.migrate(repo, env={})

    upgraded = models.Tool.from_dict(repo.get(TABLE_TOOLS, "nccl-fullrack-rounds"))
    assert upgraded.launch.kind == "async-job"
    assert upgraded.launch.endpoint == "/api/run-nccl-fullrack-async"
    assert upgraded.button_label == "Run NCCL"
    assert summary["tools_backfilled"] >= 1


def test_the_backfill_does_not_overwrite_an_operator_edit(repo):
    """A document that already carries the field is the operator's, defaults included.

    The check is "key absent", not "value equals the default": `order: 100` and an unbound
    `launch` are both legitimate choices, and comparing against defaults would silently undo
    them on the next boot.
    """
    from catalog.store import TABLE_TOOLS

    edited = {
        **_pre_widget_doc("nccl-fullrack-rounds"),
        "button_label": "Run our NCCL",
        "order": 1,
        "launch": {"kind": "none"},
        "description": "site-specific wording",
    }
    repo.upsert(TABLE_TOOLS, models.Tool.from_dict(edited).to_dict())

    seed.migrate(repo, env={})

    after = models.Tool.from_dict(repo.get(TABLE_TOOLS, "nccl-fullrack-rounds"))
    assert after.button_label == "Run our NCCL"
    assert after.order == 1
    assert after.launch.kind == "none"
    assert after.description == "site-specific wording"


def test_the_backfill_adds_the_validation_surface_without_removing_one(repo):
    """These three render on the Validation tab today while their documents say networking.

    The placement lived in the component, so nothing kept the two honest. An upgraded
    deployment would lose three buttons unless the old documents are told -- additively, so a
    curated surface list survives.
    """
    from catalog.store import TABLE_TOOLS

    doc = _pre_widget_doc("nvloom")
    doc["surfaces"] = ["rackDetail.networking", "a-site-specific-surface"]
    repo.upsert(TABLE_TOOLS, doc)

    seed.migrate(repo, env={})

    after = models.Tool.from_dict(repo.get(TABLE_TOOLS, "nvloom"))
    assert "rackDetail.validation" in after.surfaces
    assert "a-site-specific-surface" in after.surfaces
    assert "rackDetail.networking" in after.surfaces


def test_the_backfill_is_idempotent(repo):
    """Every boot runs it; the second one must be a no-op or it rewrites the store forever."""
    from catalog.store import TABLE_TOOLS

    repo.upsert(TABLE_TOOLS, _pre_widget_doc("nvloom"))
    seed.migrate(repo, env={})
    assert seed.migrate(repo, env={})["tools_backfilled"] == 0


def test_pre_v2_catalog_restricts_match_all_nemo_tools_once(repo):
    """The safety correction reaches existing catalogs without owning later operator edits."""
    from catalog.store import TABLE_TOOLS

    nemo_ids = {
        "llama",
        "nemo-xrack",
        "preset-llama405b",
        "preset-nemo",
        "validation-singlerack",
    }
    for old in (t for t in seed.SEED_TOOLS if t["id"] in nemo_ids):
        repo.upsert(
            TABLE_TOOLS,
            models.Tool.from_dict({**old, "compatible": {"match": "all"}}).to_dict(),
        )
    repo.set_meta("schema_version", 1)

    summary = seed.migrate(repo, env={})

    for tool_id in nemo_ids:
        upgraded = models.Tool.from_dict(repo.get(TABLE_TOOLS, tool_id))
        assert upgraded.compatible.capabilities == ["nvlink"]
    assert summary["nemo_compatibility_migrated"] == len(nemo_ids)

    edited = repo.get(TABLE_TOOLS, "preset-nemo")
    edited["compatible"] = {"match": "all"}
    repo.upsert(TABLE_TOOLS, edited)
    seed.migrate(repo, env={})
    assert models.Tool.from_dict(repo.get(TABLE_TOOLS, "preset-nemo")).compatible.match == "all"


def test_the_backfill_ignores_a_tool_the_operator_invented(repo):
    from catalog.store import TABLE_TOOLS

    repo.upsert(
        TABLE_TOOLS,
        models.Tool.from_dict({"id": "site-tool", "name": "Site Tool", "script": "s.sh"}).to_dict(),
    )
    seed.migrate(repo, env={})
    after = models.Tool.from_dict(repo.get(TABLE_TOOLS, "site-tool"))
    assert after.name == "Site Tool"
    assert after.launch.kind == "none"


def _routes() -> set[str]:
    """Read the tracked route snapshot rather than importing the app.

    The snapshot is the repo's record of what the URL map contains, so a launch binding is
    checked against the same document a route rename has to update.
    """
    snapshot = Path(__file__).with_name("route_snapshot.json")
    return {row["rule"] for row in json.loads(snapshot.read_text())}


def test_async_launch_endpoints_exist():
    """A widget cannot be bound to a route we do not serve."""
    routes = _routes()
    for tool in _bound():
        if tool.launch.kind in ("async-job", "stream"):
            assert tool.launch.endpoint in routes, f"{tool.id} -> {tool.launch.endpoint}"


def test_async_launch_endpoints_have_a_status_route():
    """Pins the poll convention `statusUrlFor` derives in the frontend.

    Two conventions exist: the `run-*` family has a per-tool `-status` route, everything else
    shares the generic one. A tool bound to a `run-*-async` with no matching `-status` would
    launch and then poll a 404 -- which presents as a job that never finishes, not as an error.
    """
    routes = _routes()
    for tool in _bound():
        if tool.launch.kind != "async-job":
            continue
        endpoint = tool.launch.endpoint
        expected = (
            endpoint.replace("-async", "-status")
            if endpoint.startswith("/api/run-")
            else GENERIC_JOB_STATUS
        )
        assert expected in routes, f"{tool.id}: {endpoint} has no poll route ({expected})"


def test_the_rack_surface_offers_gpt_oss_as_a_single_node_benchmark():
    """A per-surface rule, held as catalog data rather than a component's `if`.

    The rack tab has always gated GPT-OSS to exactly one node while the multirack view fans
    it out; two documents over one recipe is how the catalog expresses that.
    """
    by_id = {t.id: t for t in _bound()}
    rack = by_id["preset-gpt-oss-node"]
    assert rack.surfaces == [VALIDATION_SURFACE]
    assert (rack.launch.min_nodes, rack.launch.max_nodes) == (1, 1)
    assert rack.launch.preset_id == by_id["preset-gpt-oss"].launch.preset_id
    assert by_id["preset-gpt-oss"].launch.max_nodes == 0


def test_selection_bounds_must_be_coherent():
    tool = models.Tool.from_dict(
        {
            "id": "x",
            "name": "X",
            "script": "x.sh",
            "launch": {"kind": "async-job", "endpoint": "/api/run-x-async", "min_nodes": 4, "max_nodes": 2},
        }
    )
    assert any("min_nodes" in p for p in tool.validate())


def test_a_widget_declares_scopes_its_tool_can_actually_run():
    for tool in _bound():
        for scope in tool.launch.scopes:
            assert scope in models.EXEC_TARGETS, f"{tool.id} declares scope {scope!r}"


def test_bound_widgets_round_trip_through_the_store_shape():
    """``to_dict`` is what the API returns; a field the frontend needs must survive it."""
    for tool in _bound():
        again = models.Tool.from_dict(tool.to_dict())
        assert again.launch == tool.launch
        assert (again.subtitle, again.button_label, again.order) == (
            tool.subtitle,
            tool.button_label,
            tool.order,
        )
