# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Analytics-lake request capture must never materialise a streaming response.

The live log viewer POSTs to ``/api/tail-job-stdout-stream`` and reads a
``tail -f`` body incrementally. ``after_request`` capture calling ``get_data()``
on that response consumes a generator that never ends, so the client receives
nothing and the viewer hangs on "Connecting…".
"""
import app as app_mod
import lake_sink
from flask import Response


class _FakeSink:
    """Records what would be shipped, without shipping it.

    ``never_capture`` and ``scrub_record`` delegate to the real module deliberately: they are the
    egress policy, and a fake that stubbed them would let this test pass while the policy was
    broken. Only the transport is faked — ``submit_run`` collects instead of enqueuing.
    """

    def __init__(self):
        self.pushes = []

    def is_disabled(self):
        return False

    def make_run_id(self, *_args, **_kwargs):
        return "run-id"

    never_capture = staticmethod(lake_sink.never_capture)
    scrub_record = staticmethod(lake_sink.scrub_record)

    def submit_run(self, **kwargs):
        self.pushes.append(kwargs)

    def push_run(self, **kwargs):  # pragma: no cover - capture uses submit_run now
        raise AssertionError(
            "request capture must use submit_run, not push_run: a synchronous POST inside "
            "after_request puts the lake's availability in the operator's latency path"
        )


def test_streamed_response_is_not_consumed_by_lake_capture(monkeypatch):
    sink = _FakeSink()
    monkeypatch.setattr(app_mod, "lake_sink", sink)
    monkeypatch.setenv("RACK_LAKE_CAPTURE", "actions")

    pulled: list[int] = []

    def body():
        pulled.append(1)
        yield b"header\n"
        pulled.append(2)
        yield b"live line\n"

    with app_mod.app.test_request_context(
        "/api/tail-job-stdout-stream", method="POST", json={"path": "/tmp/x.log"}
    ):
        resp = app_mod._lake_capture_request(Response(body(), mimetype="text/plain"))
        assert pulled == [], "capture consumed the stream before the client read it"
        assert b"".join(resp.response) == b"header\nlive line\n"

    assert sink.pushes, "the request itself should still be recorded"
    assert sink.pushes[0]["log_text"] == "<streaming response — not captured>"


def test_buffered_response_body_is_still_captured(monkeypatch):
    sink = _FakeSink()
    monkeypatch.setattr(app_mod, "lake_sink", sink)
    monkeypatch.setenv("RACK_LAKE_CAPTURE", "actions")

    with app_mod.app.test_request_context(
        "/api/hw-check", method="POST", json={"rack": "p5-r01"}
    ):
        app_mod._lake_capture_request(Response(b'{"ok": true}', mimetype="application/json"))

    assert sink.pushes[0]["log_text"] == '{"ok": true}'


def test_a_credential_endpoint_is_not_captured_at_all(monkeypatch):
    """The egress refusal, exercised through the real request path rather than the unit.

    `POST /api/login` carries `{"password": ...}`. Capture used to take it verbatim into
    `request_body` and ship it to a store that retains artifacts permanently and on purpose.
    """
    sink = _FakeSink()
    monkeypatch.setattr(app_mod, "lake_sink", sink)
    monkeypatch.setenv("RACK_LAKE_CAPTURE", "all")  # widest mode; must still refuse

    with app_mod.app.test_request_context(
        "/api/login", method="POST", json={"password": "hunter2-secret"}
    ):
        app_mod._lake_capture_request(Response(b'{"ok": false}', mimetype="application/json"))

    assert sink.pushes == [], "a credential endpoint reached the lake"


def test_a_secret_on_an_ordinary_endpoint_is_redacted_before_shipping(monkeypatch):
    """Redaction of a captured request body, via the real `after_request` handler.

    `g._lake_body` is set by hand because it is normally populated by the `before_request` half of
    the pair, and these tests invoke the `after_request` half directly. Without it the body is empty
    and the assertion would pass while proving nothing -- the first version of this test did exactly
    that.
    """
    from flask import g

    sink = _FakeSink()
    monkeypatch.setattr(app_mod, "lake_sink", sink)
    monkeypatch.setenv("RACK_LAKE_CAPTURE", "actions")

    body = b'{"rack": "p5-r01", "token": "t0ken-secret-value"}'
    with app_mod.app.test_request_context("/api/hw-check", method="POST", data=body,
                                          content_type="application/json"):
        g._lake_body = body
        app_mod._lake_capture_request(Response(b'{"ok": true}', mimetype="application/json"))

    shipped = repr(sink.pushes)
    assert "t0ken-secret-value" not in shipped, "the token reached the lake"
    assert "REDACTED" in shipped, "the field was dropped rather than redacted"
    assert "p5-r01" in shipped, "redaction should not cost the provenance data"
