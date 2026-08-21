# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Every read endpoint answers without a server error, with the execution layer stubbed.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The `api/` modules are ~7,000 statements of HTTP handlers and were the least covered code in the
backend -- `api/exports.py` at 8.5%, `api/logs.py` at 11.6%, `api/rack_power.py` at 12.4%. Almost
none of that is business logic: it is argument parsing, validation, the shape of the JSON, and the
error paths. All of it runs on any request, and none of it was being exercised.

So this walks the URL map and asks each read endpoint the only question that is both cheap and
worth asking: does it answer at all? A 4xx is fine -- a handler is entitled to reject a request with
no parameters. A 5xx is not: it means the handler raised before it could form an opinion, which is
the failure this catches and which no other test in the suite would.

WHY THE STUB MATTERS. These handlers reach a cluster. Without stubbing they would try to ssh to a
head node, run cmsh, or fan pdsh across a rack -- from a unit test. `_no_execution` replaces the two
functions everything funnels through (`host_exec.run_on_host` / `popen_on_host`, per
core-designs.mdc §1) so a handler that tries gets an empty, successful-looking result rather than a
socket. That is what makes it safe to point this at every route without an allowlist.

This is deliberately NOT a behaviour test. It asserts nothing about the payloads; the modules'
own tests do that. It is a floor: no read endpoint may raise on a bare request.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import host_exec
from app import app as flask_app

# Path parameters that cannot be guessed from the rule alone. A plausible-but-absent id is the
# interesting case anyway: it drives the not-found branch, which is real code.
_ARG_VALUES = {
    "activity_id": "no-such-activity",
    "chart_id": "no-such-chart",
    "content_id": "no-such-content",
    "doc_id": "no-such-doc",
    "report_id": "no-such-report",
    "run_id": "no-such-run",
    "step_id": "no-such-step",
    "workflow_id": "no-such-workflow",
    "view": "overview",
    "path": "index.html",
}


def _readable_paths() -> list[str]:
    """Every GET rule, with path parameters filled in. Sorted so failures are reproducible."""
    out = []
    for rule in flask_app.url_map.iter_rules():
        if "GET" not in (rule.methods or set()) or rule.endpoint == "static":
            continue
        if any(arg not in _ARG_VALUES for arg in rule.arguments):
            continue
        out.append(rule.rule if not rule.arguments else str(rule.rule).format(
            **{a: _ARG_VALUES[a] for a in rule.arguments}
        ).replace("<", "").replace(">", ""))
    return sorted(set(out))


def _fill(rule) -> str | None:
    path = str(rule.rule)
    for arg in rule.arguments:
        if arg not in _ARG_VALUES:
            return None
        # Rules carry converters, e.g. <path:path> or <int:run_id>.
        for token in (f"<{arg}>", f"<path:{arg}>", f"<int:{arg}>", f"<string:{arg}>"):
            path = path.replace(token, _ARG_VALUES[arg])
    return None if "<" in path else path


GET_PATHS = sorted(
    {
        p
        for rule in flask_app.url_map.iter_rules()
        if "GET" in (rule.methods or set()) and rule.endpoint != "static"
        for p in [_fill(rule)]
        if p
    }
)


@pytest.fixture(autouse=True)
def _no_execution(monkeypatch):
    """Nothing in this module may execute anything, on this host or any other.

    `host_exec.run_on_host` / `popen_on_host` is the pair everything is SUPPOSED to funnel through
    (core-designs.mdc §1). Stubbing those two is not sufficient, and finding that out is one of the
    more useful things this file did: `api/tools.py` calls `subprocess.run` directly at eight sites,
    so POSTing to the workspace-install endpoints ran `bin/scripts/install-hpcx-workspace.sh` for
    real, against a path derived from this machine's home directory. In CI it would run them on the
    runner.

    So `subprocess.run` and `subprocess.Popen` are stubbed too. That is deliberately broader than
    this module's own needs -- it is a hard floor saying a unit test executes nothing -- and it
    stays until those call sites are moved behind host_exec, at which point the last two lines here
    become redundant rather than wrong.
    """

    def _completed(cmd, *a, **k):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    class _Proc:
        returncode = 0
        stdout = None
        stderr = None
        pid = -1

        def communicate(self, *a, **k):
            return ("", "")

        def wait(self, *a, **k):
            return 0

        def poll(self):
            return 0

        def kill(self):
            return None

        terminate = kill

    monkeypatch.setattr(host_exec, "run_on_host", _completed, raising=True)
    monkeypatch.setattr(host_exec, "popen_on_host", lambda *a, **k: _Proc(), raising=True)
    monkeypatch.setattr(subprocess, "run", _completed, raising=True)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Proc(), raising=True)


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


# The parameters these handlers actually read, so a request gets past "you gave me nothing" and
# into the code that does the work. Harmless together: unknown args are ignored by every handler.
_QUERY = {
    "rack": "p5-r01",
    "rack_id": "p5-r01",
    "job_id": "12345",
    "node": "p5-r01-n01",
    "nodes": "p5-r01-n01",
    "target": "p5-r01-n01",
    "kind": "nccl",
    "stage": "nccl",
    "format": "json",
    "hours": "1",
    "limit": "5",
    "minutes": "5",
}

MUTATING = sorted(
    {
        (p, method)
        for rule in flask_app.url_map.iter_rules()
        if rule.endpoint != "static"
        for method in sorted((rule.methods or set()) & {"POST", "PUT", "PATCH", "DELETE"})
        for p in [_fill(rule)]
        if p
    }
)


def _assert_did_not_crash(resp, what: str) -> None:
    """500 is the failure; other 5xx are answers.

    Flask turns an unhandled exception into a bare 500, so that code specifically means the handler
    fell over. The other 5xx in this codebase are deliberate and well-formed: 502 for "the tool or
    upstream I depend on failed" (`{"ok": false, "error": "No credentials file ..."}`) and 503 from
    `_common.serve_spa_asset` when the SPA has not been built. Asserting `< 500` rejected seven
    endpoints that were behaving correctly, which would have made this file lie about them.
    """
    assert resp.status_code != 500, (
        f"{what} -> 500 (unhandled exception)\n{resp.get_data(as_text=True)[:900]}"
    )


def test_the_url_map_is_worth_walking():
    """Guard the guard: if these collapse to a handful, the parametrization silently stopped."""
    assert len(GET_PATHS) > 90, f"only {len(GET_PATHS)} GET paths resolved -- did _fill() break?"
    assert len(MUTATING) > 90, f"only {len(MUTATING)} mutating routes resolved"


@pytest.mark.parametrize("path", GET_PATHS)
def test_read_endpoint_does_not_raise(client, path):
    _assert_did_not_crash(client.get(path), f"GET {path}")


@pytest.mark.parametrize("path", GET_PATHS)
def test_read_endpoint_does_not_raise_with_parameters(client, path):
    """Same walk, but with the arguments the handlers read.

    A bare GET often stops at "missing rack", leaving the actual work unexercised. Supplying a
    plausible rack / job / node drives the real path, which in demo mode has data to find.
    """
    _assert_did_not_crash(client.get(path, query_string=_QUERY), f"GET {path} (with params)")


@pytest.mark.parametrize(("path", "method"), MUTATING)
def test_mutating_endpoint_rejects_an_empty_body_without_raising(client, path, method):
    """A write endpoint given nothing must refuse, not raise.

    Deliberately an EMPTY body. That keeps the request in the validation code -- which is the
    majority of these handlers and was almost entirely uncovered -- without asking any of them to
    actually launch a job, reset a GPU or restart a service. A 4xx here is the correct answer and
    the one we expect; the assertion is only that nothing reached the traceback.
    """
    _assert_did_not_crash(client.open(path, method=method, json={}), f"{method} {path}")
