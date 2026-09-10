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
 * Entry point: app lifecycle, and nothing else.
 *
 * The composition root for the shell, deliberately thin — the same reason `backend/app.py` is a
 * ~2,000-line composition root rather than the 20,000-line one it grew into. Behaviour lives in
 * `deployments`, `tunnel`, `windows`, `ipc` and `updater`; this file wires them and owns shutdown.
 */

import { BrowserWindow, Menu, app, shell } from "electron";

import { installIpcHandlers, teardown } from "./ipc";
import { scheduleStartupUpdateCheck } from "./updater";
import { createLauncherWindow, installCertificateHandler } from "./windows";

/**
 * One instance only.
 *
 * Not tidiness: a second instance would open its own `ssh -L` on the same local ports, and the
 * second bind fails while the first keeps working — so half the windows in the second instance
 * would silently attach to the *first* instance's tunnels. Focusing the existing launcher is the
 * behaviour an operator double-clicking the dock icon expects anyway.
 */
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => createLauncherWindow());

  // Sandbox every renderer by default rather than per-window. A window created later without
  // `sandbox: true` in its own options would otherwise be unsandboxed and nothing would say so.
  app.enableSandbox();

  app.whenReady().then(() => {
    installCertificateHandler();
    installIpcHandlers();
    buildMenu();
    createLauncherWindow();
    scheduleStartupUpdateCheck();
  });

  app.on("activate", () => {
    // macOS: the dock icon reopens the launcher rather than starting a second app.
    if (BrowserWindow.getAllWindows().length === 0) createLauncherWindow();
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });

  // Every tunnel closed on the way out. Each one holds a session against the head node's
  // `sshd MaxSessions` budget, which the product's own fan-out competes for -- see
  // `closeAllTunnels`. `will-quit` rather than `quit`: at `quit` the process is already leaving.
  app.on("will-quit", teardown);
}

/**
 * A deliberately small menu.
 *
 * Electron's default menu is fine but carries entries that do not apply here. Two matter enough to
 * keep: **Reload**, because a deploy changes every Vite content hash and a window loaded before it
 * asks for chunks that no longer exist; and **Toggle DevTools**, because "the panel is blank" is
 * answered by the console far more often than by anything else.
 */
function buildMenu(): void {
  const isMac = process.platform === "darwin";
  Menu.setApplicationMenu(
    Menu.buildFromTemplate([
      ...(isMac ? [{ role: "appMenu" as const }] : []),
      {
        label: "File",
        submenu: [
          {
            label: "Deployments…",
            accelerator: "CmdOrCtrl+L",
            click: () => createLauncherWindow(),
          },
          { type: "separator" as const },
          isMac ? { role: "close" as const } : { role: "quit" as const },
        ],
      },
      // **Do not remove this while "simplifying the menu".** On every platform Electron wires the
      // clipboard accelerators (Cmd/Ctrl+C, V, X, A, Z) through the Edit menu's *roles* — there is
      // no separate keyboard layer. Replacing the default menu without an Edit menu therefore
      // disables copy and paste everywhere: the launcher's form, the dashboard's inputs, and the
      // xterm terminal. It looks like the app is deliberately blocking paste, and nothing logs an
      // error, which is why this comment is longer than the line it guards.
      { role: "editMenu" as const },
      {
        label: "View",
        submenu: [
          { role: "reload" as const },
          { role: "forceReload" as const },
          { role: "toggleDevTools" as const },
          { type: "separator" as const },
          { role: "resetZoom" as const },
          { role: "zoomIn" as const },
          { role: "zoomOut" as const },
          { role: "togglefullscreen" as const },
        ],
      },
      { role: "windowMenu" as const },
      {
        role: "help" as const,
        submenu: [
          {
            label: "NVIS Greenlight documentation",
            click: () => void shell.openExternal("https://gitlab-master.nvidia.com/"),
          },
        ],
      },
    ]),
  );
}
