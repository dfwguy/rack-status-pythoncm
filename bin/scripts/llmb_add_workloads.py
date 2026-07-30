#!/usr/bin/env python3
"""Drive `llmb-install`'s incremental (add-workloads) flow non-interactively.

Why this exists: llmb-install refuses a headless `--play` against an existing
install ("Existing LLMB installation found ... remove the existing
installation"), and `llmb-install express` refuses too ("Express mode does not
support incremental installation"). The ONLY supported way to add workloads to
a live install is the interactive menu, which needs a tty. Rack Status runs
over a non-interactive SSH exec, so we allocate a pty and answer the two
prompts.

The flow, verified against llmb-install 1.13.1 on this cluster:

  1. "What would you like to do?"        -> default row is
     "Add workloads to this installation", so a bare <enter> takes it.
  2. "Select workloads to install (use space to select, enter to confirm)"
     -> the cursor starts on "Install all workloads"; <space> ticks it and
     <enter> confirms. Selecting the aggregate row is deliberately more robust
     than arrowing to N individual rows, whose order shifts as workloads get
     installed (already-installed ones are filtered out of the list).

Already-installed workloads are excluded by llmb-install itself, so this is
additive and idempotent: nothing is removed or reinstalled.

PATH note: the head node's non-login SSH shell lacks ~/.local/bin, where
git-lfs lives. Without it llmb-install aborts with "Git LFS is not installed"
even though it is. We prepend it.

Usage:  llmb_add_workloads.py <install_path> [timeout_sec]
Exit:   0 install finished, 2 timed out, 3 llmb-install reported failure.
"""
import os
import pty
import re
import select
import signal
import sys
import time

INSTALL_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/cmods/workspace/llmb"
TIMEOUT = int(sys.argv[2]) if len(sys.argv) > 2 else 14400
VENV_BIN = os.environ.get("LLMB_VENV_BIN", "/home/cmods/workspace/llmb_venv/bin")

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
MENU_Q = "What would you like to do"
SELECT_Q = "Select workloads to install"
DONE_MARKERS = ("Install Summary", "Installation Summary", "installation complete",
                "Installation complete", "All workloads installed")
FAIL_MARKERS = ("Install Summary: FAILED", "Installation did not complete")


def main() -> int:
    if not os.path.isdir(INSTALL_PATH):
        print(f"ERROR: install path does not exist: {INSTALL_PATH}", flush=True)
        return 3
    os.chdir(INSTALL_PATH)

    env = dict(os.environ)
    env["PATH"] = f"{VENV_BIN}:{os.path.expanduser('~')}/.local/bin:" + env.get("PATH", "")
    env["TERM"] = "dumb"

    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("llmb-install", ["llmb-install", "--ui-mode", "simple"], env)

    seen = ""          # ANSI-stripped transcript, for prompt detection
    answered_menu = False
    answered_select = False
    start = time.time()
    rc = 2

    while time.time() - start < TIMEOUT:
        r, _, _ = select.select([fd], [], [], 5)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            seen += ANSI.sub("", text)
            seen = seen[-20000:]

        if not answered_menu and MENU_Q in seen:
            time.sleep(1.0)
            os.write(fd, b"\r")            # take the default: add workloads
            answered_menu = True
            continue

        if answered_menu and not answered_select and SELECT_Q in seen:
            time.sleep(1.5)
            os.write(fd, b" ")             # tick "Install all workloads"
            time.sleep(0.5)
            os.write(fd, b"\r")            # confirm
            answered_select = True
            continue

        if answered_select:
            if any(m in seen for m in FAIL_MARKERS):
                rc = 3
                break
            if any(m in seen for m in DONE_MARKERS):
                rc = 0
                break

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    print(f"\n=== llmb_add_workloads exit={rc} "
          f"(menu={answered_menu} select={answered_select}) ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
