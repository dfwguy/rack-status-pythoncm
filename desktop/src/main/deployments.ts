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
 * The desktop client's deployment list: which clusters this operator can open, and how to reach
 * each one.
 *
 * **Why this is client-side config and not the catalog.** `Project`/`Cluster` in
 * `backend/catalog/` owns "which cluster, which nodes, which customer" for the product, and
 * `core-designs.mdc` citizen 3 says to put site-specific facts there. This file is not a second
 * copy of that: it holds only what is needed *before any backend is reachable* — the address and
 * the tunnel. Reading it from the catalog API would require already being connected to the
 * cluster whose address we are trying to look up. Once a window is open, the catalog remains the
 * authority for everything about the cluster itself.
 */

import { app } from "electron";
import { closeSync, openSync, readFileSync, renameSync, writeSync } from "node:fs";
import { join } from "node:path";

/** How a deployment is reached. */
export type ConnectionKind = "direct" | "ssh-tunnel";

export interface Deployment {
  /** Stable id. Also the window identity, so it must not change when the label is edited. */
  id: string;
  /** What the operator sees, e.g. "Nscale TX (GB300)". */
  label: string;
  /** Customer name, shown in the window title so two clusters are never confused. */
  customer: string;
  kind: ConnectionKind;

  /** `kind === "direct"`: the full origin, e.g. `https://nvis.example:8443`. */
  url?: string;

  /**
   * Scheme for a tunnelled deployment. Defaults to `https`.
   *
   * **Greenlight refuses to serve without TLS**, so `https` is the correct default:
   * `backend/startup_gate.py` lists a missing certificate as a fault and `ensure_tls()`
   * generates a self-signed one on first boot rather than letting the operator opt out. The
   * browser needs a secure context for `crypto.subtle` (SRP login) anyway.
   *
   * The field exists at all because a container deployed *before* that gate landed is still
   * serving plain HTTP until it is rebuilt, and pointing an `https://` origin at it fails in a way
   * that looks like a tunnel fault rather than a scheme mismatch.
   */
  scheme?: "https" | "http";

  /**
   * Pinned certificate fingerprint, in Electron's `sha256/<base64>` form.
   *
   * Self-signed is the documented default, so *every* deployment presents a certificate no CA
   * vouches for. A browser answers that with an interstitial the operator clicks through, which
   * trains them to click through certificate warnings — the opposite of the intent. Pinning the
   * fingerprint on first connect, after the operator confirms it once, gets a real trust decision
   * and then enforces it on every later launch.
   *
   * Empty means "not yet trusted", which is refused rather than allowed.
   */
  certFingerprint?: string;

  /**
   * `kind === "ssh-tunnel"`: an alias from the operator's own `~/.ssh/config`.
   *
   * **An alias, deliberately, and never a host/user/key triple.** Delegating to `~/.ssh/config`
   * inherits `ProxyJump`, `IdentityFile`, Teleport certificates and `ControlMaster` for free —
   * all of which these clusters need and none of which we want to reimplement. It also matters
   * for correctness on Nscale, where `cluster-deployments.mdc` records that the IP address is
   * *not* cluster identity: every kit numbers its hosts out of the same `192.168.10.0/2x` behind
   * a different jumphost, so `192.168.10.3` names one cluster via `device2` and a different one
   * via `device100`. The alias is what pins the jumphost, so the alias is what we store.
   */
  sshAlias?: string;
  /** Host as seen *from* the SSH target. Usually `localhost` — the container runs on that host. */
  remoteHost?: string;
  /** Port the dashboard listens on over there. */
  remotePort?: number;
  /**
   * Local end of the forward. **Pinned per deployment, never auto-allocated.**
   *
   * The browser origin is `http://localhost:<localPort>`, and the origin is the identity for the
   * session cookie and for `nvis-rack-client-id` in `localStorage`. Picking a free port at launch
   * would therefore sign the operator out and reset their client id every single time, which
   * would read as "the desktop app keeps logging me out" rather than as a port change.
   */
  localPort?: number;

  /** Free-text operator note, e.g. a password-hop warning. */
  notes?: string;
}

export interface DeploymentsFile {
  version: 1;
  deployments: Deployment[];
}

/**
 * The list a brand-new install starts with: **empty**.
 *
 * This shipped with a hard-coded Nscale TX entry during development, and that was a bug waiting
 * to happen for two reasons:
 *
 *   * **It leaks one customer's infrastructure to all the others.** The entry carried a jumphost
 *     alias, an internal port and a note naming an internal host and its authentication. One
 *     binary goes to Mayo, Firmus, Boston and Nscale, so anything baked in here is disclosed to
 *     every operator of every other site.
 *   * **A pre-filled endpoint is a wrong-cluster hazard.** `cluster-deployments.mdc` opens by
 *     warning that the wrong host silently hits the wrong cluster, and an entry the operator did
 *     not type is one they have no reason to double-check. A plausible-looking row that connects
 *     somewhere unintended is worse than no row at all, because the label is believed.
 *
 * So the operator adds their own, and the launcher's empty state tells them how. Cluster
 * connection details belong in `cluster-deployments.mdc` and `desktop/README.md` — documentation
 * a person reads and types from — never compiled into a binary that ships to other customers.
 *
 * An admin deploying to a fleet should pre-place `deployments.json` in the app's userData
 * directory rather than reintroducing a seed here; the loader below respects an existing file.
 */
function seedDeployments(): Deployment[] {
  return [];
}

function configPath(): string {
  return join(app.getPath("userData"), "deployments.json");
}

/** Ports we refuse to bind locally, with the reason the operator needs to hear. */
const RESERVED_LOCAL_PORTS = new Map<number, string>([
  // From cluster-deployments.mdc: macOS's AirPlay Receiver owns 5000 and answers 403 Forbidden,
  // which looks exactly like an auth failure in the dashboard rather than a port collision.
  [5000, "macOS AirPlay Receiver owns port 5000 and answers 403; pick another local port"],
]);

/**
 * Reject a deployment that cannot work, rather than storing it and failing at connect time.
 *
 * Returns a list of human-readable problems; empty means valid.
 */
export function validateDeployment(dep: Deployment): string[] {
  const problems: string[] = [];
  if (!dep.label.trim()) problems.push("label is required");

  if (dep.kind === "direct") {
    if (!dep.url) {
      problems.push("a direct deployment needs a url");
    } else {
      try {
        const parsed = new URL(dep.url);
        if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
          problems.push(`url scheme must be http or https, not ${parsed.protocol}`);
        }
      } catch {
        problems.push(`url is not a valid URL: ${dep.url}`);
      }
    }
    return problems;
  }

  // ssh-tunnel
  if (!dep.sshAlias) {
    problems.push("an ssh-tunnel deployment needs an sshAlias from your ~/.ssh/config");
  } else if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(dep.sshAlias)) {
    // The alias becomes an argv element, not a shell word, so this is not about shell quoting.
    // It is about a value beginning with "-" being parsed by ssh as an *option* — the one way an
    // alias string can change what the command does.
    problems.push(
      `sshAlias "${dep.sshAlias}" must start alphanumeric and contain only letters, digits, ` +
        "dot, dash or underscore",
    );
  }
  for (const [name, value] of [
    ["remotePort", dep.remotePort],
    ["localPort", dep.localPort],
  ] as const) {
    if (!Number.isInteger(value) || (value as number) < 1 || (value as number) > 65535) {
      problems.push(`${name} must be an integer between 1 and 65535`);
    }
  }
  if (dep.localPort !== undefined) {
    const reserved = RESERVED_LOCAL_PORTS.get(dep.localPort);
    if (reserved) problems.push(reserved);
  }
  if (!dep.remoteHost?.trim()) problems.push("remoteHost is required (usually 'localhost')");
  return problems;
}

/** The origin a deployment's window should load. */
export function originFor(dep: Deployment): string {
  if (dep.kind === "direct") return (dep.url ?? "").replace(/\/+$/, "");
  // `localhost`, not 127.0.0.1, and that is not cosmetic: `bin/gen-tls-cert.py` adds SANs for
  // localhost, 127.0.0.1 and ::1 unconditionally, so either would validate -- but `localhost` is
  // a secure context in every browser rule, which SRP's use of crypto.subtle depends on.
  return `${dep.scheme ?? "https"}://localhost:${dep.localPort}`;
}

/**
 * Load the list, seeding and persisting it on first run.
 *
 * A malformed or unreadable file is reported and replaced with the seed rather than crashing the
 * app: the operator's way out of a bad config should not be "find the JSON and hand-edit it
 * before the launcher will start".
 */
export function loadDeployments(): { file: DeploymentsFile; warning?: string } {
  const path = configPath();
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    const file: DeploymentsFile = { version: 1, deployments: seedDeployments() };
    saveDeployments(file);
    return { file };
  }

  try {
    const parsed = JSON.parse(raw) as unknown;
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      !Array.isArray((parsed as DeploymentsFile).deployments)
    ) {
      throw new Error("missing a `deployments` array");
    }
    return { file: parsed as DeploymentsFile };
  } catch (err) {
    const file: DeploymentsFile = { version: 1, deployments: seedDeployments() };
    return {
      file,
      warning:
        `${path} could not be read (${(err as Error).message}), so the default list is in ` +
        "use. Your previous entries are still in that file and were not overwritten.",
    };
  }
}

/**
 * Persist atomically.
 *
 * `renameSync` over a temp file, so a crash mid-write cannot leave a truncated list — the failure
 * mode being avoided is an operator losing every deployment because the app quit while saving.
 * The temp is created with an explicit mode rather than inheriting the umask: no secret lives in
 * here (aliases, not keys), but `identity-auth-traps.mdc` trap 3 is that `os.replace` installs the
 * *temp file's* mode, so getting into the habit of stating it is what stops the next file — one
 * that does hold a secret — from being world-readable.
 */
export function saveDeployments(file: DeploymentsFile): void {
  const path = configPath();
  const tmp = `${path}.tmp`;
  const fd = openSync(tmp, "w", 0o600);
  try {
    writeSync(fd, `${JSON.stringify(file, null, 2)}\n`);
  } finally {
    closeSync(fd);
  }
  renameSync(tmp, path);
}

export function deploymentsConfigPath(): string {
  return configPath();
}
