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
 * Smoke test the launcher over the DevTools protocol.
 *
 * "The process is still alive" is not evidence the UI works: the renderer can throw on its first
 * line and leave a window that is merely blank, with the error only in DevTools. That is exactly
 * what a CommonJS `exports` marker in a classic script does, and it is how this test earned its
 * keep. So the assertions are about the DOM and the IPC bridge, not about the process.
 *
 * Usage::
 *
 *     npm run smoke
 *
 * It launches the app itself, waits for the debugging port, asserts, and shuts it down — a single
 * command, because a two-step "start it in the background, then test it" does not survive a shell
 * that reaps its background jobs, and a half-run smoke test reports a connection error rather than
 * a result.
 *
 * Uses Node's global `WebSocket` (Node 22+), so there is no dependency to install.
 */

import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PORT = process.env.CDP_PORT || "9222";
const APP_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");

/**
 * A throwaway profile, for two reasons that both bit.
 *
 * **The single-instance lock.** `main.ts` calls `requestSingleInstanceLock()`, and that lock lives
 * in the userData directory — so with the app already open (a developer's own window, or one left
 * from a debugging session) the instance this script spawns quits instantly and the run fails
 * with a confusing message about the page never appearing, or worse, silently tests the *other*
 * instance. A separate userData path makes this run genuinely independent.
 *
 * **Hermeticity.** Sharing the real profile meant the assertions depended on the operator's own
 * deployment list — "the seeded deployment rendered" is only true on a first run — and the tunnel
 * check wrote a record into it. A fresh directory is seeded from `seedDeployments()` every time,
 * which is exactly what the assertions describe.
 */
const PROFILE = mkdtempSync(join(tmpdir(), "greenlight-smoke-"));

/** The debugger's target list, or null while it is not listening yet. */
async function targets() {
  try {
    return await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
  } catch {
    return null;
  }
}

if ((await targets()) !== null) {
  // Never adopt a listener we did not start: it could be a stale instance on an older build, and
  // "12 checks passed" against yesterday's code is worse than a refusal.
  console.error(`something is already serving CDP on port ${PORT}; set CDP_PORT or close it`);
  rmSync(PROFILE, { recursive: true, force: true });
  process.exit(1);
}

const electron = join(APP_DIR, "node_modules", ".bin", "electron");
const child = spawn(
  electron,
  [".", `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`],
  { cwd: APP_DIR, stdio: "ignore", detached: false },
);
child.on("error", (err) => {
  console.error(`could not launch electron: ${err.message}`);
  process.exit(1);
});

/** Poll rather than sleep a fixed interval: startup time varies and a fixed wait is either slow or flaky. */
let page = null;
for (let attempt = 0; attempt < 60 && page === null; attempt += 1) {
  const list = await targets();
  page = list?.find((t) => t.type === "page" && t.url.includes("launcher/index.html")) ?? null;
  if (page === null) await new Promise((r) => setTimeout(r, 500));
}

function shutdown(code) {
  child.kill("SIGTERM");
  rmSync(PROFILE, { recursive: true, force: true });
  process.exit(code);
}

if (page === null) {
  console.error("the launcher page never appeared on the debugging port");
  shutdown(1);
}

const socket = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});

let nextId = 0;
function evaluate(expression) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const onMessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id !== id) return;
      socket.removeEventListener("message", onMessage);
      const details = msg.result?.exceptionDetails;
      if (details) return reject(new Error(details.exception?.description ?? details.text));
      resolve(msg.result?.result?.value);
    };
    socket.addEventListener("message", onMessage);
    socket.send(
      JSON.stringify({
        id,
        method: "Runtime.evaluate",
        // The renderer's own calls are async, so every probe has to be awaited rather than read
        // on the same tick -- a synchronous read races the first paint and fails intermittently.
        params: { expression, awaitPromise: true, returnByValue: true },
      }),
    );
  });
}

/**
 * Wait for the renderer's initial async load before asserting on the DOM.
 *
 * The launcher fills the footer and the list from two awaited IPC calls, so a probe that lands
 * between page-load and those resolving reads an empty document. That produced exactly one
 * intermittent failure ("footer reports a version" → `""`) while every other check passed —
 * a race in the test, not the app, and the kind that would be blamed on the app later.
 */
async function waitForReady() {
  // The footer is the readiness signal, not the list: a fresh profile ships with NO deployments
  // (see `seedDeployments`), so waiting for a row would hang forever on exactly the state this
  // suite is meant to assert.
  const ready = await evaluate(`(async () => {
    for (let i = 0; i < 100; i += 1) {
      if ((document.getElementById('about')?.textContent ?? '') !== '') return true;
      await new Promise((r) => setTimeout(r, 100));
    }
    return false;
  })()`);
  if (!ready) {
    console.error("the launcher never finished its initial load (footer stayed empty)");
    shutdown(1);
  }
}

await waitForReady();

const checks = [
  // The bridge exists at all. Absent means the preload did not load or contextIsolation is off.
  ["contextBridge is exposed", "typeof window.greenlight === 'object'", (v) => v === true],
  [
    "bridge surface is exactly the named channels",
    "Object.keys(window.greenlight).sort().join(',')",
    (v) =>
      v ===
      "appInfo,checkForUpdates,clearCertificatePin,connect,disconnect,listDeployments," +
        "onTunnelStatus,removeDeployment,saveDeployment",
  ],
  // The renderer executed: it is what fills the footer and the list.
  ["footer reports a version", "document.getElementById('about').textContent", (v) => /Greenlight \d/.test(v ?? "")],
  // A fresh install must ship with NO deployments: a pre-filled endpoint leaks one customer's
  // infrastructure to every other, and is a wrong-cluster hazard because nobody re-checks a row
  // they did not type. This asserts the shipped first-run state, so a seed cannot creep back in.
  ["a fresh profile has no deployments", "document.querySelectorAll('#list li').length", (v) => v === 0],
  ["the empty state explains what to do", "document.getElementById('empty').hidden === false && /ssh\\/config/.test(document.getElementById('empty').textContent)", (v) => v === true],
  // The IPC round trip, not just the shape of the bridge.
  [
    "listDeployments() round-trips over IPC",
    "window.greenlight.listDeployments().then(r => r.deployments.length + '|' + (r.warning ?? 'none'))",
    (v) => v === "0|none",
  ],
  // Add → list → remove, which is now the only way a deployment can exist. Covers the path an
  // operator actually takes on first launch.
  [
    "a deployment can be added, listed and removed",
    `(async () => {
       const dep = { id: 'smoke-add', label: 'Smoke Cluster', customer: 'SMOKE',
         kind: 'ssh-tunnel', sshAlias: 'smoke-alias', remoteHost: 'localhost',
         remotePort: 5056, localPort: 5198, scheme: 'https' };
       const saved = await window.greenlight.saveDeployment(dep);
       if (!saved.ok) return 'save-refused:' + saved.problems.join(';');
       const after = await window.greenlight.listDeployments();
       const row = after.deployments.find(d => d.id === 'smoke-add');
       await window.greenlight.removeDeployment('smoke-add');
       const gone = (await window.greenlight.listDeployments()).deployments.length;
       return (row ? row.origin : 'missing') + '|' + gone;
     })()`,
    (v) => v === "https://localhost:5198|0",
  ],
  [
    "appInfo() reports the runtime",
    "window.greenlight.appInfo().then(i => i.electron.split('.')[0])",
    (v) => Number(v) >= 38,
  ],
  // Validation runs in the main process, so a bad record cannot be persisted by the renderer.
  [
    "saveDeployment() refuses an invalid record",
    "window.greenlight.saveDeployment({id:'x',label:'',customer:'c',kind:'ssh-tunnel'})" +
      ".then(r => r.ok + '|' + r.problems.length)",
    (v) => typeof v === "string" && v.startsWith("false|") && Number(v.split("|")[1]) >= 3,
  ],
  [
    "port 5000 is refused with the AirPlay reason",
    "window.greenlight.saveDeployment({id:'y',label:'L',customer:'C',kind:'ssh-tunnel'," +
      "sshAlias:'a',remoteHost:'localhost',remotePort:1,localPort:5000})" +
      ".then(r => r.problems.join(' '))",
    (v) => typeof v === "string" && v.includes("AirPlay"),
  ],
  [
    "an sshAlias starting with '-' is refused",
    "window.greenlight.saveDeployment({id:'z',label:'L',customer:'C',kind:'ssh-tunnel'," +
      "sshAlias:'-oProxyCommand=touch /tmp/pwned',remoteHost:'localhost',remotePort:1,localPort:6000})" +
      ".then(r => r.ok)",
    (v) => v === false,
  ],
  // End-to-end through the tunnel supervisor: a real `ssh` is spawned, fails, and its stderr is
  // classified into an instruction. Deterministic because the alias cannot resolve anywhere, and
  // fast because `BatchMode=yes` refuses instead of prompting -- which is the flag's whole point.
  // Adds and removes its own record so the operator's list is unchanged.
  [
    "a bogus ssh alias fails fast with an actionable remedy",
    `(async () => {
       const dep = { id: 'smoke-bogus', label: 'Smoke bogus', customer: 'SMOKE',
         kind: 'ssh-tunnel', sshAlias: 'nvis-smoke-nonexistent-host', remoteHost: 'localhost',
         remotePort: 5056, localPort: 5199, scheme: 'http' };
       const saved = await window.greenlight.saveDeployment(dep);
       if (!saved.ok) return 'save-refused:' + saved.problems.join(';');
       const status = await window.greenlight.connect(dep.id);
       await window.greenlight.removeDeployment(dep.id);
       return status.state + '|' + (status.remedy ?? status.detail);
     })()`,
    (v) => typeof v === "string" && v.startsWith("failed|") && /ssh\/config|resolve|terminal/i.test(v),
  ],
];

let failures = 0;
for (const [name, expression, predicate] of checks) {
  try {
    const value = await evaluate(expression);
    if (predicate(value)) {
      console.log(`  ok    ${name}`);
    } else {
      failures += 1;
      console.log(`  FAIL  ${name}\n          got: ${JSON.stringify(value)}`);
    }
  } catch (err) {
    failures += 1;
    console.log(`  ERROR ${name}\n          ${err.message}`);
  }
}

socket.close();
console.log(failures === 0 ? `\n${checks.length} checks passed` : `\n${failures} check(s) failed`);
shutdown(failures === 0 ? 0 : 1);
