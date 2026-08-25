# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Interactive web terminal: PTY sessions over a WebSocket.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The first module extracted from ``app.py`` as part of splitting it by domain.

**Not a Blueprint.** ``/ws/terminal`` is handled by WSGI middleware wrapping ``app.wsgi_app``,
below Flask's router: simple_websocket writes the HTTP 101 directly onto the raw socket and never
calls ``start_response``, which a Flask view cannot do. So this module exposes ``install(app)``
instead of ``bp``, which the loader in ``backend/api/__init__.py`` calls.

A session outlives its WebSocket by a grace period, so a reconnecting browser -- or a pane
detached into a new tab -- adopts the running shell instead of opening a second SSH hop to the
same node.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import pty
import queue
import select
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from collections.abc import Callable
from http.cookies import SimpleCookie

import host_exec
from core import truthy_env

_logger = logging.getLogger("rack_web.api.terminal")



_TERMINAL_ENABLED = os.environ.get("RACK_ENABLE_WEB_TERMINAL", "1").strip().lower() in (
    "1", "true", "yes", "on",
)

class _TerminalSession:
    """Holds state for a single PTY session that can outlive a WebSocket."""

    def __init__(self, session_id: str, master_fd: int, proc: subprocess.Popen,
                 target: str, mode: str):
        self.session_id = session_id
        self.master_fd = master_fd
        self.proc = proc
        self.target = target
        self.mode = mode
        self.alive = True
        self.ws_count = 0
        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue[bytes | None]] = []
        self._start_reader()

    def _start_reader(self) -> None:
        def _pty_reader():
            try:
                while self.alive:
                    rlist, _, _ = select.select([self.master_fd], [], [], 0.5)
                    if rlist:
                        try:
                            data = os.read(self.master_fd, 4096)
                        except OSError:
                            break
                        if not data:
                            break
                        with self.lock:
                            for q in self.subscribers:
                                q.put(data)
                    if self.proc.poll() is not None:
                        time.sleep(0.2)
                        try:
                            remaining = os.read(self.master_fd, 4096)
                            if remaining:
                                with self.lock:
                                    for q in self.subscribers:
                                        q.put(remaining)
                        except OSError:
                            pass
                        msg = f"\r\n\x1b[31m[Process exited with code {self.proc.returncode}]\x1b[0m\r\n".encode()
                        with self.lock:
                            for q in self.subscribers:
                                q.put(msg)
                        break
            except Exception as exc:
                _logger.debug("_pty_reader: ignoring error", exc_info=True)
                msg = f"\r\n\x1b[31m[PTY error: {exc}]\x1b[0m\r\n".encode()
                with self.lock:
                    for q in self.subscribers:
                        q.put(msg)
            finally:
                self.alive = False
                with self.lock:
                    for q in self.subscribers:
                        q.put(None)

        threading.Thread(target=_pty_reader, daemon=True).start()

    def subscribe(self) -> queue.Queue[bytes | None]:
        q: queue.Queue[bytes | None] = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
            self.ws_count += 1
        return q

    def unsubscribe(self, q: queue.Queue[bytes | None]) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)
            self.ws_count -= 1

    def _mark_dead(self, note: str) -> None:
        """Flip to dead and tell every viewer why, exactly once."""
        if not self.alive:
            return
        self.alive = False
        _logger.info("WebSocket terminal: %s (%s): %s", self.target, self.session_id, note)
        payload = f"\r\n\x1b[1;31m[{note}]\x1b[0m\r\n".encode()
        with self.lock:
            for q in self.subscribers:
                q.put(payload)
                q.put(None)

    def write(self, data: bytes) -> None:
        """Forward keystrokes to the shell. Never raises.

        ``alive`` is flipped by the reader thread, so checking it is inherently racy: the child can
        exit between the check and the write, and writing to a pty master whose child is gone raises
        EIO. Unguarded, that exception escaped the websocket handler and closed the socket
        abnormally -- the browser reported a bare "WebSocket error", so an operator who typed into a
        shell that had already ended was told nothing about what happened. Reported as an ended
        session instead, which is what it is.
        """
        if not self.alive:
            return
        try:
            os.write(self.master_fd, data)
        except OSError as exc:
            self._mark_dead(f"session ended: {exc.strerror or exc}")

    def resize(self, cols: int, rows: int) -> None:
        """Apply a new window size. Never raises — xterm resizes on every pane drag."""
        if not self.alive:
            return
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            os.kill(self.proc.pid, signal.SIGWINCH)
        except (OSError, ProcessLookupError) as exc:
            self._mark_dead(f"session ended: {exc.strerror or exc}")

    def force_redraw(self) -> None:
        """Send SIGWINCH to the process group to trigger a full screen redraw."""
        if self.alive:
            try:
                os.kill(self.proc.pid, signal.SIGWINCH)
            except (ProcessLookupError, OSError):
                pass

    def destroy(self) -> None:
        if not self.alive:
            return
        self.alive = False
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            _logger.debug("destroy: ignoring error", exc_info=True)
            try:
                self.proc.kill()
            except Exception:
                _logger.debug("destroy: ignoring error", exc_info=True)
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        _logger.info("WebSocket terminal: session %s (%s) destroyed", self.session_id, self.target)


_TERMINAL_GRACE_PERIOD = 30  # seconds to keep PTY alive with no WebSocket


class _WebSocketTerminalMiddleware:
    """WSGI middleware that handles /ws/terminal at the WSGI layer.

    This bypasses Flask's response handling entirely, which prevents
    Werkzeug from writing HTTP headers into the WebSocket stream.

    Sessions are tracked by UUID so a WebSocket can disconnect and a new
    one (e.g. from a pop-out tab) can reattach to the same PTY.
    """

    def __init__(
        self,
        wsgi_app,
        authorized: Callable[[dict], bool] | None = None,
    ):  # type: ignore[no-untyped-def]
        self.wsgi_app = wsgi_app
        self.authorized = authorized or (lambda _environ: True)
        self._sessions: dict[str, _TerminalSession] = {}
        self._sessions_lock = threading.Lock()

    def __call__(self, environ, start_response):  # type: ignore[no-untyped-def]
        path = environ.get("PATH_INFO", "")
        upgrade = (environ.get("HTTP_UPGRADE") or "").strip().lower()
        has_socket = "werkzeug.socket" in environ or "gunicorn.socket" in environ
        # Werkzeug injects werkzeug.socket for *every* request, so require Upgrade:
        # websocket before claiming this path. simple_websocket writes the HTTP 101
        # response on the raw socket and never calls start_response; Werkzeug still
        # finalizes with write(b""), so we must call start_response before returning.
        if (
            path == "/ws/terminal"
            and upgrade == "websocket"
            and has_socket
            and _TERMINAL_ENABLED
        ):
            if not self.authorized(environ):
                # Delegate to Flask so its normal /ws authentication response is
                # returned before any socket upgrade or PTY allocation occurs.
                return self.wsgi_app(environ, start_response)
            try:
                self._handle_terminal(environ)
            finally:
                try:
                    start_response("200 OK", [])
                except Exception:
                    # Headers may already be set, or the peer already closed the socket.
                    _logger.debug("__call__: ignoring error", exc_info=True)
            return []
        return self.wsgi_app(environ, start_response)

    def _cleanup_session(self, session_id: str) -> None:
        """Destroy a session after the grace period if no clients reconnected."""
        import time
        time.sleep(_TERMINAL_GRACE_PERIOD)
        with self._sessions_lock:
            sess = self._sessions.get(session_id)
            if sess and sess.ws_count <= 0:
                sess.destroy()
                del self._sessions[session_id]
                _logger.info("WebSocket terminal: session %s expired after grace period", session_id)

    def _get_or_create_session(self, qs: dict) -> _TerminalSession | None:
        """Return an existing session for reconnect, or create a new one."""
        session_id = (qs.get("session") or [""])[0].strip()
        if session_id:
            with self._sessions_lock:
                sess = self._sessions.get(session_id)
            if sess and sess.alive:
                return sess
            return None

        # New session
        target = (qs.get("target") or qs.get("node") or [""])[0].strip()
        mode = (qs.get("mode") or ["node"])[0].strip()
        if not target and mode != "headnode":
            return None

        if truthy_env("RACK_STATUS_DEMO"):
            _logger.info("WebSocket terminal: blocked in demo mode (target=%s)", target or "headnode")
            return None

        if mode == "headnode":
            argv = host_exec.ssh_to_headnode_argv()
            target = target or "headnode"
        else:
            argv = host_exec.ssh_to_node_argv(target)

        _logger.info("WebSocket terminal: new session for %s via %s", target, " ".join(argv))

        master_fd, slave_fd = pty.openpty()
        winsize = struct.pack("HHHH", 24, 80, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            # start_new_session is setsid() done by subprocess itself. preexec_fn runs arbitrary
            # Python between fork and exec, which is not safe in a threaded server (gunicorn runs
            # this with 24 gthread workers) because the child inherits locks held by other threads.
            start_new_session=True,
            env=env,
        )
        os.close(slave_fd)

        new_id = str(uuid.uuid4())
        sess = _TerminalSession(new_id, master_fd, proc, target, mode)
        with self._sessions_lock:
            self._sessions[new_id] = sess
        return sess

    def _handle_terminal(self, environ):  # type: ignore[no-untyped-def]
        from urllib.parse import parse_qs

        from simple_websocket import ConnectionClosed as _WSClosed
        from simple_websocket import Server as _WSServer

        ws = None
        outq = None
        sess = None
        try:
            ws = _WSServer(environ)
            qs = parse_qs(environ.get("QUERY_STRING", ""))
            sess = self._get_or_create_session(qs)

            if sess is None:
                reason = (
                    "Demo mode — terminal connections are disabled"
                    if truthy_env("RACK_STATUS_DEMO")
                    else "Missing target node"
                )
                ws.send(json.dumps({"type": "error", "error": reason}))
                return

            # Send session ID so the frontend can use it for pop-out
            is_reconnect = bool((qs.get("session") or [""])[0].strip())
            ws.send(json.dumps({
                "type": "session",
                "id": sess.session_id,
                "target": sess.target,
                "mode": sess.mode,
                "reconnect": is_reconnect,
            }))

            outq = sess.subscribe()
            pending_redraw = is_reconnect

            while True:
                # Drain PTY output -> WS
                while True:
                    try:
                        chunk = outq.get_nowait()
                    except queue.Empty:
                        break
                    if chunk is None:
                        _logger.info("WebSocket terminal: PTY EOF for %s", sess.target)
                        # Say WHY the session ended, on the terminal itself. ssh's own message goes
                        # to the PTY and is usually enough, but not always: a hop killed by a
                        # keepalive timeout, or a shell that dies during login, closes the PTY with
                        # nothing printed, and the operator is left with a blank pane and the
                        # frontend's bare "Connection closed." The exit status is the one piece of
                        # evidence available in every case (255 = ssh could not establish or lost
                        # the connection, which for a compute node means the second hop).
                        rc = sess.proc.poll()
                        if rc is not None and rc != 0:
                            hint = " — ssh could not reach it, or the connection dropped" if rc == 255 else ""
                            ws.send(
                                f"\r\n\x1b[1;31m[{sess.target}: session ended, exit {rc}{hint}]\x1b[0m\r\n"
                            )
                        return
                    ws.send(chunk)

                # Read WS input -> PTY (with short timeout)
                try:
                    msg = ws.receive(timeout=0.05)
                except _WSClosed:
                    _logger.info(
                        "WebSocket terminal: client disconnected from %s (session %s)",
                        sess.target,
                        sess.session_id,
                    )
                    return
                except OSError:
                    return
                except Exception as exc:
                    _logger.warning("WebSocket terminal: receive error: %s", exc, exc_info=True)
                    continue

                if msg is None:
                    continue

                if isinstance(msg, str):
                    try:
                        ctrl = json.loads(msg)
                        if isinstance(ctrl, dict) and ctrl.get("type") == "resize":
                            sess.resize(int(ctrl.get("cols", 80)), int(ctrl.get("rows", 24)))
                            if pending_redraw:
                                pending_redraw = False
                                time.sleep(0.1)
                                sess.force_redraw()
                            continue
                    except (json.JSONDecodeError, ValueError):
                        pass
                    sess.write(msg.encode("utf-8"))
                elif isinstance(msg, bytes):
                    sess.write(msg)

        except _WSClosed:
            pass
        except Exception:
            _logger.exception("ws_terminal: unhandled error")
        finally:
            if sess and outq:
                sess.unsubscribe(outq)
                if sess.ws_count <= 0 and sess.alive:
                    threading.Thread(
                        target=self._cleanup_session,
                        args=(sess.session_id,),
                        daemon=True,
                    ).start()
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    _logger.debug("_handle_terminal: ignoring error", exc_info=True)


def install(app) -> None:  # type: ignore[no-untyped-def]
    """Wrap the app's WSGI callable so ``/ws/terminal`` is handled before Flask routing."""
    if not _TERMINAL_ENABLED:
        _logger.info("web terminal disabled (RACK_ENABLE_WEB_TERMINAL)")
        return
    def authorized(environ: dict) -> bool:
        if truthy_env("RACK_WEB_AUTH_DISABLED"):
            return True
        try:
            cookies = SimpleCookie()
            cookies.load(str(environ.get("HTTP_COOKIE") or ""))
            cookie_name = str(app.config.get("SESSION_COOKIE_NAME") or "session")
            morsel = cookies.get(cookie_name)
            serializer = app.session_interface.get_signing_serializer(app)
            if morsel is None or serializer is None:
                return False
            values = serializer.loads(
                morsel.value,
                max_age=int(app.permanent_session_lifetime.total_seconds()),
            )
            return isinstance(values, dict) and values.get("rack_web_auth") == "1"
        except Exception:
            _logger.debug("terminal websocket session authentication failed", exc_info=True)
            return False

    app.wsgi_app = _WebSocketTerminalMiddleware(app.wsgi_app, authorized=authorized)
