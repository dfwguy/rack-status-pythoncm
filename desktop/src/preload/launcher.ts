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
 * The launcher's only bridge into the main process.
 *
 * **This preload is loaded by exactly one window — the launcher, which renders our own local
 * HTML.** Cluster windows deliberately have no preload at all, because they render code served
 * from a customer data centre. Keeping that true is what makes the surface below safe to have:
 * nothing remote can reach it.
 *
 * Every method is an explicit named channel. There is no generic `invoke(channel, ...args)`
 * escape hatch, which would hand the renderer the whole `ipcMain` namespace and make this file
 * decorative.
 */

import { contextBridge, ipcRenderer } from "electron";

/** Mirrors `main/deployments.ts`; duplicated because a sandboxed preload cannot import from it. */
interface Deployment {
  id: string;
  label: string;
  customer: string;
  kind: "direct" | "ssh-tunnel";
  url?: string;
  scheme?: "https" | "http";
  certFingerprint?: string;
  sshAlias?: string;
  remoteHost?: string;
  remotePort?: number;
  localPort?: number;
  notes?: string;
}

interface TunnelStatus {
  deploymentId: string;
  state: "idle" | "starting" | "up" | "failed";
  origin: string;
  detail: string;
  remedy?: string;
}

contextBridge.exposeInMainWorld("greenlight", {
  listDeployments: (): Promise<{ deployments: Deployment[]; warning?: string; configPath: string }> =>
    ipcRenderer.invoke("deployments:list"),

  saveDeployment: (dep: Deployment): Promise<{ ok: boolean; problems: string[] }> =>
    ipcRenderer.invoke("deployments:save", dep),

  removeDeployment: (id: string): Promise<void> => ipcRenderer.invoke("deployments:remove", id),

  clearCertificatePin: (id: string): Promise<void> =>
    ipcRenderer.invoke("deployments:clear-pin", id),

  /** Open the tunnel if needed, then the cluster window. Resolves with the final status. */
  connect: (id: string): Promise<TunnelStatus> => ipcRenderer.invoke("cluster:open", id),

  disconnect: (id: string): Promise<void> => ipcRenderer.invoke("cluster:close", id),

  appInfo: (): Promise<{ version: string; electron: string; node: string; chrome: string }> =>
    ipcRenderer.invoke("app:info"),

  checkForUpdates: (): Promise<{ status: string; detail: string }> =>
    ipcRenderer.invoke("updater:check"),

  /**
   * Subscribe to tunnel status pushes.
   *
   * The listener is wrapped so the renderer never receives Electron's `IpcRendererEvent`: that
   * object carries `sender` and `ports`, which is a live handle back into the main process and
   * exactly the kind of thing `contextIsolation` exists to keep out of page scope.
   */
  onTunnelStatus: (listener: (status: TunnelStatus) => void): (() => void) => {
    const wrapped = (_event: unknown, status: TunnelStatus) => listener(status);
    ipcRenderer.on("tunnel:status", wrapped);
    return () => ipcRenderer.removeListener("tunnel:status", wrapped);
  },
});
