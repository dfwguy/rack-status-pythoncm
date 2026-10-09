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

import re
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


@pytest.fixture(autouse=True)
def _configured_workspace(monkeypatch, tmp_path):
    """A cluster with its workspace set, as a deployed one has.

    The workspace endpoints refuse to run without one (there is no fallback path any more -- see
    docs/explanation/how-cluster-paths-reach-scripts.md). Configure it so a smoke request gets past
    that guard into the code that does the work, which is the point of this module.
    """
    monkeypatch.setenv("NVIS_WORKSPACE_DIR", str(tmp_path / "workspace"))


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


#: The one deliberate non-JSON 5xx in the product. `api/_common.serve_spa_asset` answers a missing
#: SPA bundle with a human-readable HTML page, because its audience is somebody looking at a browser
#: rather than a script parsing a response -- and it is 503 because an unbuilt frontend is
#: unavailability, not a server fault. Its contract is pinned by
#: test_app_api.py::test_missing_shell_503s_instead_of_raising, so this only has to recognise it.
_SHELL_NOT_BUILT = "Frontend not built"


def _assert_did_not_crash(resp, what: str) -> None:
    """A crash is an exception or an HTML error page -- NOT any particular status code.

    Two attempts at this were wrong, and the reasons are worth keeping because both look right.

    `< 500` rejected seven endpoints that were behaving correctly: they answer 502 with a formed
    body -- {"ok": false, "error": "No credentials file. Set PROMETHEUS_RC ..."} -- because a tool
    they wrap is absent. That is a report, not a fall-over.

    `!= 500` was wrong too, and more interestingly. A deliberate JSON error carrying status 500 is
    this codebase's DOMINANT convention: 100 such sites against 14 using 502. Gating on the code
    would have meant either rewriting 100 API contracts from inside a coverage task, or a test that
    fails on established behaviour.

    What actually separates the two is the body. An unhandled exception yields Flask's HTML error
    page; every deliberate error here yields JSON. And with TESTING = True a genuine crash does not
    even reach this function -- Flask re-raises, so the test fails on the traceback, which is how
    the `{"steps": 1}` TypeError in api/workflows.py surfaced. This check is the belt to that
    braces, and it is what would catch the crash if TESTING were ever turned off.
    """
    assert resp.status_code < 600, f"{what} -> {resp.status_code}: not a response at all"
    if resp.status_code < 500:
        return

    body = resp.get_data(as_text=True)
    # The SPA shell is the documented exception, and this is where CI and a developer's tree
    # disagree: `static/spa/` is a build artifact and is gitignored, so the four shell routes
    # answer 200 locally and 503-with-an-HTML-page on a runner that never ran `npm run build`.
    # Local runs cannot see this class of failure at all, which is why it reached the pipeline.
    if resp.status_code == 503 and _SHELL_NOT_BUILT in body:
        return

    assert resp.is_json, (
        f"{what} -> {resp.status_code} with a {resp.content_type} body, which means an "
        f"unhandled exception rather than a reported error:\n{body[:900]}"
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


def _dry_run_switches() -> list[str]:
    """Every RACK_*_DRY_RUN knob, read out of the source rather than listed here.

    A hand-written list would be correct on the day it was written. Discovering them means a knob
    added next month is honoured by this test without anyone remembering it exists.
    """
    names: set[str] = set()
    root = Path(__file__).resolve().parent.parent
    for path in root.rglob("*.py"):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        names |= set(re.findall(r"RACK_[A-Z0-9_]*DRY_RUN", path.read_text(encoding="utf-8")))
    return sorted(names)


# The fields these handlers look for. Sent together because unknown keys are ignored, so one body
# gets many different endpoints past validation and into the work they actually do.
_BODY = {
    "rack": "p5-r01",
    "rack_id": "p5-r01",
    "racks": ["p5-r01"],
    "node": "p5-r01-n01",
    "nodes": ["p5-r01-n01"],
    "target": "p5-r01-n01",
    "pdsh_w": "p5-r01-n01",
    "job_id": "12345",
    "kind": "nccl",
    "stage": "nccl",
    "state": "resume",
    "reason": "endpoint smoke test",
    "operator": "test-suite",
    "format": "json",
    "steps": 1,
    "confirm": True,
    "dry_run": True,
}


@pytest.fixture
def dry_run_everything(monkeypatch):
    """Turn on every dry-run switch in the tree.

    This is what makes it safe to POST a REAL body to endpoints that reset GPUs, power-cycle a
    rack, change a MAC or restart munge. Each takes its dry-run branch, which still runs the
    validation, the target expansion and the command construction -- the code worth covering --
    and reports the command instead of running it. Combined with the stubbed subprocess, there are
    two independent reasons nothing executes.
    """
    for name in _dry_run_switches():
        monkeypatch.setenv(name, "1")


@pytest.mark.parametrize(("path", "method"), MUTATING)
def test_mutating_endpoint_accepts_a_realistic_body(client, dry_run_everything, path, method):
    """Same walk, with a body the handlers recognise, every dry-run switch on.

    The empty-body pass covers the refusals; this one covers the accepted path as far as the point
    of execution. Together they are why api/rack_power.py stops being a 12% file.
    """
    _assert_did_not_crash(client.open(path, method=method, json=_BODY), f"{method} {path} (body)")


@pytest.mark.parametrize("steps", [1, "abc", {"a": 1}, 3.5, True])
def test_generate_mermaid_rejects_a_non_list_steps(client, steps):
    """Regression: found by the walk above, fixed in api/workflows.py.

    `if not steps` accepted any truthy value, so `{"steps": 1}` reached `for step in steps` and
    raised TypeError inside the generator -- a client's malformed request presented as a server
    fault. Parametrized over the types a real caller plausibly sends by mistake.
    """
    resp = client.post("/api/workflows/generate-mermaid", json={"steps": steps})
    assert resp.status_code == 400, resp.get_data(as_text=True)[:300]
    assert resp.get_json()["error"] == "steps must be an array"


def test_generate_mermaid_still_accepts_a_real_step_list(client):
    """The guard above must not have closed the door on the working case."""
    resp = client.post(
        "/api/workflows/generate-mermaid", json={"steps": [{"id": "a", "name": "A"}]}
    )
    assert resp.status_code == 200
    assert "graph TD" in resp.get_json()["mermaid"]


@pytest.mark.parametrize(("path", "method"), MUTATING)
def test_mutating_endpoint_rejects_an_empty_body_without_raising(client, path, method):
    """A write endpoint given nothing must refuse, not raise.

    Deliberately an EMPTY body. That keeps the request in the validation code -- which is the
    majority of these handlers and was almost entirely uncovered -- without asking any of them to
    actually launch a job, reset a GPU or restart a service. A 4xx here is the correct answer and
    the one we expect; the assertion is only that nothing reached the traceback.
    """
    _assert_did_not_crash(client.open(path, method=method, json={}), f"{method} {path}")
