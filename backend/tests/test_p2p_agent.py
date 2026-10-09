# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""nvis-p2p-agent: the pull agent that converges a node on its desired-state document.

The agent is exercised for real -- a local HTTP server serves the document and a stub ``dfget``
stands in for the Dragonfly peer (it copies a source file to ``-O``, or sleeps until killed) -- so
install, cancel, the sha256 gate, backoff and the generation-bump retry are all observed end to end.
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

AGENT_PATH = Path(__file__).resolve().parents[2] / "deploy" / "p2p" / "scripts" / "nvis-p2p-agent.py"
spec = importlib.util.spec_from_file_location("nvis_p2p_agent", AGENT_PATH)
agent_mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = agent_mod  # dataclasses resolve their module by name
assert spec.loader is not None
spec.loader.exec_module(agent_mod)

NODE = "sys1-gso1-p-phy-gpu50-su25"


class _Doc(http.server.BaseHTTPRequestHandler):
    body: bytes | None = None
    etag = '"1"'

    def do_GET(self):
        cls = type(self)
        if cls.body is None:
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("If-None-Match") == cls.etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("ETag", cls.etag)
        self.end_headers()
        self.wfile.write(cls.body)

    def log_message(self, *_):
        pass


@pytest.fixture
def server():
    handler = type("Doc", (_Doc,), {"body": None, "etag": '"1"'})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield handler, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _publish(handler, doc: dict, etag: str) -> None:
    handler.body, handler.etag = json.dumps(doc).encode(), etag


@pytest.fixture
def node(tmp_path):
    """A peer root with a stub dfget: copy $NVIS_TEST_SRC to -O, or sleep when $NVIS_TEST_HANG is set."""
    base = tmp_path / "containers" / ".nvis-p2p"
    (base / "bin").mkdir(parents=True)
    (base / "run").mkdir()
    src = tmp_path / "src.sqsh"
    src.write_bytes(b"x" * 4096)
    dfget = base / "bin" / "dfget"
    dfget.write_text(
        "#!/bin/sh\n"
        'out=""; while [ $# -gt 0 ]; do [ "$1" = "-O" ] && out="$2"; shift; done\n'
        f'if [ -e "{tmp_path}/hang" ]; then exec sleep 600; fi\n'
        f'cp "{src}" "$out"\n'
    )
    dfget.chmod(0o755)
    textfile = tmp_path / "textfile"
    textfile.mkdir()
    return base, src, textfile, tmp_path


def _doc(src: Path, generation: int = 1, sha: str | None = None, origin: str = "http://10.0.0.1:12130") -> dict:
    return {
        "schema_version": 1, "generation": generation, "node": NODE, "origin": origin,
        "images": [{"filename": "probe.sqsh", "sha256": sha or hashlib.sha256(src.read_bytes()).hexdigest(),
                    "bytes": src.stat().st_size}],
    }


def _agent(base, textfile, url):
    return agent_mod.Agent(base, NODE, f"{url}/.nvis-p2p/desired/{NODE}.json", textfile, log=lambda _m: None)


def _poll_until(agent, predicate, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        agent.poll()
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"condition not reached; tracks={ {n: vars(t) for n, t in agent.tracks.items()} }")


class TestTheContract:
    def test_a_valid_document_parses_in_priority_order(self):
        doc = {"schema_version": 1, "generation": 3, "node": NODE, "origin": "http://h:1",
               "images": [{"filename": "b.sqsh", "sha256": "a" * 64, "bytes": 1, "priority": 2},
                          {"filename": "a.sqsh", "sha256": "b" * 64, "bytes": 2, "priority": 1}]}
        desired = agent_mod.parse_desired(doc, NODE)
        assert desired.generation == 3 and [i.filename for i in desired.images] == ["a.sqsh", "b.sqsh"]

    @pytest.mark.parametrize("mutate, why", [
        (lambda d: d.update(schema_version=2), "schema_version"),
        (lambda d: d.update(node="other-node"), "not"),
        (lambda d: d["images"][0].update(filename="../etc/passwd"), "bad filename"),
        (lambda d: d["images"][0].update(sha256="ABC"), "sha256"),
        (lambda d: d["images"][0].update(bytes=0), "bytes"),
        (lambda d: d.update(origin="file:///etc"), "origin"),
        (lambda d: d["images"].append(dict(d["images"][0])), "duplicate"),
    ])
    def test_anything_unexpected_is_refused(self, mutate, why):
        doc = {"schema_version": 1, "generation": 1, "node": NODE, "origin": "http://h:1",
               "images": [{"filename": "a.sqsh", "sha256": "a" * 64, "bytes": 1}]}
        mutate(doc)
        with pytest.raises(agent_mod.DesiredError, match=why):
            agent_mod.parse_desired(doc, NODE)


class TestConvergence:
    def test_installs_with_receipt_and_reports_ready(self, server, node):
        handler, url = server
        base, src, textfile, _ = node
        _publish(handler, _doc(src), '"1"')
        agent = _agent(base, textfile, url)
        _poll_until(agent, lambda: agent.tracks.get("probe.sqsh") and agent.tracks["probe.sqsh"].state == "ready")
        target = base.parent / "probe.sqsh"
        assert target.read_bytes() == src.read_bytes()
        sha = hashlib.sha256(src.read_bytes()).hexdigest()
        assert (base / "receipts" / "probe.sqsh.sha256").read_text() == f"{sha}  {target}\n"
        prom = (textfile / "nvis_p2p_agent.prom").read_text()
        assert 'nvis_p2p_image_state{filename="probe.sqsh",state="ready"} 1' in prom
        assert 'nvis_p2p_image_bytes{filename="probe.sqsh"} 4096' in prom

    def test_no_document_means_idle(self, server, node):
        _handler, url = server
        base, _src, textfile, _ = node
        agent = _agent(base, textfile, url)
        agent.poll()
        assert agent.download is None and agent.tracks == {}
        assert 'nvis_p2p_agent_up{generation="0"' in (textfile / "nvis_p2p_agent.prom").read_text()

    def test_removing_an_image_mid_download_cancels_it(self, server, node):
        handler, url = server
        base, src, textfile, tmp = node
        (tmp / "hang").touch()
        _publish(handler, _doc(src), '"1"')
        agent = _agent(base, textfile, url)
        _poll_until(agent, lambda: agent.download is not None and agent.download.proc is not None)
        _publish(handler, {**_doc(src, generation=2), "images": []}, '"2"')
        _poll_until(agent, lambda: agent.download is None)
        assert not (base / "staging" / "probe.sqsh.partial").exists()
        assert not (base.parent / "probe.sqsh").exists()

    def test_a_wrong_hash_is_quarantined_and_backs_off_then_a_generation_bump_retries(self, server, node):
        handler, url = server
        base, src, textfile, _ = node
        _publish(handler, _doc(src, sha="0" * 64), '"1"')
        agent = _agent(base, textfile, url)
        _poll_until(agent, lambda: agent.tracks.get("probe.sqsh") and agent.tracks["probe.sqsh"].state == "backoff")
        assert agent.tracks["probe.sqsh"].failures == 1
        assert "sha256" in agent.tracks["probe.sqsh"].last_error
        assert list((base / "quarantine").glob("probe.sqsh.badsha.*"))
        assert not (base.parent / "probe.sqsh").exists()
        _publish(handler, _doc(src, generation=2), '"2"')  # the operator re-pins and retries
        _poll_until(agent, lambda: agent.tracks["probe.sqsh"].state == "ready")
        assert agent.tracks["probe.sqsh"].failures == 0

    def test_an_already_installed_image_is_adopted_without_a_download(self, server, node):
        handler, url = server
        base, src, textfile, tmp = node
        (base.parent / "probe.sqsh").write_bytes(src.read_bytes())
        (tmp / "hang").touch()  # a download would hang; adoption must not start one
        _publish(handler, _doc(src), '"1"')
        agent = _agent(base, textfile, url)
        agent.poll()
        assert agent.download is None and agent.tracks["probe.sqsh"].state == "ready"
        assert (base / "receipts" / "probe.sqsh.sha256").is_file()

    def test_a_malformed_document_keeps_the_last_good_one(self, server, node):
        handler, url = server
        base, src, textfile, _ = node
        _publish(handler, _doc(src), '"1"')
        agent = _agent(base, textfile, url)
        _poll_until(agent, lambda: agent.tracks.get("probe.sqsh") and agent.tracks["probe.sqsh"].state == "ready")
        handler.body, handler.etag = b"{not json", '"2"'
        agent.poll()
        assert agent.desired is not None and agent.desired.generation == 1
        assert agent.fetch_errors == 1


def test_metrics_land_in_a_handed_over_file_when_the_directory_is_not_writable(server, node):
    """node_exporter's textfile directory is root's; the installer hands over one file instead."""
    _handler, url = server
    base, _src, textfile, _ = node
    prom = textfile / "nvis_p2p_agent.prom"
    prom.write_text("stale\n" * 200)
    textfile.chmod(0o555)
    try:
        agent = _agent(base, textfile, url)
        agent.poll()
        text = prom.read_text()
        assert text.startswith("# HELP nvis_p2p_agent_up") and "stale" not in text
    finally:
        textfile.chmod(0o755)
