# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Preparing a node for a P2P peer: correct what a reimage wiped, invent nothing, refuse the rest.

Author: Rafael M. Koike

``prepare-peer-node.sh`` runs as root, so its refusals matter as much as its fixes. It may add a
local passwd entry that shadows a directory account's wrong primary group -- the fix that brought
767 reimaged Nscale NC nodes back -- but never creates an account, never takes a different uid, never
rewrites an existing local entry, and never writes an entry nsswitch would ignore. Everything it
touches is a parameter, and these tests use values that are deliberately not Nscale's.

``/etc`` lives in a sysroot under tmp; ``getent`` resolves local files first and then a stand-in
directory, as nsswitch "files sss ldap" does; ownership and sudo are recorded, not performed.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "p2p" / "scripts" / "prepare-peer-node.sh"
USER, UID, GROUP, GID = "svc", "4100", "p2pgrp", "4200"

STUBS = {
    # files first, then the directory -- the precedence the local-entry fix depends on.
    "getent": '#!/bin/bash\ncase "$1" in\n'
              '  passwd) grep -h "^$2:" "$NVIS_PREP_SYSROOT/etc/passwd" "$STUB_DIRECTORY" 2>/dev/null | head -1'
              ' | grep . ;;\n'
              '  group) case " $STUB_GROUPS " in *" $2 "*) exit 0;; *) exit 2;; esac ;;\n'
              'esac\n',
    "stat": '#!/bin/bash\necho "$STUB_STAT"\n',
    "chown": '#!/bin/bash\necho "chown $*" >> "$STUB_LOG"\n',
    "chmod": '#!/bin/bash\necho "chmod $*" >> "$STUB_LOG"\n',
    "install": '#!/bin/bash\necho "install $*" >> "$STUB_LOG"\n',
    "find": '#!/bin/bash\n[ -n "$STUB_FOREIGN" ] && echo "$1/storage/metadata/LOCK"\nexit 0\n',
    "sudo": '#!/bin/bash\necho "sudo $*" >> "$STUB_LOG"\n[ "$STUB_SUDO" = yes ]\n',
}


@pytest.fixture
def node(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in STUBS.items():
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    etc = tmp_path / "sysroot" / "etc"
    etc.mkdir(parents=True)
    (etc / "passwd").write_text("root:x:0:0:root:/root:/bin/bash\n")
    (etc / "shadow").write_text("root:*:20000:0:99999:7:::\n")
    (etc / "nsswitch.conf").write_text("passwd:     files sss ldap\ngroup:      files sss ldap\n")
    directory = tmp_path / "directory-passwd"
    # The directory gives the account the right uid but the wrong primary group.
    directory.write_text(f"{USER}:*:{UID}:9999:{USER}:/home/{USER}:/bin/bash\n")
    containers = tmp_path / "containers"
    containers.mkdir()
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("NVIS_", "STUB_"))},
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "NVIS_SERVICE_USER": USER, "NVIS_SERVICE_UID": UID, "NVIS_SERVICE_GROUP": GROUP, "NVIS_SERVICE_GID": GID,
        "NVIS_P2P_BASE": str(tmp_path / "peer"),
        "NVIS_P2P_CONTAINER_DIR": str(containers),
        "NVIS_PREP_SYSROOT": str(tmp_path / "sysroot"),
        "STUB_DIRECTORY": str(directory),
        "STUB_GROUPS": f"{GID} 9999",
        "STUB_STAT": f"root:{GROUP}:775",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "STUB_SUDO": "yes",
        "STUB_FOREIGN": "",
    }
    return {"env": env, "etc": etc, "tmp": tmp_path, "containers": containers, "directory": directory}


def _prep(node, **overrides) -> subprocess.CompletedProcess:
    # check=False: refusals (exit 2/3) are outcomes these tests assert on.
    return subprocess.run(["bash", str(SCRIPT)], env={**node["env"], **overrides},
                          capture_output=True, text=True, check=False)


def _report(res) -> dict[str, str]:
    assert res.returncode == 0, res.stdout + res.stderr
    words = res.stdout.split()
    assert words[0] == "PREP"
    return dict(w.split("=", 1) for w in words[2:])


def _calls(node) -> list[str]:
    log = node["tmp"] / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


class TestIdentity:
    def test_a_wrong_primary_group_gets_a_local_entry_that_shadows_the_directory(self, node):
        r = _report(_prep(node))
        assert (r["passwd"], r["uid"], r["gid"]) == ("added", UID, GID)
        assert f"{USER}:x:{UID}:{GID}::/home/{USER}:/bin/bash" in (node["etc"] / "passwd").read_text()
        shadow = [ln for ln in (node["etc"] / "shadow").read_text().splitlines() if ln.startswith(f"{USER}:")]
        assert len(shadow) == 1
        _, pw, changed, *_ = shadow[0].split(":")
        assert pw == "!"  # key-only, as through the directory
        assert abs(int(changed) - int(time.time()) // 86400) <= 1  # today, not a copied constant
        assert (node["etc"] / "passwd.bak-nvis-p2p").is_file()

    def test_a_second_run_changes_nothing(self, node):
        _prep(node)
        before = (node["etc"] / "passwd").read_text()
        assert _report(_prep(node))["passwd"] == "ok"
        assert (node["etc"] / "passwd").read_text() == before

    def test_an_account_already_right_is_left_alone(self, node):
        node["directory"].write_text(f"{USER}:*:{UID}:{GID}::/home/{USER}:/bin/bash\n")
        assert _report(_prep(node))["passwd"] == "ok"
        assert USER not in (node["etc"] / "passwd").read_text()

    @pytest.mark.parametrize(
        "setup, reason",
        [
            (lambda n: n["directory"].write_text(""), "account-svc-missing"),
            (lambda n: n["directory"].write_text(f"{USER}:*:5555:9999::/h:/bin/bash\n"), "uid-mismatch"),
            (lambda n: (n["etc"] / "passwd").write_text(f"root:x:0:0::/root:/bin/bash\n{USER}:x:{UID}:9999::/h:/bin/sh\n"),
             "not-rewriting-a-local-account"),
            (lambda n: (n["etc"] / "nsswitch.conf").write_text("passwd: sss files\n"), "nsswitch-does-not-consult-files-first"),
            (lambda n: n["env"].update(STUB_GROUPS="9999"), f"group-{GID}-missing"),
        ],
    )
    def test_an_identity_it_cannot_correct_safely_is_refused_untouched(self, node, setup, reason):
        setup(node)
        before = (node["etc"] / "passwd").read_text()
        res = _prep(node)
        assert res.returncode == 3 and reason in res.stderr
        assert (node["etc"] / "passwd").read_text() == before
        assert not any(c.startswith(("chown", "chmod", "install")) for c in _calls(node))

    @pytest.mark.parametrize(
        "overrides",
        [{"NVIS_SERVICE_UID": "abc"}, {"NVIS_SERVICE_GID": "-1"}, {"NVIS_SERVICE_USER": "Bad User"},
         {"NVIS_P2P_CONTAINER_MODE": "rwx"}],
    )
    def test_malformed_parameters_are_rejected(self, node, overrides):
        assert _prep(node, **overrides).returncode == 2

    @pytest.mark.parametrize("missing", ["NVIS_SERVICE_USER", "NVIS_SERVICE_UID", "NVIS_SERVICE_GROUP", "NVIS_SERVICE_GID"])
    def test_the_identity_is_never_defaulted(self, node, missing):
        env = {k: v for k, v in node["env"].items() if k != missing}
        res = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)
        assert res.returncode != 0 and missing in res.stderr


class TestDirectories:
    def test_a_container_directory_already_right_is_left_alone(self, node):
        assert _report(_prep(node))["containers"] == "ok"
        assert not any(c.startswith(("chown", "chmod")) for c in _calls(node))

    def test_a_container_directory_with_the_wrong_owner_is_fixed(self, node):
        r = _report(_prep(node, STUB_STAT="root:root:700", NVIS_P2P_CONTAINER_OWNER=f"svc:{GROUP}"))
        assert r["containers"] == "fixed(root:root:700)"
        assert f"chown svc:{GROUP} {node['containers']}" in _calls(node)
        assert f"chmod 775 {node['containers']}" in _calls(node)

    def test_a_missing_container_directory_is_created(self, node):
        node["containers"].rmdir()
        assert _report(_prep(node))["containers"] == "created"
        assert f"install -d -o root -g {GROUP} -m 775 {node['containers']}" in _calls(node)

    def test_peer_state_owned_by_someone_else_is_reclaimed(self, node):
        peer = Path(node["env"]["NVIS_P2P_BASE"])
        assert _report(_prep(node))["state"] == "absent"
        peer.mkdir()
        assert _report(_prep(node))["state"] == "ok"
        assert _report(_prep(node, STUB_FOREIGN="1"))["state"] == "fixed"
        assert f"chown -R {USER}:{GROUP} {peer}" in _calls(node)


class TestAllowlistAndSudo:
    def test_without_a_config_there_is_no_allowlist_step(self, node):
        assert _report(_prep(node))["allowlist"] == "n/a"

    def test_the_account_is_added_once(self, node):
        cfg = node["tmp"] / "rogueprocess.py"
        cfg.write_text('legitProcessNames = []\nlegitUsers = [\n    "root",\n]\n')
        assert _report(_prep(node, NVIS_P2P_BCM_ROGUEPROCESS_CONFIG=str(cfg)))["allowlist"] == "added"
        assert f'legitUsers = [\n    "{USER}",\n    "root",' in cfg.read_text()
        assert (node["tmp"] / "rogueprocess.py.bak-nvis-p2p").is_file()
        assert _report(_prep(node, NVIS_P2P_BCM_ROGUEPROCESS_CONFIG=str(cfg)))["allowlist"] == "present"
        assert cfg.read_text().count(f'"{USER}"') == 1

    def test_an_unrecognised_config_is_reported_not_guessed(self, node):
        cfg = node["tmp"] / "rogueprocess.py"
        cfg.write_text("legit_users = ['root']\n")
        assert _report(_prep(node, NVIS_P2P_BCM_ROGUEPROCESS_CONFIG=str(cfg)))["allowlist"] == "anchor-missing"
        assert cfg.read_text() == "legit_users = ['root']\n"

    def test_a_configured_but_missing_config_is_refused(self, node):
        res = _prep(node, NVIS_P2P_BCM_ROGUEPROCESS_CONFIG=str(node["tmp"] / "absent.py"))
        assert res.returncode == 3 and "rogueprocess-config-missing" in res.stderr

    @pytest.mark.parametrize("sudo", ["yes", "no"])
    def test_the_accounts_sudo_is_reported(self, node, sudo):
        assert _report(_prep(node, STUB_SUDO=sudo))["sudo"] == sudo
