# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Installing the 15-minute catalog refresh on a node, and the peer deploy that does it.

Author: Rafael M. Koike

``deploy-catalog-refresh-timer-node.sh`` runs the real installer here against stand-ins for
``ssh``, ``scp`` and ``sudo``. The ``ssh`` stand-in joins its arguments into one command line the
way ssh does -- so an empty argument vanishes -- which is the exact behaviour that made the first
version die with ``$2: unbound variable`` whenever the URL was left to the node's env file (the path
every peer redeploy takes). ``sudo`` records the systemd actions and, on ``systemctl start``, runs
the real refresh script, so a successful install proves a refresh actually ran.

Also pinned: the units agree with the paths the installer writes, and the peer deploy reports the
timer's outcome without letting it fail an otherwise healthy peer.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

P2P = Path(__file__).resolve().parents[2] / "deploy" / "p2p"
INSTALLER = P2P / "scripts" / "deploy-catalog-refresh-timer-node.sh"
PEER_DEPLOY = P2P / "scripts" / "deploy-catalog-peer-node.sh"
SERVICE = P2P / "deploy" / "systemd" / "nvis-p2p-catalog-refresh.service"
TIMER = P2P / "deploy" / "systemd" / "nvis-p2p-catalog-refresh.timer"
URL = "http://10.10.20.22:12130/.nvis-p2p/catalog/images.tsv"
SHA = "c" * 64

pytestmark = pytest.mark.skipif(not shutil.which("curl"), reason="the stand-in service runs curl")

SSH = r"""#!/bin/bash
# ssh joins everything after the host into ONE command string for the remote shell.
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -*) shift ;; *) break ;; esac; done
echo "ssh $1" >> "$STUB_LOG"; shift
exec bash -c "$*"
"""
SCP = r"""#!/bin/bash
args=("$@"); n=${#args[@]}; src=${args[$((n-2))]}; dst=${args[$((n-1))]}; dst=${dst#*:}
echo "scp $dst" >> "$STUB_LOG"; mkdir -p "$(dirname "$dst")"; cp "$src" "$dst"
"""
SUDO = r"""#!/bin/bash
[ "$1" = -n ] && shift
echo "sudo $*" >> "$STUB_LOG"
case "$1 $2" in
  "install -m") cp "$4" "$FAKE_ETC/$(basename "$5")" ;;
  "systemctl start")
    [ -n "${STUB_START_FAIL:-}" ] && exit 1
    # The service: the real refresh script, as the unit would run it, against the test origin.
    NVIS_P2P_BASE="$NVIS_P2P_BASE" NVIS_P2P_CATALOG_URL="$STUB_ORIGIN_URL" \
      bash "$NVIS_P2P_BASE/bin/catalog-refresh.sh" >/dev/null ;;
esac
exit 0
"""


@pytest.fixture
def node(tmp_path):
    """A stand-in node (its P2P dir and /etc/systemd/system) reached through stub ssh/scp/sudo."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (("ssh", SSH), ("scp", SCP), ("sudo", SUDO)):
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    base, etc = tmp_path / "node-p2p", tmp_path / "node-etc-systemd"
    (base / "bin").mkdir(parents=True)
    etc.mkdir()
    origin = tmp_path / "origin-images.tsv"
    origin.write_text(f"order\tname\tfilename\tbytes\tsha256\n1\tNeMo\tnemo.sqsh\t100\t{SHA}\n")
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("NVIS_P2P_")},
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "NVIS_P2P_ROOT": str(P2P),
        "NVIS_P2P_BASE": str(base),
        "FAKE_ETC": str(etc),
        "STUB_LOG": str(tmp_path / "calls.log"),
        "STUB_ORIGIN_URL": origin.as_uri(),
    }
    return {"env": env, "base": base, "etc": etc, "log": tmp_path / "calls.log"}


def _install(node, **extra) -> subprocess.CompletedProcess:
    # check=False: refusals are outcomes the tests assert on.
    return subprocess.run(["bash", str(INSTALLER), "n1"], env={**node["env"], **extra},
                          capture_output=True, text=True, check=False)


def _calls(node) -> list[str]:
    return node["log"].read_text().splitlines() if node["log"].exists() else []


def test_a_fresh_install_writes_the_url_installs_the_units_and_proves_a_refresh(node):
    res = _install(node, NVIS_P2P_CATALOG_URL=URL)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip() == "REFRESH_TIMER_READY node=n1 status=CATALOG_REFRESH_UPDATED"
    base = node["base"]
    assert (base / "catalog-refresh.env").read_text() == f"NVIS_P2P_CATALOG_URL={URL}\n"
    script = base / "bin" / "catalog-refresh.sh"
    assert script.read_bytes() == (P2P / "scripts" / "catalog-refresh.sh").read_bytes()
    assert os.access(script, os.X_OK)
    assert not list(base.rglob("*.next"))  # every staged file was moved into place
    assert (node["etc"] / SERVICE.name).read_bytes() == SERVICE.read_bytes()
    assert (node["etc"] / TIMER.name).read_bytes() == TIMER.read_bytes()
    sudo = [c for c in _calls(node) if c.startswith("sudo systemctl")]
    assert sudo == [
        "sudo systemctl daemon-reload",
        "sudo systemctl enable --now nvis-p2p-catalog-refresh.timer",
        "sudo systemctl start nvis-p2p-catalog-refresh.service",
        "sudo systemctl is-active --quiet nvis-p2p-catalog-refresh.timer",
    ]
    assert (base / "catalog" / "images.tsv").is_file()


def test_a_reinstall_reuses_the_url_left_on_the_node(node):
    """The peer deploy's path. ssh drops an empty URL argument entirely; this must still work."""
    env_file = node["base"] / "catalog-refresh.env"
    env_file.write_text(f"NVIS_P2P_CATALOG_URL={URL}\n")
    res = _install(node)  # no NVIS_P2P_CATALOG_URL
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.startswith("REFRESH_TIMER_READY node=n1 status=")
    assert env_file.read_text() == f"NVIS_P2P_CATALOG_URL={URL}\n"


def test_without_a_url_anywhere_nothing_is_installed(node):
    res = _install(node)
    assert res.returncode != 0
    assert "REFRESH_TIMER_READY" not in res.stdout
    assert list(node["etc"].iterdir()) == []
    assert not any(c.startswith("sudo systemctl") for c in _calls(node))


@pytest.mark.parametrize(
    "url",
    [
        "http://10.10.20.22/images.tsv;reboot",
        "http://host/images.tsv $(id)",
        "file:///etc/images.tsv",
        "http://host/catalog.json",
    ],
)
def test_a_bad_url_is_refused_before_the_node_is_touched(node, url):
    res = _install(node, NVIS_P2P_CATALOG_URL=url)
    assert res.returncode == 64
    assert "REFRESH_TIMER_REJECTED node=n1 reason=bad-url" in res.stderr
    assert _calls(node) == []


def test_an_install_whose_first_refresh_fails_is_not_ready(node):
    res = _install(node, NVIS_P2P_CATALOG_URL=URL, STUB_START_FAIL="1")
    assert res.returncode != 0
    assert "REFRESH_TIMER_READY" not in res.stdout


class TestUnitsAgreeWithTheInstaller:
    @staticmethod
    def _settings(unit: Path) -> dict[str, str]:
        return dict(re.findall(r"^(\w+)=(.*)$", unit.read_text(), flags=re.MULTILINE))

    def test_the_service_runs_what_the_installer_installs_as_the_service_user(self):
        base = re.search(r"base=\$\{NVIS_P2P_BASE:-([^}]+)\}", INSTALLER.read_text()).group(1)
        s = self._settings(SERVICE)
        assert s["Type"] == "oneshot"
        assert (s["User"], s["Group"]) == ("nvis-p2p", "cmods")
        assert s["EnvironmentFile"] == f"{base}/catalog-refresh.env"
        assert s["ExecStart"] == f"{base}/bin/catalog-refresh.sh"

    def test_the_timer_fires_every_quarter_hour_spread_over_two_minutes(self):
        t = self._settings(TIMER)
        assert t["OnCalendar"] == "*:00/15"
        assert t["RandomizedDelaySec"] == "2min"
        assert t["FixedRandomDelay"] == "true"  # stable per-node offset: the period stays 15 minutes
        assert "OnBootSec" in t
        assert t["WantedBy"] == "timers.target"


class TestPeerDeployHook:
    """The peer deploy installs the timer, reports how it went, and never fails a healthy peer on it."""

    @pytest.fixture
    def hook(self, tmp_path):
        text = PEER_DEPLOY.read_text()
        start = text.index("# Keep this node's catalog pins current")
        end = text.index("\n", text.index('echo "DEPLOY_READY', start))
        root = tmp_path / "root"
        (root / "scripts").mkdir(parents=True)
        stub = root / "scripts" / "deploy-catalog-refresh-timer-node.sh"

        def run(installer_body: str) -> subprocess.CompletedProcess:
            stub.write_text("#!/bin/bash\n" + installer_body)
            stub.chmod(0o755)
            prelude = f'root={root}; node=n1; su=19; ip=10.0.0.1; ready_images=0; missing_bytes=0\n'
            return subprocess.run(["bash", "-c", prelude + text[start:end]],
                                  capture_output=True, text=True, check=False)

        return run

    def test_a_ready_timer_is_reported(self, hook):
        res = hook('echo "REFRESH_TIMER_READY node=$1 status=CATALOG_REFRESH_CURRENT"\n')
        assert res.returncode == 0
        assert res.stdout.strip().endswith("catalog_refresh=CATALOG_REFRESH_CURRENT")
        assert res.stdout.startswith("DEPLOY_READY node=n1 ")

    def test_a_failed_timer_install_is_reported_but_the_peer_stays_ready(self, hook):
        res = hook("exit 1\n")
        assert res.returncode == 0
        assert res.stdout.startswith("DEPLOY_READY node=n1 ")
        assert res.stdout.strip().endswith("catalog_refresh=not-installed")
