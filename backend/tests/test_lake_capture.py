# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Analytics-lake request capture must never materialise a streaming response.

The live log viewer POSTs to ``/api/tail-job-stdout-stream`` and reads a
``tail -f`` body incrementally. ``after_request`` capture calling ``get_data()``
on that response consumes a generator that never ends, so the client receives
nothing and the viewer hangs on "Connecting…".
"""
import app as app_mod
from flask import Response


class _FakeSink:
    def __init__(self):
        self.pushes = []

    def is_disabled(self):
        return False

    def make_run_id(self, *_args, **_kwargs):
        return "run-id"

    def push_run(self, **kwargs):
        self.pushes.append(kwargs)


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
