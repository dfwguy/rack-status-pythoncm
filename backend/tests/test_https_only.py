# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""HTTPS is the only way this app serves, and one function decides the certificate.

The bug this pins: the container's ENTRYPOINT decided `--certfile` in `sh` from
`RACK_WEB_TLS_CERT`, *before* gunicorn's `on_starting` hook generated a self-signed certificate. So
a first boot with no certificate configured created one and then served plain HTTP beside it.
The certificate is now settled by `startup_gate.ensure_tls()` when gunicorn loads its config file,
which is before the listener exists, and every serving path reads that one answer.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import urllib.error
from pathlib import Path

import pytest
import startup_gate

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def tls_env(tmp_path, monkeypatch):
    """No certificate anywhere: not in the environment, not supplied, not generated."""
    monkeypatch.delenv("RACK_WEB_TLS_CERT", raising=False)
    monkeypatch.delenv("RACK_WEB_TLS_KEY", raising=False)
    monkeypatch.setenv("NVIS_RACKSTATUS_CONFIG", str(tmp_path / "config" / "rackstatus.toml"))
    monkeypatch.setenv("RACK_WEB_TLS_DIR", str(tmp_path / "generated"))
    return tmp_path


def _pair(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / "server.crt", directory / "server.key"
    cert.write_text("cert")
    key.write_text("key")
    return cert, key


def _no_generation(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a certificate was generated although one was available")

    monkeypatch.setattr(startup_gate.subprocess, "run", refuse)


class TestEnsureTlsPicksOneCertificate:
    def test_the_environment_wins(self, tls_env, monkeypatch):
        cert, key = _pair(tls_env / "explicit")
        _pair(tls_env / "config" / "tls")
        monkeypatch.setenv("RACK_WEB_TLS_CERT", str(cert))
        monkeypatch.setenv("RACK_WEB_TLS_KEY", str(key))
        _no_generation(monkeypatch)
        assert startup_gate.ensure_tls() == (str(cert), str(key))

    def test_an_operator_supplied_pair_in_config_tls_is_used(self, tls_env, monkeypatch):
        cert, key = _pair(tls_env / "config" / "tls")
        _no_generation(monkeypatch)
        assert startup_gate.ensure_tls() == (str(cert), str(key))
        # Exported, so the app and the cookie policy see the same answer gunicorn served.
        assert os.environ["RACK_WEB_TLS_CERT"] == str(cert)
        assert os.environ["RACK_WEB_TLS_KEY"] == str(key)

    def test_a_supplied_pair_beats_a_previously_generated_one(self, tls_env, monkeypatch):
        _pair(tls_env / "generated")
        cert, _ = _pair(tls_env / "config" / "tls")
        _no_generation(monkeypatch)
        assert startup_gate.ensure_tls()[0] == str(cert)

    def test_half_a_supplied_pair_is_not_used(self, tls_env, monkeypatch):
        (tls_env / "config" / "tls").mkdir(parents=True)
        (tls_env / "config" / "tls" / "server.crt").write_text("cert only")
        generated_cert, _ = _pair(tls_env / "generated")
        _no_generation(monkeypatch)
        assert startup_gate.ensure_tls()[0] == str(generated_cert)

    def test_with_nothing_available_it_generates_one(self, tls_env, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            calls.append(argv)
            _pair(Path(argv[argv.index("--out-dir") + 1]))

        monkeypatch.setattr(startup_gate.subprocess, "run", fake_run)
        cert, key = startup_gate.ensure_tls()
        assert len(calls) == 1 and calls[0][1].endswith("gen-tls-cert.py")
        assert cert == str(tls_env / "generated" / "server.crt")
        assert key == str(tls_env / "generated" / "server.key")

    @pytest.mark.parametrize("present", ["RACK_WEB_TLS_CERT", "RACK_WEB_TLS_KEY"])
    def test_half_an_environment_pair_is_left_for_the_gate_to_report(
        self, tls_env, monkeypatch, present
    ):
        """Pairing the operator's key with some other certificate would hide the mistake."""
        monkeypatch.setenv(present, "/somewhere/half")
        _no_generation(monkeypatch)
        startup_gate.ensure_tls()
        assert os.environ[present] == "/somewhere/half"
        assert any("half-configured" in f.what for f in startup_gate._tls_faults())


class TestGunicornOwnsTheCertificate:
    def _load_config(self):
        spec = importlib.util.spec_from_file_location("gunicorn_conf_probe", REPO_ROOT / "gunicorn.conf.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_loading_the_config_sets_certfile_and_keyfile(self, tls_env, monkeypatch):
        cert, key = _pair(tls_env / "config" / "tls")
        _no_generation(monkeypatch)
        module = self._load_config()
        assert (module.certfile, module.keyfile) == (str(cert), str(key))

    def test_the_entrypoint_does_not_decide_tls(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        entrypoint = next(line for line in dockerfile.splitlines() if line.startswith("ENTRYPOINT"))
        assert "--certfile" not in entrypoint
        assert "RACK_WEB_TLS_CERT" not in entrypoint

    def test_the_cli_passes_no_certificate_of_its_own(self, monkeypatch):
        """A second source beside gunicorn.conf.py is how the two could disagree."""
        import cli

        captured: list[list[str]] = []
        monkeypatch.setattr(cli.os, "execv", lambda _exe, argv: captured.append(argv))
        monkeypatch.setattr(startup_gate, "prepare", lambda **_k: [])
        monkeypatch.setenv("RACK_WEB_TLS_CERT", "/x/server.crt")
        monkeypatch.setenv("RACK_WEB_TLS_KEY", "/x/server.key")
        cli.main(["serve"])
        argv = captured[0]
        assert "--certfile" not in argv and "--keyfile" not in argv
        assert argv[argv.index("-c") + 1] == "gunicorn.conf.py"


class TestTheContainerIsHttpsOnly:
    def test_the_image_has_a_https_healthcheck(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        assert re.search(r"^HEALTHCHECK .*\n\s+CMD \[\"python3\", \"/app/bin/healthcheck.py\"\]", dockerfile, re.MULTILINE)

    def test_compose_mounts_the_config_directory_read_only(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert "- ./config:/app/config:ro" in compose
        assert "env_file" not in compose, "settings live in config/rackstatus.toml, not an env file"
        assert "RACK_WEB_TLS_CERT" not in compose

    def test_compose_does_not_rename_the_data_volume(self):
        """`name:` on rack-data would move every existing deployment onto an empty volume."""
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        volumes = compose[compose.index("\nvolumes:"):]
        assert not re.search(r"^\s+name:", volumes, re.MULTILINE)

    def test_the_removed_env_file_is_gone(self):
        assert not (REPO_ROOT / "rack-config.env").exists()
        assert "rack-config.env" not in (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")


class TestHealthcheck:
    @pytest.fixture()
    def healthcheck(self):
        spec = importlib.util.spec_from_file_location("healthcheck_probe", REPO_ROOT / "bin" / "healthcheck.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    class _Response:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def test_it_asks_over_https_on_loopback(self, healthcheck, monkeypatch):
        seen = {}

        def fake(url, **kwargs):
            seen["url"] = url
            return self._Response(200)

        monkeypatch.setattr(healthcheck.urllib.request, "urlopen", fake)
        assert healthcheck.probe("5000") is True
        assert seen["url"] == "https://127.0.0.1:5000/api/about"

    @pytest.mark.parametrize(("code", "healthy"), [(401, True), (429, True), (500, False), (503, False)])
    def test_a_4xx_is_alive_and_a_5xx_is_not(self, healthcheck, monkeypatch, code, healthy):
        def fake(url, **_kwargs):
            raise urllib.error.HTTPError(url, code, "x", {}, None)

        monkeypatch.setattr(healthcheck.urllib.request, "urlopen", fake)
        assert healthcheck.probe("5000") is healthy

    def test_no_tls_listener_is_unhealthy(self, healthcheck, monkeypatch):
        def fake(url, **_kwargs):
            raise urllib.error.URLError("wrong version number")

        monkeypatch.setattr(healthcheck.urllib.request, "urlopen", fake)
        assert healthcheck.probe("5000") is False

    def test_main_reads_the_port_from_the_environment(self, healthcheck, monkeypatch):
        ports = []
        monkeypatch.setattr(healthcheck, "probe", lambda port: ports.append(port) or True)
        monkeypatch.setenv("PORT", "5123")
        assert healthcheck.main() == 0
        assert ports == ["5123"]


def test_this_runs_without_the_backend_on_a_special_path():
    """`healthcheck.py` must import with the standard library alone -- it runs in the image."""
    source = (REPO_ROOT / "bin" / "healthcheck.py").read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:import|from) (\w+)", source, re.MULTILINE))
    assert imports <= set(sys.stdlib_module_names) | {"__future__"}
