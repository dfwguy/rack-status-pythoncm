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


def test_live_tail_capacity_preserves_threads_for_normal_requests():
    """Abandoned tail viewers cannot consume the entire Gunicorn thread pool."""
    leases = []
    try:
        for _ in range(app_mod._TAIL_STREAM_CAPACITY):
            prep = {}
            assert app_mod._tail_stream_slots.acquire(blocking=False)
            prep["_tail_stream_slot"] = True
            leases.append(prep)

        assert not app_mod._tail_stream_slots.acquire(blocking=False)
    finally:
        for prep in leases:
            app_mod._release_tail_stream_slot(prep)

    # Release is idempotent because Response.close and generator.finally may race.
    app_mod._release_tail_stream_slot(leases[0])
    assert app_mod._tail_stream_slots.acquire(blocking=False)
    app_mod._tail_stream_slots.release()
