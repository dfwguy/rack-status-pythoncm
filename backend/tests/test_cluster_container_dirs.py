# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Where a cluster keeps its container images is a property of the cluster, not of a script.

Launchers hard-coded ``/raid/data`` (Boston/Mayo DGX) and ``/tmp/containers`` (Nscale TX) as the
node-local image directory. Nscale NC has neither: its compute nodes cache images under
``/opt/containers`` and have no ``/raid``, so single-rack NeMo died at ``mkdir -p /raid/data`` before
Slurm saw the job. The directory is now ``connection.node_container_dir`` (and the shared store
``connection.shared_container_dir``), projected by the overlay to the env the launchers read.
"""
from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import validation_presets
from catalog import context, models

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"
HELPER = SCRIPTS / "lib" / "node_images.inc.sh"


def _cluster(**connection):
    return models.Cluster.from_dict({"id": "c1", "name": "C1", "connection": connection})


class TestTheFields:
    def test_defaults_are_blank(self):
        c = _cluster().connection
        assert (c.shared_container_dir, c.node_container_dir) == ("", "")

    def test_they_round_trip(self):
        c = _cluster(shared_container_dir="/cluster/containers", node_container_dir="/opt/containers")
        conn = c.to_dict()["connection"]
        assert (conn["shared_container_dir"], conn["node_container_dir"]) == (
            "/cluster/containers", "/opt/containers"
        )

    @pytest.mark.parametrize("good", ["/opt/containers", "/opt/containers/", "/raid/data", "/a/b+c/d-e_f.g"])
    def test_plain_absolute_paths_validate(self, good):
        assert not _cluster(node_container_dir=good).connection.validate()

    @pytest.mark.parametrize(
        "bad",
        [
            "opt/containers",        # relative
            "/opt/my containers",    # splits into two words on the remote shell
            "/opt/$(id)",            # substitution
            "/opt/a,b",              # the sbatch --export separator
            "/opt/../etc",           # escapes the directory it names
            "/opt/'x'",
        ],
    )
    def test_paths_a_launcher_cannot_interpolate_safely_are_refused(self, bad):
        problems = _cluster(node_container_dir=bad, shared_container_dir=bad).connection.validate()
        assert any("node_container_dir" in p for p in problems)
        assert any("shared_container_dir" in p for p in problems)


class TestTheOverlayProjection:
    def test_both_dirs_reach_the_launcher_env(self):
        env = context.high_overlay(
            None,
            _cluster(shared_container_dir="/cluster/containers", node_container_dir="/opt/containers"),
            None,
        )
        assert env["RACK_VALIDATION_CONTAINER_ROOT"] == "/cluster/containers"
        assert env["NVIS_NODE_CONTAINER_DIR"] == "/opt/containers"

    def test_blank_dirs_project_nothing(self):
        env = context.high_overlay(None, _cluster(), None)
        assert "RACK_VALIDATION_CONTAINER_ROOT" not in env
        assert "NVIS_NODE_CONTAINER_DIR" not in env


class TestPresetsFollowTheCluster:
    @pytest.fixture
    def nc(self, monkeypatch):
        monkeypatch.setenv("NVIS_NODE_CONTAINER_DIR", "/opt/containers/")

    def test_the_default_is_the_historical_cache(self, monkeypatch):
        monkeypatch.delenv("NVIS_NODE_CONTAINER_DIR", raising=False)
        assert validation_presets.node_container_dir() == validation_presets.LOCAL_CONTAINER_DIR

    def test_the_cluster_value_wins_without_its_trailing_slash(self, nc):
        assert validation_presets.node_container_dir() == "/opt/containers"

    def test_a_local_image_moves_only_its_directory(self, nc):
        assert validation_presets.resolve_local_image("/tmp/containers/nvidia+nemo+26.04.01-arm.sqsh") == (
            "/opt/containers/nvidia+nemo+26.04.01-arm.sqsh"
        )
        assert validation_presets.resolve_local_image(None) is None

    def test_a_request_may_restate_the_cache_but_not_move_it(self, nc):
        assert validation_presets.check_local_container_dir(None) == "/opt/containers"
        assert validation_presets.check_local_container_dir("/opt/containers/") == "/opt/containers"
        with pytest.raises(ValueError, match="/opt/containers"):
            validation_presets.check_local_container_dir("/tmp/containers")

    def test_normalized_options_and_catalogue_carry_the_cluster_cache(self, nc, preset_image_catalog):
        preset = validation_presets.resolve_preset("nemo-26.04.01")
        opts = validation_presets.normalize_options({"scope": "rack"}, preset)
        assert opts["local_container_dir"] == "/opt/containers"
        public = preset.public()
        assert public["defaults"]["local_container_dir"] == "/opt/containers"
        # The catalog names the file; the cluster's node cache is where it lives.
        assert public["image"]["path"] == "/opt/containers/nvidia+nemo+26.04.01-arm.sqsh"


def _p2p_cluster(**connection):
    return models.Cluster.from_dict(
        {"id": "nc", "name": "NC", "p2p_enabled": True, "connection": connection}
    )


class TestP2pDerivesFromTheCluster:
    """The cluster record is the one source of truth for where images live; P2P derives from it."""

    def test_a_p2p_cluster_with_blank_fields_inherits_p2p_paths(self):
        """NC predates the fields: its launchers must find what P2P already distributed."""
        p2p = models.P2pSettings(cluster_id="nc", container_path="/cluster/containers",
                                 peer_root="/opt/containers/.nvis-p2p")
        assert models.node_image_dirs(_p2p_cluster(), p2p) == ("/cluster/containers", "/opt/containers")

    def test_a_p2p_cluster_without_stored_settings_uses_p2p_defaults(self):
        assert models.node_image_dirs(_p2p_cluster(), None) == ("/cluster/containers", "/opt/containers")

    def test_explicit_cluster_fields_win(self):
        p2p = models.P2pSettings(cluster_id="nc", container_path="/old/store")
        cluster = _p2p_cluster(shared_container_dir="/cluster/images/", node_container_dir="/opt/containers")
        assert models.node_image_dirs(cluster, p2p) == ("/cluster/images", "/opt/containers")

    def test_a_non_p2p_cluster_states_only_what_it_says(self):
        assert models.node_image_dirs(_cluster(), None) == ("", "")
        assert models.node_image_dirs(_cluster(node_container_dir="/tmp/containers"), None) == (
            "", "/tmp/containers"
        )

    def test_p2p_settings_align_to_the_cluster(self):
        p2p = models.P2pSettings(cluster_id="nc", container_path="/old/store", peer_root="/old/.nvis-p2p")
        aligned = p2p.aligned_to(
            _p2p_cluster(shared_container_dir="/cluster/containers", node_container_dir="/opt/containers")
        )
        assert (aligned.container_path, aligned.peer_root) == ("/cluster/containers", "/opt/containers/.nvis-p2p")
        assert aligned.to_render_env()["NVIS_PEER_ROOT"] == "/opt/containers/.nvis-p2p"

    def test_blank_cluster_fields_leave_p2p_settings_alone(self):
        p2p = models.P2pSettings(cluster_id="nc", container_path="/x", peer_root="/y/.nvis-p2p")
        aligned = p2p.aligned_to(_p2p_cluster())
        assert (aligned.container_path, aligned.peer_root) == ("/x", "/y/.nvis-p2p")

    def test_a_node_cache_p2p_never_writes_is_refused_on_a_p2p_cluster(self):
        """P2P's node scripts publish to /opt/containers literally; any other cache would send
        launchers to a directory P2P never fills."""
        problems = _p2p_cluster(node_container_dir="/tmp/containers").validate()
        assert any("disagrees with P2P" in p for p in problems)
        assert not _p2p_cluster(node_container_dir="/opt/containers/").validate()
        assert not _cluster(node_container_dir="/tmp/containers").validate()

    def test_the_overlay_marks_a_p2p_cluster_and_exports_its_dirs(self):
        env = context.high_overlay(None, _p2p_cluster(), None)
        assert env["NVIS_NODE_IMAGES_P2P"] == "1"
        assert env["NVIS_NODE_CONTAINER_DIR"] == "/opt/containers"
        assert env["RACK_VALIDATION_CONTAINER_ROOT"] == "/cluster/containers"
        assert "NVIS_NODE_IMAGES_P2P" not in context.high_overlay(None, _cluster(), None)


class TestPresetsOnAP2pCluster:
    @pytest.fixture(autouse=True)
    def p2p(self, monkeypatch):
        monkeypatch.setenv("NVIS_NODE_IMAGES_P2P", "1")
        monkeypatch.setenv("NVIS_NODE_CONTAINER_DIR", "/opt/containers")

    def test_node_local_runs_verify_rather_than_rsync_by_default(self):
        preset = validation_presets.resolve_preset("nemo-26.04.01")
        opts = validation_presets.normalize_options({"scope": "rack", "storage": "local"}, preset)
        assert opts["rsync_to_local"] is False
        defaults = preset.public()["defaults"]
        assert defaults["rsync"] is False
        assert defaults["p2p_managed"] is True

    def test_an_explicit_rsync_is_refused_with_a_pointer_to_p2p(self):
        preset = validation_presets.resolve_preset("nemo-26.04.01")
        with pytest.raises(ValueError, match="P2P"):
            validation_presets.normalize_options({"scope": "rack", "storage": "local", "rsync": True}, preset)


class TestTheLaunchersCarryNoSitePath:
    def _code_lines(self, path: Path):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                yield line

    def test_no_launcher_defaults_to_raid_data(self):
        offenders = [
            path.name
            for path in sorted(SCRIPTS.rglob("*"))
            if path.is_file() and path.suffix in (".sh", ".sbatch")
            and any("/raid/data" in line for line in self._code_lines(path))
        ]
        assert not offenders, f"{offenders} still use /raid/data; it does not exist on NC nodes"

    def test_every_node_cache_fallback_is_the_one_default(self):
        """Scripts fall back to the same directory the backend does, so a manual run and a UI
        launch on an unconfigured cluster agree on where the image is."""
        default = validation_presets.LOCAL_CONTAINER_DIR
        assert f"NVIS_NODE_CONTAINER_DIR_DEFAULT={default}" in HELPER.read_text(encoding="utf-8")
        fallbacks = set()
        for path in SCRIPTS.rglob("*"):
            # The helper defines the default itself (asserted above) and falls back to it by name.
            if path.is_file() and path.suffix in (".sh", ".sbatch") and path != HELPER:
                fallbacks.update(re.findall(r"\$\{NVIS_NODE_CONTAINER_DIR:-([^}]*)\}", path.read_text(encoding="utf-8")))
        assert fallbacks == {default}

    def test_nothing_hardcodes_the_tx_cache_outside_a_fallback(self):
        offenders = []
        for path in SCRIPTS.rglob("*"):
            if not (path.is_file() and path.suffix in (".sh", ".sbatch")):
                continue
            for line in self._code_lines(path):
                scrubbed = line.replace("${NVIS_NODE_CONTAINER_DIR:-/tmp/containers}", "")
                scrubbed = scrubbed.replace("NVIS_NODE_CONTAINER_DIR_DEFAULT=/tmp/containers", "")
                if "/tmp/containers" in scrubbed:
                    offenders.append(f"{path.name}: {line.strip()}")
        assert not offenders, offenders

    def test_single_rack_stages_through_the_helper_and_exports_what_it_staged(self):
        src = (SCRIPTS / "run_singlerack.sh").read_text(encoding="utf-8")
        assert "nvis_stage_node_image run_singlerack" in src
        assert "NVIS_NEMO_CONT=${NVIS_NEMO_CONT}" in src


def _bash_with_fake_pdsh(tmp_path: Path, body: str, *, unreachable: str = "", env: dict | None = None):
    """Run *body* after sourcing the helper, with a ``pdsh`` that runs each host's command locally.

    Each "node" is the same machine, so the cache directory is shared between hosts; that is enough
    to exercise the warm/cold/unreachable branches. A host named in *unreachable* produces no
    output at all -- exactly what pdsh does when ssh to it fails.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "pdsh").write_text(textwrap.dedent(f"""\
        #!/bin/bash
        while (($#)); do case "$1" in -R) shift 2;; -w) hosts="$2"; shift 2;; *) cmd="$1"; shift;; esac; done
        IFS=, read -ra hs <<<"$hosts"
        for h in "${{hs[@]}}"; do
          [[ " {unreachable} " == *" $h "* ]] && {{ echo "pdsh@x: $h: ssh exited with exit code 255" >&2; continue; }}
          bash -c "$cmd" 2>&1 | sed "s/^/$h: /"
          [[ -n "${{PDSH_HOOK:-}}" ]] && bash -c "$PDSH_HOOK"
        done
        """))
    (bindir / "pdsh").chmod(0o755)
    (bindir / "rsync").write_text('#!/bin/bash\necho rsync-called >>"$RSYNC_LOG"\nexec cp "${@: -2:1}" "${@: -1}"\n')
    (bindir / "rsync").chmod(0o755)
    full_env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "RSYNC_LOG": str(tmp_path / "rsync.log")}
    full_env.update(env or {})
    script = f'source "{HELPER}"\n{body}'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full_env, timeout=60, check=False)


@pytest.mark.skipif(subprocess.run(["stat", "-c%s", "/"], capture_output=True, check=False).returncode != 0,
                    reason="needs GNU stat (run in the CI Linux image)")
class TestTheStagingHelper:
    @pytest.fixture
    def image(self, tmp_path):
        src = tmp_path / "src" / "nemo.sqsh"
        src.parent.mkdir()
        src.write_bytes(b"x" * 4096)
        return src

    def test_a_cold_cache_is_filled_and_the_staged_path_exported(self, tmp_path, image):
        cache = tmp_path / "cache"
        r = _bash_with_fake_pdsh(
            tmp_path,
            f'nvis_stage_node_image t n1,n2 "{image}" && echo "CONT=$NVIS_NEMO_CONT"',
            env={"NVIS_NODE_CONTAINER_DIR": f"{cache}/"},
        )
        assert r.returncode == 0, r.stderr
        assert f"CONT={cache}/nemo.sqsh" in r.stdout
        assert (cache / "nemo.sqsh").stat().st_size == 4096

    def test_a_warm_cache_is_not_copied_again(self, tmp_path, image):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "nemo.sqsh").write_bytes(b"y" * 4096)
        r = _bash_with_fake_pdsh(
            tmp_path, f'nvis_stage_node_image t n1,n2 "{image}"', env={"NVIS_NODE_CONTAINER_DIR": str(cache)}
        )
        assert r.returncode == 0, r.stderr
        assert not (tmp_path / "rsync.log").exists()

    def test_an_unreachable_node_fails_the_stage(self, tmp_path, image):
        """A node pdsh never reached prints nothing; counting failures would pass it."""
        r = _bash_with_fake_pdsh(
            tmp_path,
            f'nvis_stage_node_image t n1,n2 "{image}"',
            unreachable="n2",
            env={"NVIS_NODE_CONTAINER_DIR": str(tmp_path / "cache")},
        )
        assert r.returncode == 1
        assert "1/2" in r.stderr

    def test_a_missing_source_fails_before_touching_nodes(self, tmp_path):
        r = _bash_with_fake_pdsh(tmp_path, f'nvis_stage_node_image t n1 "{tmp_path}/nope.sqsh"')
        assert r.returncode == 1
        assert "missing or empty" in r.stderr

    def test_no_hosts_is_a_failure_not_a_vacuous_pass(self, tmp_path, image):
        r = _bash_with_fake_pdsh(tmp_path, f'nvis_stage_node_image t "" "{image}"')
        assert r.returncode == 1
        assert "no nodes" in r.stderr

    @staticmethod
    def _p2p(tmp_path, *, content=b"y" * 4096, pinned_sha=None, pinned_bytes=None, listed=True):
        """A node cache holding nemo.sqsh and a published catalog pinning it (by default correctly)."""
        import hashlib

        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "nemo.sqsh").write_bytes(content)
        config = tmp_path / "config"
        config.mkdir()
        sha = pinned_sha or hashlib.sha256(content).hexdigest()
        size = len(content) if pinned_bytes is None else pinned_bytes
        rows = ["order\tname\tfilename\tbytes\tsha256", f"1\tOther\tother.sqsh\t1\t{'0' * 64}"]
        if listed:
            rows.append(f"2\tNeMo\tnemo.sqsh\t{size}\t{sha}")
        (config / "image-catalog.tsv").write_text("\n".join(rows) + "\n")
        return {"NVIS_NODE_CONTAINER_DIR": str(cache), "NVIS_NODE_IMAGES_P2P": "1",
                "NVIS_NODE_CONFIG_DIR": str(config)}

    def test_on_a_p2p_cluster_an_image_matching_the_catalog_passes(self, tmp_path):
        """Only the basename is used: NC's launch host (the BCM head) cannot see /cluster at all."""
        env = self._p2p(tmp_path)
        r = _bash_with_fake_pdsh(
            tmp_path,
            'nvis_stage_node_image t n1,n2 /cluster/containers/nemo.sqsh && echo "CONT=$NVIS_NEMO_CONT"',
            env=env,
        )
        assert r.returncode == 0, r.stderr
        assert f"CONT={tmp_path}/cache/nemo.sqsh" in r.stdout
        assert not (tmp_path / "rsync.log").exists()

    def test_on_a_p2p_cluster_a_hash_mismatch_fails(self, tmp_path):
        env = self._p2p(tmp_path, pinned_sha="ab" * 32)
        r = _bash_with_fake_pdsh(tmp_path, "nvis_stage_node_image t n1 /x/nemo.sqsh", env=env)
        assert r.returncode == 1
        assert "STAGE_FAIL_sha256=" in r.stderr

    def test_size_mode_skips_the_hash_but_not_the_size(self, tmp_path):
        env = self._p2p(tmp_path, pinned_sha="ab" * 32)
        r = _bash_with_fake_pdsh(tmp_path, "nvis_stage_node_image t n1 /x/nemo.sqsh",
                                 env={**env, "NVIS_NODE_IMAGE_VERIFY": "size"})
        assert r.returncode == 0, r.stderr
        short = tmp_path / "short"
        short.mkdir()
        env = self._p2p(short, pinned_bytes=1)
        r = _bash_with_fake_pdsh(short, "nvis_stage_node_image t n1 /x/nemo.sqsh",
                                 env={**env, "NVIS_NODE_IMAGE_VERIFY": "size"})
        assert r.returncode == 1
        assert "STAGE_FAIL_size=4096" in r.stderr

    def test_on_a_p2p_cluster_an_image_the_catalog_does_not_pin_fails(self, tmp_path):
        env = self._p2p(tmp_path, listed=False)
        r = _bash_with_fake_pdsh(tmp_path, "nvis_stage_node_image t n1 /x/nemo.sqsh", env=env)
        assert r.returncode == 1
        assert "not pinned in the image catalog" in r.stderr

    def test_on_a_p2p_cluster_no_published_catalog_fails_with_how_to_publish(self, tmp_path):
        env = {**self._p2p(tmp_path), "NVIS_NODE_CONFIG_DIR": str(tmp_path / "nowhere")}
        r = _bash_with_fake_pdsh(tmp_path, "nvis_stage_node_image t n1 /x/nemo.sqsh", env=env)
        assert r.returncode == 1
        assert "no image catalog" in r.stderr

    def test_on_a_p2p_cluster_a_missing_image_fails_and_is_never_copied(self, tmp_path):
        env = self._p2p(tmp_path)
        (tmp_path / "cache" / "nemo.sqsh").unlink()
        r = _bash_with_fake_pdsh(tmp_path, "nvis_stage_node_image t n1 /x/nemo.sqsh", env=env)
        assert r.returncode == 1
        assert "STAGE_FAIL_missing" in r.stderr
        assert "P2P campaign" in r.stderr
        assert not (tmp_path / "rsync.log").exists()

    def test_the_config_dir_is_beside_these_scripts(self):
        r = subprocess.run(["bash", "-c", f'source "{SCRIPTS}/lib/node_config.inc.sh"; nvis_node_config_dir'],
                           capture_output=True, text=True, check=False,
                           env={k: v for k, v in os.environ.items() if k != "NVIS_NODE_CONFIG_DIR"})
        assert r.stdout.strip() == str(SCRIPTS / "config")


def test_the_rsync_launchers_refuse_to_copy_on_a_p2p_cluster(tmp_path):
    """Both --rsync paths stop before touching Slurm when P2P owns the node cache."""
    env = {**os.environ, "NVIS_NODE_IMAGES_P2P": "1", "NVIS_NODE_CONTAINER_DIR": "/opt/containers",
           "RACK_JOB_LOG_DIR": str(tmp_path)}
    for script, args in (
        ("run-validation-preset.sh", ["--preset", "nemo-26.04.01", "--nodelist", "n1", "--storage", "local", "--rsync"]),
        ("run-llmb-nccl-container.sh", ["--nodelist", "n1", "--rsync"]),
    ):
        r = subprocess.run(["bash", str(SCRIPTS / script), *args], capture_output=True, text=True,
                           env=env, cwd=tmp_path, timeout=60, check=False)
        assert r.returncode == 2, (script, r.stdout, r.stderr)
        assert "NVIS-P2P distributes its images" in r.stderr, (script, r.stderr)
