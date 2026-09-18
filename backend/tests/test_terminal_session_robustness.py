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
"""Typing into a shell that has already ended must report it, not drop the socket.

Observed on Nscale TX: a compute-node shell exited moments after connecting (the node's own
interactive login is broken), the operator pressed Enter, and the UI showed a bare "WebSocket
error". The cause was ours -- ``write()`` checked an ``alive`` flag owned by a different thread,
then called ``os.write`` on a pty master whose child had gone, which raises EIO. The exception
escaped the websocket handler and closed the connection abnormally, discarding the
"[Process exited...]" message the code was otherwise ready to send.

The flag can never be authoritative here: the child may exit between the check and the write. So
these tests pin the requirement that the write path is non-throwing regardless of the flag.
"""
from __future__ import annotations

import os
import pty
import queue
import subprocess
import time

import api.terminal as T
import pytest

# These exercise a real pty, because the bug is in pty semantics -- a mock would have to encode the
# EIO behaviour under test and would prove nothing. Some restricted environments cannot allocate
# one ("out of pty devices"), so skip rather than fail there. The container the app actually runs in
# does allocate ptys; that is how the terminal works at all.
try:
    _probe_master, _probe_slave = pty.openpty()
except OSError as exc:  # pragma: no cover - environment-dependent
    pytest.skip(f"pty allocation unavailable: {exc}", allow_module_level=True)
else:
    os.close(_probe_master)
    os.close(_probe_slave)


def _session(argv: list[str]) -> T._TerminalSession:
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        argv, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, start_new_session=True
    )
    os.close(slave_fd)
    return T._TerminalSession("test-session", master_fd, proc, "ct07-01", "node")


def _settle(sess, timeout: float = 3.0) -> None:
    """Wait for the reader thread to finish owning ``alive``.

    ``proc.wait()`` only waits for the child. The reader thread flips ``alive`` in its own
    ``finally``, so a test that sets ``alive = True`` immediately after the child exits can have it
    flipped back underneath, making ``write()`` return early and report nothing.
    """
    deadline = time.monotonic() + timeout
    while sess.alive and time.monotonic() < deadline:
        time.sleep(0.01)


def _drain(q: queue.Queue) -> tuple[list[bytes], bool]:
    """Everything queued so far, and whether the stream was terminated with None."""
    out: list[bytes] = []
    ended = False
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        if item is None:
            ended = True
        else:
            out.append(item)
    return out, ended


class TestWriteAfterTheShellExits:
    def test_a_keystroke_after_exit_does_not_raise(self):
        sess = _session(["true"])  # exits immediately
        sess.subscribe()  # the subscription is the point; the queue itself is not read
        # Let the child exit and the reader thread notice.
        for _ in range(100):
            if not sess.alive:
                break
            time.sleep(0.02)

        sess.write(b"\r")  # the Enter that produced "WebSocket error"
        sess.write(b"echo hi\r")

    def test_a_keystroke_into_a_dead_pty_is_reported_as_an_ended_session(self):
        """The exact race: the flag still says alive, but the pty is gone."""
        sess = _session(["true"])
        q = sess.subscribe()
        _settle(sess)
        # Force the race rather than waiting for it: pretend the reader has not caught up.
        sess.alive = True
        os.close(sess.master_fd)

        sess.write(b"\r")

        messages, ended = _drain(q)
        assert sess.alive is False
        assert ended, "the viewer must be told the stream is over"
        assert any(b"session ended" in m for m in messages), messages

    def test_the_reason_is_only_announced_once(self):
        sess = _session(["true"])
        q = sess.subscribe()
        _settle(sess)
        sess.alive = True
        os.close(sess.master_fd)

        for _ in range(5):
            sess.write(b"x")

        messages, _ = _drain(q)
        assert sum(b"session ended" in m for m in messages) == 1, messages

    def test_a_resize_after_exit_does_not_raise(self):
        """xterm resizes on every pane drag, so this fires constantly in the grid."""
        sess = _session(["true"])
        sess.subscribe()
        _settle(sess)
        sess.alive = True
        os.close(sess.master_fd)

        sess.resize(120, 40)
        assert sess.alive is False


class TestALiveSessionStillWorks:
    def test_input_reaches_a_running_shell_and_output_comes_back(self):
        sess = _session(["cat"])  # echoes stdin
        q = sess.subscribe()
        try:
            sess.write(b"hello\r")
            deadline = time.monotonic() + 3
            seen = b""
            while time.monotonic() < deadline and b"hello" not in seen:
                try:
                    item = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item:
                    seen += item
            assert b"hello" in seen, seen
            assert sess.alive is True
        finally:
            sess.destroy()

    def test_resizing_a_live_session_keeps_it_alive(self):
        sess = _session(["cat"])
        sess.subscribe()
        try:
            sess.resize(100, 30)
            assert sess.alive is True
        finally:
            sess.destroy()
