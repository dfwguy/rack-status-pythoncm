# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A deployment with no head node degrades; it does not raise from every predicate.

The reported symptom was ``GET /api/status`` returning 500 on a laptop with no BCM:
``host_exec.is_dir`` raised ``RuntimeError: No SSH host is configured`` out of
``paths._resolve_log_dir``. The endpoint already knows how to report an unreachable cluster --
it calls ``host_exec.reachability_state()`` and merges the answer into its payload -- but the
predicate exploded first.

``_probe_reachable`` had always treated this exact condition as a reportable state (it catches
the same error and returns ``(False, detail)``). These tests pin the *other* 65 call sites
agreeing with it, and pin the boundary: "nowhere to ask" degrades, "asked and refused" does not.
"""
from __future__ import annotations

import host_exec
import paths
import pytest
from transport import NotConfigured


@pytest.fixture
def unconfigured(monkeypatch):
    """An SSH deployment with no host — the laptop-without-BCM case."""
    monkeypatch.setenv("RACK_STATUS_DEMO", "0")
    monkeypatch.delenv("RACK_EXEC_MODE", raising=False)
    monkeypatch.setenv("RACK_SSH_HOST", "")
    host_exec.invalidate_connection()
    yield
    host_exec.invalidate_connection()


@pytest.fixture
def unreachable(monkeypatch):
    """A configured head node that cannot be reached: port 1 refuses immediately."""
    monkeypatch.setenv("RACK_STATUS_DEMO", "0")
    monkeypatch.delenv("RACK_EXEC_MODE", raising=False)
    monkeypatch.setenv("RACK_SSH_HOST", "127.0.0.1")
    monkeypatch.setenv("RACK_SSH_PORT", "1")
    monkeypatch.setenv("RACK_SSH_USER", "nobody")
    host_exec.invalidate_connection()
    yield
    host_exec.invalidate_connection()


@pytest.fixture
def local_mode(monkeypatch):
    monkeypatch.setenv("RACK_STATUS_DEMO", "0")
    monkeypatch.setenv("RACK_EXEC_MODE", "local")
    host_exec.invalidate_connection()
    yield
    host_exec.invalidate_connection()


# --- the typed condition ---------------------------------------------------------------


def test_not_configured_is_still_a_runtime_error():
    """Widening the type would silently break every existing ``except RuntimeError``.

    ``host_exec._probe_reachable`` is one of them, and it is what makes the status payload
    able to explain itself. If this subclassing is ever dropped, reachability starts raising.
    """
    assert issubclass(NotConfigured, RuntimeError)


def test_an_unconfigured_ssh_target_raises_the_typed_error(unconfigured):
    from transport import SSHConfig, SSHExecutor

    with pytest.raises(NotConfigured):
        SSHExecutor(SSHConfig(host="")).ssh_argv()


# --- "nowhere to ask" degrades ---------------------------------------------------------


def test_predicates_answer_no_instead_of_raising(unconfigured):
    assert host_exec.execution_host_configured() is False
    assert host_exec.is_dir("/var/log/nvis") is False
    assert host_exec.is_file("/usr/bin/sinfo") is False
    assert host_exec.which("sinfo") is None


def test_reachability_still_explains_why(unconfigured):
    """Degrading the predicate must not cost the operator the reason."""
    ok, detail = host_exec.reachability_state()
    assert ok is False
    assert "RACK_SSH_HOST" in detail


def test_resolve_log_dir_does_not_blame_the_path(unconfigured):
    """It returns the configured default rather than "not a directory".

    Calling an unverifiable path invalid is what turned an unconfigured deployment into a 500
    on a continuously-polled endpoint.
    """
    assert str(paths._resolve_log_dir(None)).endswith("var/logs")


# --- "asked and refused" must NOT be flattened -----------------------------------------


def test_a_configured_host_is_still_verified(unreachable):
    """The boundary that makes this safe.

    With a host configured, verification runs and a bad path is still rejected. Skipping the
    check whenever anything went wrong would turn "the head node refused us" into "the file is
    absent", which is the failure mode the narrow ``NotConfigured`` scope exists to avoid.
    """
    assert host_exec.execution_host_configured() is True
    with pytest.raises(ValueError, match="not a directory"):
        paths._resolve_log_dir("/definitely/not/here")


def test_a_genuine_ssh_failure_is_reported_not_silent(unreachable):
    ok, detail = host_exec.reachability_state()
    assert ok is False
    assert detail  # something about the refused connection, not an empty shrug


# --- the endpoint the bug was reported on ----------------------------------------------


def test_api_status_reports_the_cluster_instead_of_failing(monkeypatch):
    """The point of the whole change: a degraded answer, not a 500.

    ``api_status`` already merges ``reachability_state()`` into its payload so the dashboard
    can render "cluster unreachable". Patching the predicate that used to raise is enough to
    show the endpoint now reaches that code at all.
    """
    import app as app_module
    from api import dashboard

    monkeypatch.setattr(
        dashboard.host_exec,
        "reachability_state",
        lambda *a, **k: (False, "No SSH host is configured. Point RACK_SSH_HOST at the head node."),
    )
    monkeypatch.setattr(paths.host_exec, "execution_host_configured", lambda *a, **k: False)

    with app_module.app.test_client() as client:
        resp = client.get("/api/status")

    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    body = resp.get_json()
    assert body.get("cluster_reachable") is False
    assert "RACK_SSH_HOST" in str(body.get("cluster_unreachable_detail", ""))


# --- the whole GET surface, in this profile --------------------------------------------


def test_no_endpoint_answers_with_an_html_error_page(unconfigured, monkeypatch):
    """Walk every GET route with no head node and demand a machine-readable answer.

    ``test_endpoint_smoke`` walks the same paths but only in **demo mode**, which is exactly
    why the reported ``/api/status`` 500 was never caught: in demo mode the SSH path is never
    taken, so the whole "deployment has no cluster" profile went unexercised. This is the
    second profile, as one test rather than a second parametrized suite -- the class of bug it
    guards is "an unknown situation produced HTML", which needs no per-path assertions.

    The SPA shell routes are excluded by status, not by path: they legitimately answer 200 with
    HTML (or 503 with HTML when the frontend is not built, which CI sees and a developer does
    not -- see ``_assert_did_not_crash``).
    """
    import app as app_module

    from tests.test_endpoint_smoke import _QUERY, GET_PATHS

    monkeypatch.setitem(app_module.app.config, "PROPAGATE_EXCEPTIONS", False)

    offenders: list[str] = []
    with app_module.app.test_client() as client:
        for path in GET_PATHS:
            try:
                resp = client.get(path, query_string=_QUERY)
            except Exception as e:  # noqa: BLE001 - the point is that nothing escapes
                offenders.append(f"{path} raised {type(e).__name__}: {e}")
                continue
            if resp.status_code < 500:
                continue
            body = resp.get_data(as_text=True)
            if resp.status_code == 503 and "Frontend not built" in body:
                continue
            if not resp.is_json:
                offenders.append(f"{path} -> {resp.status_code} {resp.content_type}: {body[:120]}")

    assert not offenders, "endpoints answered an unknown situation with a non-JSON body:\n" + "\n".join(
        offenders
    )


def test_a_missing_cluster_is_unavailable_not_a_server_fault(unconfigured, monkeypatch):
    """``NotConfigured`` reaching the handler is 503, so config problems are not "crashes".

    Six endpoints still answer 500 because they catch the exception themselves and report it
    in their own body; they are honest, just differently classified. This pins the handler's
    contract, which is what the other six should converge on.
    """
    import app as app_module

    monkeypatch.setitem(app_module.app.config, "PROPAGATE_EXCEPTIONS", False)
    with app_module.app.test_client() as client:
        resp = client.get("/api/scheduler-jobs")
    assert resp.status_code == 503, resp.get_data(as_text=True)[:200]
    assert "RACK_SSH_HOST" in resp.get_json()["error"]


# --- local mode is untouched -----------------------------------------------------------


def test_local_mode_is_unaffected(local_mode, tmp_path):
    assert host_exec.execution_host_configured() is True
    assert host_exec.is_dir(str(tmp_path)) is True
    assert host_exec.is_dir(str(tmp_path / "nope")) is False
