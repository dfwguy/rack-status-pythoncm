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
 * IPC handlers for the launcher.
 *
 * One named channel per operation, matching `preload/launcher.ts` exactly. No generic dispatcher:
 * a `invoke(channel, ...args)` passthrough would expose every handler registered anywhere in the
 * process, which is the whole point of enumerating them.
 */

import { BrowserWindow, app, ipcMain } from "electron";

import {
  deploymentsConfigPath,
  loadDeployments,
  originFor,
  saveDeployments,
  validateDeployment,
  type Deployment,
} from "./deployments";
import { closeAllTunnels, closeTunnel, onTunnelStatus, openTunnel } from "./tunnel";
import { closeClusterWindow, createClusterWindow, hasClusterWindow } from "./windows";
import { checkForUpdates } from "./updater";

function findDeployment(id: unknown): Deployment | undefined {
  if (typeof id !== "string") return undefined;
  return loadDeployments().file.deployments.find((d) => d.id === id);
}

export function installIpcHandlers(): void {
  ipcMain.handle("deployments:list", () => {
    const { file, warning } = loadDeployments();
    return {
      deployments: file.deployments.map((dep) => ({
        ...dep,
        // Derived, so the renderer never has to reimplement scheme/port assembly and drift from it.
        origin: originFor(dep),
        connected: hasClusterWindow(dep.id),
      })),
      warning,
      configPath: deploymentsConfigPath(),
    };
  });

  ipcMain.handle("deployments:save", (_event, incoming: Deployment) => {
    const problems = validateDeployment(incoming);
    if (problems.length > 0) return { ok: false, problems };

    const { file } = loadDeployments();
    const index = file.deployments.findIndex((d) => d.id === incoming.id);
    if (index >= 0) file.deployments[index] = incoming;
    else file.deployments.push(incoming);
    saveDeployments(file);
    return { ok: true, problems: [] };
  });

  ipcMain.handle("deployments:remove", (_event, id: unknown) => {
    if (typeof id !== "string") return;
    closeTunnel(id);
    closeClusterWindow(id);
    const { file } = loadDeployments();
    file.deployments = file.deployments.filter((d) => d.id !== id);
    saveDeployments(file);
  });

  ipcMain.handle("deployments:clear-pin", (_event, id: unknown) => {
    if (typeof id !== "string") return;
    const { file } = loadDeployments();
    const record = file.deployments.find((d) => d.id === id);
    if (!record) return;
    // `delete` rather than setting "" — an empty string is a value, and `decideCertificate` reads
    // the field's presence to mean "already trusted".
    delete record.certFingerprint;
    saveDeployments(file);
  });

  ipcMain.handle("cluster:open", async (_event, id: unknown) => {
    const dep = findDeployment(id);
    if (!dep) {
      return { deploymentId: String(id), state: "failed", origin: "", detail: "No such deployment." };
    }
    const status = await openTunnel(dep);
    if (status.state === "up") createClusterWindow(dep);
    return status;
  });

  ipcMain.handle("cluster:close", (_event, id: unknown) => {
    if (typeof id !== "string") return;
    closeClusterWindow(id);
    closeTunnel(id);
  });

  ipcMain.handle("app:info", () => ({
    version: app.getVersion(),
    electron: process.versions.electron,
    node: process.versions.node,
    chrome: process.versions.chrome,
  }));

  ipcMain.handle("updater:check", () => checkForUpdates());

  // Push tunnel transitions to every open launcher. Broadcast rather than held against one
  // window because the launcher can be closed and reopened while a tunnel stays up.
  onTunnelStatus((status) => {
    for (const win of BrowserWindow.getAllWindows()) {
      if (win.isDestroyed()) continue;
      win.webContents.send("tunnel:status", status);
    }
  });
}

export function teardown(): void {
  closeAllTunnels();
}
