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
 * Assert the application menu still carries the clipboard roles.
 *
 * **This exists because the bug shipped.** `main.ts` replaces Electron's default menu with a
 * minimal one, and the first version omitted `role: "editMenu"`. On every platform Electron wires
 * Cmd/Ctrl+C, V, X, A and Z through the Edit menu's *roles* — there is no separate keyboard layer
 * — so copy and paste stopped working in every text field: the launcher's form, the dashboard's
 * inputs and the xterm terminal. Nothing logged an error, and it reads to a user as though the app
 * is deliberately blocking paste.
 *
 * A comment in `main.ts` asks the next person not to remove it; this makes the request enforceable.
 *
 * **Why the Node inspector and not CDP.** `--remote-debugging-port` reaches renderers only, and
 * menu accelerators are handled in the native layer — verified the hard way: a synthetic
 * `Input.dispatchKeyEvent` for Cmd+V does nothing, while the same harness sending a plain `x`
 * lands it in the focused field. So a renderer-side test cannot see this at all. `--inspect`
 * attaches to the main process, where `Menu.getApplicationMenu()` is the authority.
 */
import { spawn, execSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const APP_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const PROFILE = mkdtempSync(join(tmpdir(), "greenlight-menu-"));
const PORT = "9333";


const child = spawn(
  `${APP_DIR}/node_modules/.bin/electron`,
  [`--inspect=${PORT}`, ".", `--user-data-dir=${PROFILE}`],
  { cwd: APP_DIR, stdio: "ignore" },
);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let target = null;
for (let i = 0; i < 60 && !target; i += 1) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
    target = list[0] ?? null;
  } catch { /* not up yet */ }
  if (!target) await sleep(500);
}
if (!target) { console.error("main-process inspector never appeared"); child.kill(); process.exit(1); }

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.addEventListener("open", res, { once: true });
  ws.addEventListener("error", rej, { once: true });
});

let id = 0;
function send(method, params = {}) {
  const mine = ++id;
  return new Promise((resolve, reject) => {
    const onMsg = (e) => {
      const m = JSON.parse(e.data);
      if (m.id !== mine) return;
      ws.removeEventListener("message", onMsg);
      if (m.error) return reject(new Error(JSON.stringify(m.error)));
      resolve(m.result);
    };
    ws.addEventListener("message", onMsg);
    ws.send(JSON.stringify({ id: mine, method, params }));
  });
}

await send("Runtime.enable");
// Give the app time to run whenReady() and call setApplicationMenu.
await sleep(4000);

const expr = `
  (() => {
    const { Menu } = require('electron');
    const m = Menu.getApplicationMenu();
    if (!m) return 'NO APPLICATION MENU';
    const flat = [];
    for (const top of m.items) {
      for (const sub of (top.submenu ? top.submenu.items : [])) {
        if (sub.role) flat.push(top.label + '/' + sub.role);
      }
    }
    const need = ['paste', 'copy', 'cut', 'selectAll'];
    const found = need.filter(r => flat.some(f => f.toLowerCase().endsWith('/' + r.toLowerCase())));
    return JSON.stringify({
      topLevel: m.items.map(i => i.label).filter(Boolean),
      clipboardRoles: found,
      missing: need.filter(r => !found.includes(r)),
      pasteAccelerator: (() => {
        for (const top of m.items) {
          for (const sub of (top.submenu ? top.submenu.items : [])) {
            if (sub.role === 'paste') return sub.accelerator ?? '(role default)';
          }
        }
        return null;
      })(),
    });
  })()
`;

const r = await send("Runtime.evaluate", { expression: expr, includeCommandLineAPI: true, returnByValue: true });
ws.close();
child.kill("SIGTERM");
execSync(`rm -rf ${PROFILE}`);

if (r.exceptionDetails) {
  console.error("eval failed:", r.exceptionDetails.exception?.description);
  process.exit(1);
}
const out = r.result?.value;
console.log(out);
try {
  const parsed = JSON.parse(out);
  process.exit(parsed.missing.length === 0 ? 0 : 1);
} catch {
  process.exit(1);
}
