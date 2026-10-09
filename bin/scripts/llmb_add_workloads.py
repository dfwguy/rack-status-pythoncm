#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Drive `llmb-install`'s incremental (add-workloads) flow non-interactively.

Why this exists: llmb-install refuses a headless `--play` against an existing
install ("Existing LLMB installation found ... remove the existing
installation"), and `llmb-install express` refuses too ("Express mode does not
support incremental installation"). The ONLY supported way to add workloads to
a live install is the interactive menu, which needs a tty. Rack Status runs
over a non-interactive SSH exec, so we allocate a pty and answer the prompts.

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

HOW COMPLETION IS DECIDED -- and why not by string matching
-----------------------------------------------------------
Earlier versions broke out of the read loop as soon as the transcript contained
a "done"-ish string ("Install Summary", "Successfully installed", ...) and then
SIGKILLed llmb-install. Those strings are printed routinely by uv/pip and after
every container import, so on 2026-07-30/31 the driver killed the installer
seconds after its FIRST `enroot import` finished -- three times, ~115 GB of
downloads, and `cluster_config.yaml` was never written. It then reported exit 0.

So: we never kill on a string. We read until the pty closes, reap the child, and
use its REAL exit status. Then we re-read `cluster_config.yaml` and report which
workloads actually appeared. A caller that passes --expect gets a non-zero exit
when a requested workload is still missing, however cleanly llmb-install exited.

PATH note: the head node's non-login SSH shell lacks ~/.local/bin, where
git-lfs lives. Without it llmb-install aborts with "Git LFS is not installed"
even though it is. We prepend it.

Usage:  llmb_add_workloads.py <install_path> [timeout_sec] [--expect w1,w2]
Exit:   0 llmb-install exited 0 and every expected workload is installed
        2 timed out (child killed)
        3 llmb-install exited non-zero
        4 llmb-install exited 0 but an expected workload is still missing
"""
import os
import pty
import re
import select
import signal
import sys
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
MENU_Q = "What would you like to do"
SELECT_Q = "Select workloads to install"
# Shown instead of MENU_Q when a previous incremental attempt did not finish (llmb-install
# records the pending set). Default row is "Yes, resume incremental installation", which is
# what we want -- it keeps the already-chosen workload set and carries on.
RESUME_Q = "Continue adding workloads to existing installation"


def installed_workloads(install_path):
    """The `workloads.installed` list from cluster_config.yaml.

    Deliberately a small hand parser: this script runs under the head node's bare
    system python3, which has no PyYAML.
    """
    path = os.path.join(install_path, "cluster_config.yaml")
    found, in_installed = [], False
    try:
        with open(path) as fh:
            for line in fh:
                if re.match(r"^\s*installed:\s*$", line):
                    in_installed = True
                    continue
                if in_installed:
                    m = re.match(r"^\s*-\s*(\S+)\s*$", line)
                    if m:
                        found.append(m.group(1))
                    elif line.strip():
                        break
    except OSError:
        pass
    return found


def main() -> int:
    args = list(sys.argv[1:])
    expect = []
    if "--expect" in args:
        i = args.index("--expect")
        expect = [w.strip() for w in args[i + 1].split(",") if w.strip()] if i + 1 < len(args) else []
        del args[i:i + 2]

    # Defaults follow the cluster's workspace (project setting -> NVIS_WORKSPACE_DIR), the same
    # derivation as workspace-dir-common.sh: <workspace>/llmb and <workspace>/llmb_venv.
    workspace = (os.environ.get("NVIS_WORKSPACE_DIR") or os.environ.get("RACK_WORKSPACE_DIR") or "").rstrip("/")
    install_path = args[0] if args else (os.environ.get("LLMB_INSTALL") or (f"{workspace}/llmb" if workspace else ""))
    if not install_path:
        print("ERROR: no LLMB install path: pass it, or configure the cluster's workspace "
              "(Projects -> cluster -> Workspace) / export NVIS_WORKSPACE_DIR", flush=True)
        return 2
    timeout = int(args[1]) if len(args) > 1 else 14400
    venv_bin = os.environ.get("LLMB_VENV_BIN") or (
        f"{os.environ['LLMB_VENV']}/bin" if os.environ.get("LLMB_VENV")
        else f"{workspace}/llmb_venv/bin" if workspace else "")

    if not os.path.isdir(install_path):
        print(f"ERROR: install path does not exist: {install_path}", flush=True)
        return 3
    os.chdir(install_path)

    before = installed_workloads(install_path)
    print(f"=== workloads before: {before or '(none)'} ===", flush=True)

    env = dict(os.environ)
    env["PATH"] = f"{venv_bin}:{os.path.expanduser('~')}/.local/bin:" + env.get("PATH", "")
    env["TERM"] = "dumb"

    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("llmb-install", ["llmb-install", "--ui-mode", "simple"], env)

    seen = ""          # ANSI-stripped transcript, for prompt detection only
    answered_menu = False
    answered_resume = False
    answered_select = False
    start = time.time()
    timed_out = False

    # Read until the pty closes. llmb-install owns the decision to finish; our only
    # job is to answer prompts and stay out of the way. No completion strings.
    while True:
        if time.time() - start >= timeout:
            timed_out = True
            break
        try:
            r, _, _ = select.select([fd], [], [], 5)
        except (OSError, ValueError):
            break
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:      # EIO on Linux = the child closed the pty
                break
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            seen = (seen + ANSI.sub("", text))[-20000:]

        # Clearing `seen` after each answer keeps a prompt from being re-detected in
        # scrollback, which would send a stray keystroke into the next screen.
        if not answered_menu and MENU_Q in seen:
            time.sleep(1.0)
            os.write(fd, b"\r")            # take the default: add workloads
            answered_menu = True
            seen = ""
            continue

        # Resume path: a prior attempt left a pending set. Default row is
        # "Yes, resume incremental installation" -- take it and keep the set.
        if not answered_resume and RESUME_Q in seen:
            time.sleep(1.0)
            os.write(fd, b"\r")
            answered_resume = True
            answered_menu = True           # resume replaces the top-level menu
            seen = ""
            continue

        if answered_menu and not answered_select and SELECT_Q in seen:
            time.sleep(1.5)
            os.write(fd, b" ")             # tick "Install all workloads"
            time.sleep(0.5)
            os.write(fd, b"\r")            # confirm
            answered_select = True
            seen = ""
            continue

    if timed_out:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        child_rc = None
    else:
        try:
            _, status = os.waitpid(pid, 0)
            child_rc = os.waitstatus_to_exitcode(status)
        except (ChildProcessError, OSError):
            child_rc = None

    after = installed_workloads(install_path)
    added = [w for w in after if w not in before]
    missing = [w for w in expect if w not in after]

    if timed_out:
        rc = 2
    elif child_rc not in (0, None):
        rc = 3
    elif missing:
        rc = 4
    else:
        rc = 0

    print(f"\n=== llmb_add_workloads exit={rc} child_rc={child_rc} "
          f"(menu={answered_menu} resume={answered_resume} select={answered_select}) ===",
          flush=True)
    print(f"=== workloads after: {after or '(none)'} ===", flush=True)
    print(f"=== added: {added or '(none)'} ===", flush=True)
    if missing:
        print(f"=== STILL MISSING (expected): {missing} ===", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
