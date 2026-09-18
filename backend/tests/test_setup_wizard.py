# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The setup wizard: when it opens, what it proposes, and how it stores a key.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Three subjects, each with a failure that would be found by a customer rather than by us.

**The trigger.** "No project exists" is the obvious condition and it is wrong: seeding creates
a `default` project on first boot, so a wizard waiting for an empty table never appears -- and
the opposite mistake, keying on the seeded project, shows the wizard to every deployment that
upgrades. The matrix below is the whole contract.

**The naming proposal.** A template derived from one hostname that cannot parse that hostname
is worse than no proposal: it produces node targets that do not resolve, which reads as a
network fault. Every candidate is round-tripped, and the four deployment families from
`test_nvis_id_templates.py` are the fixture table.

**The key file.** Mode 0600 across *several* writes, because a single-write test passes against
the broken implementation -- the mode is right on create and lost on rewrite
(`identity-auth-traps.mdc` §3).
"""

from __future__ import annotations

import os
import stat

import naming_propose
import pytest
import setup_state
import ssh_key_store
from catalog import models
from catalog.store import TABLE_PROJECTS, get_repo

# --- the trigger --------------------------------------------------------------------------------


@pytest.fixture
def clean_catalog(tmp_path, monkeypatch):
    """A catalog of this test's own, holding only what seeding creates.

    **Not the session's store, and deleting what the test added is not enough.** The trigger is a
    question *about* the catalog -- "has anyone configured a project here?" -- so any other test
    that leaves an operator-created project behind makes the answer "yes" for every test after
    it. Three of these passed alone and failed in the suite for exactly that reason.

    Dropping the repo and bumping the generation is the part that is easy to miss: the store is a
    module-level singleton and `catalog.context` caches overlays on the generation counter, so
    repointing `NVIS_CATALOG_FILE` on its own leaves the previous file open. Same reasoning as
    `conftest.catalog_from_fixture`, which is where this was learned.
    """
    from catalog import store as catalog_store

    monkeypatch.setenv("NVIS_CATALOG_FILE", str(tmp_path / "catalog.json"))
    catalog_store.set_repo(None)
    catalog_store._bump_generation()

    repo = get_repo()
    repo.set_meta(setup_state.META_KEY, {"done": False, "project": ""})
    yield repo, []

    catalog_store.set_repo(None)
    catalog_store._bump_generation()


def _add_project(repo, created, doc_id: str, source: str) -> None:
    project = models.Project(id=doc_id, name=doc_id, customer="ACME", source=source)
    repo.upsert(TABLE_PROJECTS, project.to_dict())
    created.append(doc_id)


def test_a_seeded_project_alone_still_needs_setup(clean_catalog, monkeypatch):
    """The trap: seeding always creates one, so an empty-table check never fires."""
    repo, created = clean_catalog
    monkeypatch.delenv("NVIS_FORCE_SETUP_WIZARD", raising=False)
    _add_project(repo, created, "seeded-only", "seed")

    status = setup_state.status()
    assert status["required"] is True
    assert status["reason"] == "no-project-configured"


def test_an_operator_created_project_means_setup_happened(clean_catalog, monkeypatch):
    """What stops an upgraded deployment being shown a wizard it does not need."""
    repo, created = clean_catalog
    monkeypatch.delenv("NVIS_FORCE_SETUP_WIZARD", raising=False)
    _add_project(repo, created, "nscale-tx", "operator")

    status = setup_state.status()
    assert status["required"] is False
    assert status["reason"] == "project-configured"
    assert "nscale-tx" in status["projects"]


def test_the_recorded_flag_stops_a_second_prompt(clean_catalog, monkeypatch):
    """A wizard that finished without creating a project must not reopen every login."""
    repo, created = clean_catalog
    monkeypatch.delenv("NVIS_FORCE_SETUP_WIZARD", raising=False)
    _add_project(repo, created, "seeded-only", "seed")
    setup_state.mark_complete("seeded-only")

    assert setup_state.status()["reason"] == "already-completed"
    assert setup_state.required() is False


def test_the_force_flag_beats_everything(clean_catalog, monkeypatch):
    """`cli.py serve --setup-wizard`: deliberate, so it outranks both other signals."""
    repo, created = clean_catalog
    _add_project(repo, created, "nscale-tx", "operator")
    setup_state.mark_complete("nscale-tx")
    monkeypatch.setenv("NVIS_FORCE_SETUP_WIZARD", "1")

    status = setup_state.status()
    assert status["required"] is True
    assert status["reason"] == "forced"


def test_reopen_undoes_completion(clean_catalog, monkeypatch):
    repo, created = clean_catalog
    monkeypatch.delenv("NVIS_FORCE_SETUP_WIZARD", raising=False)
    _add_project(repo, created, "seeded-only", "seed")
    setup_state.mark_complete("seeded-only")
    assert setup_state.required() is False

    setup_state.reopen()
    assert setup_state.required() is True


def test_auth_check_carries_the_flag(authed_client):
    """The SPA gates on this field; a separate endpoint would be a second round trip."""
    body = authed_client.get("/api/auth-check").get_json()
    assert "setup_required" in body
    assert isinstance(body["setup_required"], bool)
    # Beside the field it mirrors, which is the reason it lives here.
    assert "must_change_password" in body


# --- the naming proposal ------------------------------------------------------------------------


#: The four families whose templates are pinned in `test_nvis_id_templates.py`, each as the one
#: hostname an operator would type, with the rack id and tray the site actually uses.
FAMILIES = [
    ("nscale", "ink50-dh01-h15-15", "ink50-dh01-h15", "15"),
    ("bright", "p5-r03-ct12", "p5-r03", "12"),
    ("cbr", "CBR01-DH01-SVR-CN010213", "CBR01-DH01-SVR-CN0102", "13"),
    ("boston", "bos01-a05-11-s1-dgx-01-c01", "bos01-a05", "01"),
]


@pytest.mark.parametrize("family,hostname,rack_id,tray", FAMILIES)
def test_the_real_reading_is_among_the_proposals(family, hostname, rack_id, tray):
    """Offered, not necessarily first -- and the difference is the honest part.

    Three of the four rank first. Boston does not, and cannot: nothing in
    `bos01-a05-11-s1-dgx-01-c01` says `-s1-dgx-` is fixed infrastructure text rather than data,
    so "the rack is `bos01-a05`" and "the rack is `bos01-a05-11-s1-dgx-01`" both parse the
    example. Ranking it first would need a heuristic fitted to these four samples, which would
    then mis-order the fifth site. The wizard shows each candidate's parsed rack and tray and
    lets the operator pick the one they recognise.
    """
    result = naming_propose.propose(hostname)
    assert result["ok"], result.get("error")
    readings = [(c["parsed"]["rack_id"], c["parsed"]["tray"]) for c in result["candidates"]]
    assert (rack_id, tray) in readings, f"{family}: {readings}"


@pytest.mark.parametrize("family,hostname,rack_id,tray", FAMILIES)
def test_every_proposal_parses_the_example_it_came_from(family, hostname, rack_id, tray):
    """The contract that makes a proposal safe to accept without reading the regex.

    A candidate that cannot parse its own example would build node targets that do not resolve,
    and that failure surfaces as `Name or service not known` from pdsh -- which reads as a
    network problem, days later.
    """
    import nvis_id_templates as nid

    for candidate in naming_propose.propose(hostname)["candidates"]:
        naming = nid.NvisNaming.from_templates(
            candidate["rack_id_template"],
            candidate["ct_id_template"],
            candidate["nvsw_id_template"],
        )
        hit = naming.matcher().match_hostname(hostname)
        assert hit is not None, f"{family}: {candidate['ct_id_template']} cannot parse {hostname}"
        assert hit.rack_id == candidate["parsed"]["rack_id"]


def test_a_hostname_with_one_number_is_refused_with_a_reason():
    """It cannot separate a rack from a tray, and saying so beats proposing nonsense."""
    result = naming_propose.propose("headnode01")
    assert result["ok"] is False
    assert "rack" in result["error"] and "tray" in result["error"]


def test_a_hostname_with_no_numbers_is_refused():
    result = naming_propose.propose("login")
    assert result["ok"] is False
    assert "digits" in result["error"]


def test_the_ambiguity_is_stated_rather_than_hidden():
    """A proposal presented as an answer is how a site ends up with the wrong templates."""
    result = naming_propose.propose("ink50-dh01-h15-15")
    assert result.get("caveat")


# --- the key file -------------------------------------------------------------------------------


_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"


@pytest.fixture
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NVIS_SSH_KEY_DIR", str(tmp_path / "ssh"))
    return tmp_path / "ssh"


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_the_key_is_owner_only_across_repeated_writes(key_dir):
    """Several writes, because a single-write test passes on the broken implementation.

    `O_CREAT` does not change the mode of a file that already exists, so a key created once at
    0644 -- by hand, by an earlier version, by a restore -- would stay 0644 through every
    rewrite while a one-shot test reported success. This is trap 3 in `identity-auth-traps.mdc`
    arriving in a second module.
    """
    path, _ = ssh_key_store.write_key("nscale-tx", "gb300", _KEY)
    assert _mode(path) == 0o600

    # The hostile starting state: a key already on disk, world-readable.
    os.chmod(path, 0o644)
    for _ in range(3):
        again, _fp = ssh_key_store.write_key("nscale-tx", "gb300", _KEY)
        assert again == path
        assert _mode(again) == 0o600, "a rewrite left the key readable by everyone"


def test_the_directory_is_owner_only_too(key_dir):
    """A 0600 file in a loose directory still leaks its name, and ssh reads it regardless."""
    path, _ = ssh_key_store.write_key("p", "c", _KEY)
    assert _mode(os.path.dirname(path)) == 0o700


def test_a_public_key_is_refused_by_name(key_dir):
    """The commonest paste mistake, and otherwise it fails much later inside ssh."""
    with pytest.raises(ssh_key_store.KeyRejected) as exc:
        ssh_key_store.write_key("p", "c", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 rafael@nvidia")
    assert "public key" in str(exc.value)


def test_junk_is_refused(key_dir):
    with pytest.raises(ssh_key_store.KeyRejected):
        ssh_key_store.write_key("p", "c", "hunter2")
    with pytest.raises(ssh_key_store.KeyRejected):
        ssh_key_store.write_key("p", "c", "   ")


def test_an_id_cannot_walk_out_of_the_key_directory(key_dir):
    for bad in ("../../etc", "a/b", "Upper", ""):
        with pytest.raises(ssh_key_store.KeyRejected):
            ssh_key_store.write_key(bad, "c", _KEY)


def test_the_material_never_leaves_the_module(key_dir, caplog):
    """It is stored and fingerprinted; it is never logged, returned or echoed."""
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nSUPERSECRETMATERIAL\n-----END OPENSSH PRIVATE KEY-----"
    with caplog.at_level("DEBUG"):
        path, fp = ssh_key_store.write_key("p", "c", secret)

    assert "SUPERSECRETMATERIAL" not in caplog.text
    assert "SUPERSECRETMATERIAL" not in path
    assert "SUPERSECRETMATERIAL" not in fp
    assert fp.startswith("sha256:")
    described = ssh_key_store.describe(path)
    assert "SUPERSECRETMATERIAL" not in repr(described)
    assert described["mode"] == "0o600"
