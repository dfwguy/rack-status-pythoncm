# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The app writes one layer of the script tree and refuses the rest.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The shared default set and the platform overrides are the product's: shipped, upgradeable, and
shared by every project on the server, so editing one in place changes another customer's run.
A project's own directory exists precisely so that edit has somewhere safe to go.

**Immutability is a property of the layer, not a flag on a record**, and that distinction is
what this file pins. A ``protected`` field on the Platform document would let an operator clear
it and start editing the shared files -- the outcome being prevented -- so there is nothing to
toggle. Platform *records* stay editable, because tuning ``tool_param_defaults`` per site is
routine.

Nothing in the app writes the tree today except the bootstrap, which composes a project
directory. That is why the rule is asserted here rather than only exercised: the next writer --
an in-UI editor, an upload, a generated preset -- should inherit it instead of rediscovering it.
"""

from __future__ import annotations

import pytest
import script_tree


@pytest.fixture(autouse=True)
def _tree(monkeypatch, tmp_path):
    """A tree on this machine, with all four kinds of layer present."""
    base = tmp_path / "deploy"
    tree = base / "scripts"
    for layer in ("multi-platform", "gb300-nvl72", "project-nscale-tx", "custom"):
        (tree / layer).mkdir(parents=True)
    (base / "bin" / "scripts").mkdir(parents=True)
    import host_exec

    monkeypatch.setenv("RACK_EXEC_MODE", "local")
    monkeypatch.setenv("RACK_REMOTE_BASE_PATH", str(base))
    # `host_exec` caches the role roots behind its own init, so clearing only script_tree's
    # cache leaves `legacy_dir()` pointing at whatever base a previous test set.
    host_exec.invalidate_connection()
    script_tree.clear_cache()
    yield base
    host_exec.invalidate_connection()
    script_tree.clear_cache()


def test_the_app_writes_the_project_and_custom_layers_only(_tree):
    """Two writable layers, and the split is about *ownership*, not convenience.

    `project-<id>/` is one site's copy; `custom/` is where a script an operator registers
    lands, shared across projects. The shipped layers are the product's -- changing one for
    everyone is a merge request, not a file write.
    """
    tree = f"{_tree}/scripts"
    assert script_tree.is_writable(f"{tree}/project-nscale-tx")
    assert script_tree.is_writable(f"{tree}/project-nscale-tx/sysinfo.sh")
    assert script_tree.is_writable(f"{tree}/custom/my_probe.sh")
    assert not script_tree.is_writable(f"{tree}/multi-platform/sysinfo.sh")
    assert not script_tree.is_writable(f"{tree}/gb300-nvl72/sysinfo.sh")


def test_the_legacy_flat_directory_counts_as_the_product_s(_tree):
    """The safe direction: unmigrated, it is shared by every project, so a write is widest.

    Calling it writable would mean the guarantee silently does not hold on exactly the
    deployments that have not migrated yet.
    """
    assert not script_tree.is_writable(f"{_tree}/bin/scripts/sysinfo.sh")


@pytest.mark.parametrize(
    "layer,expected",
    [
        ("project-nscale-tx", "project"),
        ("multi-platform", "multi-platform"),
        ("gb300-nvl72", "platform"),
        ("custom", "custom"),
    ],
)
def test_a_path_is_attributed_to_its_layer(_tree, layer, expected):
    assert script_tree.layer_of(f"{_tree}/scripts/{layer}/x.sh") == expected


def test_a_path_outside_the_tree_is_not_writable_either(_tree):
    for outside in ("/etc/passwd", f"{_tree}/var/logs/x", "", "relative/path"):
        assert not script_tree.is_writable(outside)


def test_the_refusal_says_what_to_do_instead(_tree):
    """A gate that says what is wrong without saying what to do is one operators route around."""
    with pytest.raises(script_tree.ImmutableLayer) as exc:
        script_tree.assert_writable(f"{_tree}/scripts/multi-platform/sysinfo.sh")

    message = str(exc.value)
    assert "shared by every project" in message
    assert "Update scripts" in message, "the refusal must name the action that does work"
    # And where an operator-added script belongs, which is the question that produced `custom/`.
    assert "custom" in message
    assert "merge request" in message, "changing the shipped copy for everyone has a route too"


def test_assert_writable_permits_the_project_directory(_tree):
    script_tree.assert_writable(f"{_tree}/scripts/project-nscale-tx/custom.sh")


def test_the_layout_names_the_writable_directory(_tree):
    """So a UI can mark files editable rather than learning it from a refusal.

    Asserted as "the reported directory is one `is_writable` accepts", rather than against a
    literal: which project is active depends on the catalog, and a test that pins the name ends
    up asserting the fixture instead of the property.
    """
    writable = str(script_tree.describe()["writable_dir"])
    assert writable.startswith(f"{_tree}/scripts/{script_tree.PROJECT_PREFIX}")
    assert script_tree.is_writable(writable)


def test_immutability_is_not_a_platform_record_flag():
    """Pinned as a decision, because the obvious implementation is the wrong one.

    If this ever becomes a field on the Platform document, an operator can clear it and edit the
    shared `gb300-nvl72/` scripts for every project on the server -- which is what the layered
    tree exists to prevent. The Platform record stays editable for its own fields.
    """
    from catalog import models

    assert not hasattr(models.Platform, "protected"), (
        "immutability belongs to the script layer (script_tree.is_writable), not to the "
        "Platform document, which operators must stay able to edit"
    )


def test_switching_cluster_re_resolves_the_script_root(monkeypatch, tmp_path):
    """The product bug this file's own fixture ordering turned up.

    `host_exec` caches the role roots, and the cluster-switch hook cleared the naming, job-env
    and status caches but not that one. Measured before the fix: after switching to a cluster
    with a different `remote_base_path`, `_eget` returned the new base while
    `role_root("scripts")` still returned the old one — so scripts resolved against the
    *previous customer's* paths. `cluster-deployments.mdc` exists to prevent exactly that.
    """
    import host_exec

    monkeypatch.setenv("RACK_EXEC_MODE", "local")
    monkeypatch.setenv("RACK_REMOTE_BASE_PATH", str(tmp_path / "cluster-a"))
    host_exec.invalidate_connection()
    script_tree.clear_cache()
    assert script_tree.legacy_dir().startswith(str(tmp_path / "cluster-a"))

    monkeypatch.setenv("RACK_REMOTE_BASE_PATH", str(tmp_path / "cluster-b"))
    # What the switch hook now does, and the assertion is that it is enough.
    host_exec.invalidate_connection()
    script_tree.clear_cache()
    assert script_tree.legacy_dir().startswith(str(tmp_path / "cluster-b"))


def test_the_cluster_switch_hook_drops_both_caches():
    """Asserted against the source, because the hook's body is the contract.

    A test that only checks the resolver would pass while the hook forgot to call it — which is
    the state this was found in.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text()
    body = source[source.index("def _apply_catalog_active_to_environ") :][:6000]
    assert "host_exec.invalidate_connection()" in body
    assert "script_tree.clear_cache()" in body
