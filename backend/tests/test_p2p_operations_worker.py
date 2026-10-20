# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The P2P operations worker: what it runs, in what order, and what it refuses to start.

Author: Rafael M. Koike

``deploy/p2p/scripts/operations-worker.py`` turns queued requests into fleet transfers. Three
properties are pinned because each was learned on a live rollout:

* **A stale pin is fixed before the transfer, not discovered by it.** Seeds and nodes verify against
  the cluster manifest; a manifest still carrying an old pin made every node reject the image, but
  only after a preheat and a whole wave. The worker syncs first and refuses if that does not settle it.
* **Requests run in the order they were enqueued** -- not filename order, where a distribute
  enqueued after a sync sorted ahead of it.
* **One worker at a time.** A cron tick that finds a rollout running leaves the queue alone instead
  of starting a second fleet-wide transfer on the same seeds.

The catalog scripts are stand-ins that record when they ran and what the manifest said at that moment.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[2] / "deploy" / "p2p" / "scripts" / "operations-worker.py"
_spec = importlib.util.spec_from_file_location("p2p_operations_worker", WORKER)
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)

OLD, NEW = "a" * 64, "b" * 64


def _manifest(path: Path, pins: dict[str, str]) -> None:
    path.write_text(json.dumps({"images": [{"filename": f, "sha256": s} for f, s in pins.items()]}))


@pytest.fixture
def site(tmp_path):
    """An install root with stand-in catalog scripts, a manifest, a roster and an ops dir."""
    scripts = tmp_path / "root" / "current" / "scripts"
    scripts.mkdir(parents=True)
    calls = tmp_path / "calls.log"
    manifest = tmp_path / "images.json"
    for name in ("preheat-catalog-image.sh", "rollout-catalog-image-fleet.sh", "audit-fleet.sh"):
        # Record the call and the pin the manifest held at that moment.
        (scripts / name).write_text(
            "#!/bin/bash\n"
            f"pin=$(python3 -c \"import json;print(([i['sha256'] for i in json.load(open('{manifest}'))['images']"
            f" if i['filename']=='nemo.sqsh'] or ['-'])[0])\" 2>/dev/null)\n"
            f'echo "{name} $1 $pin" >> {calls}\n'
        )
    roster = tmp_path / "roster.nodes"
    roster.write_text("n1\nn2\n")
    ops = tmp_path / "ops"
    ops.mkdir()
    return {"root": tmp_path / "root", "calls": calls, "manifest": manifest, "roster": roster, "ops": ops,
            "log": tmp_path / "op.log"}


def _inner(site, catalog_pin: str | None) -> dict:
    inner = {"install_root": str(site["root"]), "catalog_path": str(site["manifest"]),
             "container_path": "/cluster/containers", "rollout": {"wave_size": 50}}
    if catalog_pin is not None:
        inner["catalog"] = [{"order": 1, "name": "NeMo", "filename": "nemo.sqsh", "bytes": 5, "sha256": catalog_pin}]
    return inner


def _calls(site) -> list[str]:
    return site["calls"].read_text().splitlines() if site["calls"].exists() else []


class TestEnsurePinned:
    @pytest.fixture
    def sync_spy(self, monkeypatch, site):
        calls = []

        def _spy(inner, roster, log, *, writes=NEW, rc=0):
            calls.append(inner)
            if writes:
                _manifest(site["manifest"], {"nemo.sqsh": writes})
            return rc

        monkeypatch.setattr(worker, "_sync_catalog", _spy)
        return calls

    def test_without_an_authored_catalog_there_is_nothing_to_check(self, site, sync_spy):
        _manifest(site["manifest"], {"nemo.sqsh": OLD})
        assert worker._ensure_pinned(_inner(site, None), ["nemo.sqsh"], site["roster"], site["log"]) == 0
        assert sync_spy == []

    def test_a_manifest_that_agrees_is_left_alone(self, site, sync_spy):
        _manifest(site["manifest"], {"nemo.sqsh": NEW})
        assert worker._ensure_pinned(_inner(site, NEW), ["nemo.sqsh"], site["roster"], site["log"]) == 0
        assert sync_spy == []

    def test_a_stale_manifest_is_synced_first(self, site, sync_spy):
        _manifest(site["manifest"], {"nemo.sqsh": OLD})
        assert worker._ensure_pinned(_inner(site, NEW), ["nemo.sqsh"], site["roster"], site["log"]) == 0
        assert len(sync_spy) == 1
        assert "syncing first" in site["log"].read_text()

    def test_an_image_missing_from_the_manifest_counts_as_stale(self, site, sync_spy):
        _manifest(site["manifest"], {})
        assert worker._ensure_pinned(_inner(site, NEW), ["nemo.sqsh"], site["roster"], site["log"]) == 0
        assert len(sync_spy) == 1

    def test_a_manifest_still_wrong_after_sync_is_refused(self, site, monkeypatch):
        _manifest(site["manifest"], {"nemo.sqsh": OLD})
        monkeypatch.setattr(worker, "_sync_catalog", lambda inner, roster, log: 0)  # writes nothing
        assert worker._ensure_pinned(_inner(site, NEW), ["nemo.sqsh"], site["roster"], site["log"]) == 3
        assert "still disagrees" in site["log"].read_text()

    def test_a_failed_sync_is_the_operation_failing(self, site, monkeypatch):
        _manifest(site["manifest"], {"nemo.sqsh": OLD})
        monkeypatch.setattr(worker, "_sync_catalog", lambda inner, roster, log: 1)
        assert worker._ensure_pinned(_inner(site, NEW), ["nemo.sqsh"], site["roster"], site["log"]) == 1

    def test_an_unpinned_image_is_refused_without_syncing(self, site, sync_spy):
        assert worker._ensure_pinned(_inner(site, ""), ["nemo.sqsh"], site["roster"], site["log"]) == 2
        assert sync_spy == []


class TestHandle:
    def _req(self, site, action, catalog_pin):
        return {"id": f"{action}-1", "action": action, "images": ["nemo.sqsh"], "request": _inner(site, catalog_pin)}

    def _args(self, site):
        return type("A", (), {"roster": str(site["roster"])})()

    def test_a_distribute_with_a_stale_manifest_preheats_against_the_new_pin(self, site):
        """End to end with the real sync (no app-server address, so it rewrites only the manifest)."""
        _manifest(site["manifest"], {"nemo.sqsh": OLD})
        assert worker._handle(self._req(site, "distribute", NEW), self._args(site), site["log"]) == 0
        calls = _calls(site)
        assert calls[0] == f"preheat-catalog-image.sh nemo.sqsh {NEW}"
        assert all(c.endswith(NEW) for c in calls)
        assert json.loads(site["manifest"].read_text())["images"][0]["sha256"] == NEW

    @pytest.mark.parametrize("action", ["persist", "distribute", "repair"])
    def test_an_unpinned_image_never_reaches_a_transfer(self, site, action):
        _manifest(site["manifest"], {"nemo.sqsh": OLD})
        assert worker._handle(self._req(site, action, ""), self._args(site), site["log"]) == 2
        assert _calls(site) == []


def _enqueue(ops: Path, op_id: str, action: str, started: str, **extra) -> Path:
    path = ops / f"{op_id}.json"
    path.write_text(json.dumps({"id": op_id, "action": action, "images": [], "started_at": started,
                                "finished_at": None, **extra}))
    return path


class TestQueue:
    def test_requests_run_in_enqueue_order_not_filename_order(self, site):
        ops = site["ops"]
        _enqueue(ops, "sync-x", "sync", "2026-10-07T10:00:00Z")
        _enqueue(ops, "distribute-y", "distribute", "2026-10-07T10:05:00Z")
        _enqueue(ops, "audit-z", "verify", "2026-10-07T09:00:00Z", finished_at="2026-10-07T09:10:00Z")
        (ops / "torn.json").write_text("{not json")
        assert [p.name for p in worker._pending(ops)] == ["sync-x.json", "distribute-y.json"]

    def test_a_busy_worker_leaves_the_queue_to_it(self, site):
        ops = site["ops"]
        path = _enqueue(ops, "verify-1", "verify", "2026-10-07T10:00:00Z",
                        request={"install_root": str(site["root"]), "rollout": {}})
        argv = ["--ops-dir", str(ops), "--roster", str(site["roster"])]
        with open(ops / ".worker.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            assert worker.main(argv) == 0
            assert json.loads(path.read_text())["finished_at"] is None
            assert _calls(site) == []
        # Once the running worker is gone, the next tick takes the request.
        assert worker.main(argv) == 0
        done = json.loads(path.read_text())
        assert done["state"] == "succeeded" and done["finished_at"]
        assert _calls(site)[0].startswith("audit-fleet.sh")


class TestRolloutEnv:
    def test_the_stall_window_reaches_the_rollout(self):
        env = worker._rollout_env({}, {"stall_seconds": 300}, "/opt/nvis-p2p")
        assert env["NVIS_P2P_ROLLOUT_STALL_SECONDS"] == "300"

    def test_a_request_queued_before_the_field_existed_gets_the_default(self):
        assert worker._rollout_env({}, {}, "/opt/nvis-p2p")["NVIS_P2P_ROLLOUT_STALL_SECONDS"] == "600"

    def test_zero_is_passed_through_as_off(self):
        assert worker._rollout_env({}, {"stall_seconds": 0}, "/opt/nvis-p2p")["NVIS_P2P_ROLLOUT_STALL_SECONDS"] == "0"

    def test_the_download_limit_reaches_the_rollout_and_defaults_to_an_hour(self):
        assert worker._rollout_env({}, {"download_timeout_seconds": 900}, "/r")["NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS"] == "900"
        assert worker._rollout_env({}, {}, "/r")["NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS"] == "3600"

    def test_the_verify_rate_reaches_the_rollout_and_defaults_to_100(self):
        assert worker._rollout_env({}, {"verify_mb_per_second": 250}, "/r")["NVIS_P2P_ROLLOUT_VERIFY_MBPS"] == "250"
        assert worker._rollout_env({}, {}, "/r")["NVIS_P2P_ROLLOUT_VERIFY_MBPS"] == "100"
        assert worker._rollout_env({}, {"verify_mb_per_second": 0}, "/r")["NVIS_P2P_ROLLOUT_VERIFY_MBPS"] == "0"


class TestSyncRefreshesNodesWithClush:
    """The node refresh is one clush fan-out from the appserver, not an ssh loop (Nscale NC)."""

    def test_the_refresh_is_a_bounded_clush_over_the_roster(self, tmp_path, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(worker, "_run", lambda cmd, log, env, timeout: calls.append(cmd) or 0)
        roster = tmp_path / "fleet.nodes"
        roster.write_text("n1\nn2\n")
        inner = {"catalog_path": str(tmp_path / "images.json"), "appserver_ip": "10.0.0.22",
                 "catalog": [{"order": 1, "name": "x", "filename": "x.sqsh", "bytes": 1, "sha256": "a" * 64}]}
        assert worker._sync_catalog(inner, roster, tmp_path / "log") == 0
        (cmd,) = calls
        assert cmd[:4] == ["clush", "--nostdin", "--hostfile", str(roster)]
        assert {"-t", "-u", "-f"} <= set(cmd)
        assert cmd[-1].startswith("curl -s -m10 http://10.0.0.22:12130/.nvis-p2p/catalog/images.tsv ")
