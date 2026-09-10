/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 *
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */

/**
 * SSH port-forward supervision: bring up `ssh -L`, prove it carries traffic, and tear it down.
 *
 * This is the reason the desktop client exists. Opening a URL is a bookmark; holding the tunnel
 * open, naming which cluster it reaches and saying something legible when it fails is a tool.
 *
 * **We shell out to the operator's own `ssh`, and never speak the protocol ourselves.** Every
 * cluster in `cluster-deployments.mdc` needs something an embedded SSH library would have to
 * re-learn: `ProxyJump` through `meljump`, a Teleport certificate for the Nscale jumphosts, an
 * `IdentityFile` per kit, and `ControlMaster` reuse. `~/.ssh/config` already expresses all of it
 * and the operator already maintains it.
 *
 * **The password hop is designed for, not worked around.** Nscale TX authenticates hop 2 with a
 * password, so an unattended process cannot open a fresh session — the documented workflow is that
 * a human runs `ssh nscale-tx-ova` once and everything else rides the `ControlMaster`. So we pass
 * `BatchMode=yes` to make that case fail *immediately and legibly* instead of blocking forever on
 * a prompt nobody can see, and then tell the operator the exact command to run.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { createConnection } from "node:net";

import { originFor, validateDeployment, type Deployment } from "./deployments";

export type TunnelState = "idle" | "starting" | "up" | "failed";

export interface TunnelStatus {
  deploymentId: string;
  state: TunnelState;
  origin: string;
  /** Operator-facing detail. On `failed`, why; on `up`, how it came up. */
  detail: string;
  /** Set when the failure is fixable by the operator running one command. */
  remedy?: string;
}

interface ActiveTunnel {
  child: ChildProcess;
  status: TunnelStatus;
}

const active = new Map<string, ActiveTunnel>();
type Listener = (status: TunnelStatus) => void;
const listeners = new Set<Listener>();

export function onTunnelStatus(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function publish(status: TunnelStatus): void {
  for (const listener of listeners) listener(status);
}

/**
 * Per-hop TCP connect timeout handed to ssh.
 *
 * **Load-bearing, and measured.** Without it ssh inherits the OS default: an unreachable host took
 * **75 seconds** to fail here, because these names resolve through a search domain and then sit in
 * a TCP connect. Ten seconds is generous for a single hop even through Teleport, since this bounds
 * each connect rather than the whole chain.
 */
const CONNECT_TIMEOUT_SECONDS = 10;

/**
 * How long to wait for the forward to start accepting connections.
 *
 * **Deliberately longer than `CONNECT_TIMEOUT_SECONDS`**, so that when ssh is going to fail, *its*
 * diagnosis arrives first and gets classified into an instruction. Set the other way round -- as
 * it was at 20 s against the OS connect default -- this timer wins every race and the operator is
 * told "the forward did not start accepting connections", which is true, useless, and hides the
 * real reason ssh was about to give.
 */
const READY_TIMEOUT_MS = 30_000;
/** Gap between readiness probes. */
const PROBE_INTERVAL_MS = 250;

/**
 * Does *something* accept a TCP connection on this local port?
 *
 * A connect probe rather than an HTTP request on purpose. `ssh -L` binds the listener immediately
 * but only dials the far side on first use, so an HTTP probe would be the request that triggers
 * the forward and its failure mode would be indistinguishable from the dashboard being down. This
 * asks the narrower question that actually has a clear answer.
 */
function probePort(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = createConnection({ host: "127.0.0.1", port });
    const settle = (ok: boolean) => {
      socket.destroy();
      resolve(ok);
    };
    socket.setTimeout(2_000);
    socket.once("connect", () => settle(true));
    socket.once("timeout", () => settle(false));
    socket.once("error", () => settle(false));
  });
}

/**
 * Is a `ControlMaster` already established for this alias?
 *
 * `ssh -O check` exits 0 when a master is running. Used only to write a better error message:
 * `cluster-deployments.mdc` notes that a master is what makes the password-hop clusters usable at
 * all, so "no master and BatchMode refused you" is a completely different instruction to the
 * operator than "your key was rejected".
 *
 * Note that a *running* master is not proof of a *healthy* one —
 * `docs/EXECUTION_ARCHITECTURE.md` §6.1 records that a stale master reports `Master running`
 * while refusing new sessions, and never self-heals. That is why this only shapes the message and
 * is never treated as success.
 */
function hasControlMaster(alias: string): Promise<boolean> {
  return new Promise((resolve) => {
    const probe = spawn("ssh", ["-O", "check", alias], { stdio: "ignore" });
    probe.once("error", () => resolve(false));
    probe.once("exit", (code) => resolve(code === 0));
  });
}

function sshArgs(dep: Deployment): string[] {
  return [
    // No remote command and no TTY: this process exists only to hold the forward.
    "-N",
    "-T",
    // Fail instead of prompting. See the module docstring -- this is what turns the Nscale TX
    // password hop from an invisible hang into an error we can explain.
    "-o",
    "BatchMode=yes",
    // Without this, ssh happily connects while the -L bind fails (port already in use), leaving a
    // session that looks up and forwards nothing. The window would then load the *other* thing
    // listening on that port, which on a developer machine is plausibly a different cluster.
    "-o",
    "ExitOnForwardFailure=yes",
    // See CONNECT_TIMEOUT_SECONDS: without this an unreachable host takes 75 s to fail, which is
    // longer than any readiness window worth having.
    "-o",
    `ConnectTimeout=${CONNECT_TIMEOUT_SECONDS}`,
    // Notice a dead peer rather than holding a black-hole forward open for the TCP default.
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
    "-L",
    `127.0.0.1:${dep.localPort}:${dep.remoteHost}:${dep.remotePort}`,
    dep.sshAlias as string,
  ];
}

/** Classify ssh's stderr into something an operator can act on. */
function explainFailure(stderr: string, dep: Deployment, masterPresent: boolean): Omit<TunnelStatus, "deploymentId" | "state" | "origin"> {
  const text = stderr.toLowerCase();

  if (text.includes("address already in use") || text.includes("cannot listen to port")) {
    return {
      detail: `Local port ${dep.localPort} is already in use, so the forward could not bind.`,
      remedy: `Find the holder with \`lsof -nP -iTCP:${dep.localPort} -sTCP:LISTEN\`, or change this deployment's local port.`,
    };
  }
  if (text.includes("permission denied") || text.includes("batch mode") || text.includes("host key verification failed")) {
    if (!masterPresent) {
      return {
        detail:
          "SSH refused to authenticate without prompting, and no ControlMaster is established " +
          "for this alias.",
        remedy: `Run \`ssh ${dep.sshAlias} 'echo ok'\` in a terminal once, answer any prompt, then connect again.`,
      };
    }
    return {
      detail: "SSH refused to authenticate even though a ControlMaster exists — it may be stale.",
      remedy:
        "A stale master reports itself as running but refuses new sessions and never recovers. " +
        `Close it with \`ssh -O exit ${dep.sshAlias}\`, then re-establish it in a terminal.`,
    };
  }
  if (text.includes("could not resolve hostname") || text.includes("name or service not known")) {
    return {
      detail: `SSH could not resolve "${dep.sshAlias}".`,
      remedy: `Check that \`Host ${dep.sshAlias}\` exists in ~/.ssh/config.`,
    };
  }
  // Distinct from the resolve failure above, and easy to conflate. A typo'd alias usually does NOT
  // fail to resolve -- a search domain answers for it -- so it reaches a TCP connect and times out
  // instead. Both are "wrong name", but only this one also means "right name, host is down".
  if (
    text.includes("operation timed out") ||
    text.includes("connection timed out") ||
    text.includes("connect to host") ||
    text.includes("no route to host")
  ) {
    return {
      detail:
        `SSH resolved "${dep.sshAlias}" but could not reach it within ` +
        `${CONNECT_TIMEOUT_SECONDS}s.`,
      remedy:
        "Either the host is down or the alias resolves somewhere unintended — a mistyped name " +
        `often resolves through a search domain. Confirm with \`ssh -v ${dep.sshAlias}\`.`,
    };
  }
  if (text.includes("connection closed by unknown port")) {
    return {
      detail:
        "The jumphost closed the connection before authenticating, which usually means the " +
        "Teleport jumphost is offline rather than that your certificate is wrong.",
      remedy: "Check the jumphost is up, then retry.",
    };
  }
  return {
    detail: stderr.trim().split("\n").slice(-4).join(" ") || "ssh exited without explanation.",
  };
}

/**
 * Bring the tunnel up and resolve once it is carrying traffic — or reject with an explanation.
 *
 * Idempotent: a second call for a deployment already up returns the existing status rather than
 * opening a duplicate forward, so a double-click on Connect cannot leave an orphan ssh holding a
 * session against the head node's `MaxSessions` budget.
 */
export async function openTunnel(dep: Deployment): Promise<TunnelStatus> {
  const origin = originFor(dep);

  if (dep.kind === "direct") {
    return { deploymentId: dep.id, state: "up", origin, detail: "Direct connection; no tunnel needed." };
  }

  const existing = active.get(dep.id);
  if (existing && existing.status.state === "up") return existing.status;

  const problems = validateDeployment(dep);
  if (problems.length > 0) {
    const status: TunnelStatus = {
      deploymentId: dep.id,
      state: "failed",
      origin,
      detail: `This deployment is not configured correctly: ${problems.join("; ")}.`,
    };
    publish(status);
    return status;
  }

  publish({ deploymentId: dep.id, state: "starting", origin, detail: `Opening ssh -L via ${dep.sshAlias}…` });

  const masterPresent = await hasControlMaster(dep.sshAlias as string);
  const child = spawn("ssh", sshArgs(dep), { stdio: ["ignore", "ignore", "pipe"] });

  let stderr = "";
  child.stderr?.on("data", (chunk: Buffer) => {
    // Bounded: a flapping connection can emit indefinitely, and this is only ever read as the
    // tail of an error message.
    stderr = `${stderr}${chunk.toString()}`.slice(-8_192);
  });

  const status: TunnelStatus = { deploymentId: dep.id, state: "starting", origin, detail: "" };
  active.set(dep.id, { child, status });

  let exited = false;
  child.once("error", (err) => {
    exited = true;
    stderr += `\n${err.message}`;
  });
  child.once("exit", () => {
    exited = true;
    // An exit *after* we reported "up" is a drop, not a startup failure, and the window is still
    // open pointing at a dead port -- so it has to be announced rather than logged.
    if (status.state === "up") {
      const dropped: TunnelStatus = {
        deploymentId: dep.id,
        state: "failed",
        origin,
        detail: "The SSH tunnel dropped.",
        remedy: "Reconnect from the launcher; the window will reload once the tunnel is back.",
      };
      active.delete(dep.id);
      Object.assign(status, dropped);
      publish(dropped);
    }
  });

  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (exited) break;
    if (await probePort(dep.localPort as number)) {
      const up: TunnelStatus = {
        deploymentId: dep.id,
        state: "up",
        origin,
        detail: `Forwarding ${origin} → ${dep.remoteHost}:${dep.remotePort} via ${dep.sshAlias}.`,
      };
      Object.assign(status, up);
      publish(up);
      return up;
    }
    await new Promise((r) => setTimeout(r, PROBE_INTERVAL_MS));
  }

  closeTunnel(dep.id);
  const failure: TunnelStatus = {
    deploymentId: dep.id,
    state: "failed",
    origin,
    ...(exited
      ? explainFailure(stderr, dep, masterPresent)
      : { detail: `The forward did not start accepting connections within ${READY_TIMEOUT_MS / 1000}s.` }),
  };
  publish(failure);
  return failure;
}

/** Tear a tunnel down. Safe to call for an id that is not up. */
export function closeTunnel(deploymentId: string): void {
  const entry = active.get(deploymentId);
  if (!entry) return;
  active.delete(deploymentId);
  // SIGTERM is enough for `ssh -N`; it has no work in flight to lose.
  entry.child.kill("SIGTERM");
}

export function tunnelStatus(deploymentId: string): TunnelStatus | undefined {
  return active.get(deploymentId)?.status;
}

/**
 * Close every tunnel. Wired to app quit.
 *
 * Not housekeeping: each forward holds a session against the head node's `sshd MaxSessions`,
 * which `core-designs.mdc` records as unset — hence the OpenSSH default of 10 — and which the
 * product's own fan-out already competes for. Leaking these on quit would eat the same budget
 * that a fleet sweep needs, and the symptom over there is nodes silently dropping out of a scan.
 */
export function closeAllTunnels(): void {
  for (const id of [...active.keys()]) closeTunnel(id);
}
