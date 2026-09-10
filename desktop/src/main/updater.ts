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
 * Signed auto-update, over the GitLab generic Package Registry.
 *
 * `autoDownload` is off: an operator mid-validation should not have a 150 MB download start under
 * them, and the install needs a restart, which is theirs to schedule. So this checks, asks, then
 * downloads.
 *
 * **Two platforms cannot be updated in place and are refused explicitly rather than left to fail
 * obscurely.** `electron-updater` supports AppImage on Linux but *not* `.deb` or `.rpm` — those are
 * owned by the system package manager, and there is no mechanism for the app to replace itself.
 * Detected via `process.env.APPIMAGE`, which the AppImage runtime sets and nothing else does. A
 * `.deb` install therefore gets "update through apt" instead of a spinner that never resolves.
 */

import { app, dialog } from "electron";

export interface UpdateResult {
  status: "unsupported" | "dev" | "current" | "available" | "downloading" | "error";
  detail: string;
}

/** Lazy, because requiring `electron-updater` outside a packaged app logs a confusing warning. */
async function autoUpdater() {
  const { autoUpdater: updater } = await import("electron-updater");
  updater.autoDownload = false;
  updater.autoInstallOnAppQuit = true;
  return updater;
}

function unsupportedReason(): string | null {
  if (!app.isPackaged) {
    return "This is a development build; updates are only checked in a packaged app.";
  }
  if (process.platform === "linux" && !process.env.APPIMAGE) {
    return (
      "This looks like a .deb or .rpm install, which the system package manager owns. " +
      "Update it there, or use the AppImage build for in-app updates."
    );
  }
  return null;
}

export async function checkForUpdates(): Promise<UpdateResult> {
  const blocked = unsupportedReason();
  if (blocked) {
    return { status: app.isPackaged ? "unsupported" : "dev", detail: blocked };
  }

  try {
    const updater = await autoUpdater();
    const result = await updater.checkForUpdates();
    const version = result?.updateInfo?.version;
    if (!version || version === app.getVersion()) {
      return { status: "current", detail: `You are on the latest version (${app.getVersion()}).` };
    }

    const { response } = await dialog.showMessageBox({
      type: "info",
      title: "Update available",
      message: `NVIS Greenlight ${version} is available.`,
      detail:
        `You are on ${app.getVersion()}. The update is verified against a signature embedded in ` +
        "this build before anything is installed. It will be applied the next time you quit.",
      buttons: ["Later", "Download"],
      defaultId: 1,
      cancelId: 0,
    });
    if (response !== 1) {
      return { status: "available", detail: `${version} is available; download deferred.` };
    }

    await updater.downloadUpdate();
    return {
      status: "downloading",
      // Not "installing": `autoInstallOnAppQuit` means it lands on the next quit, and saying so is
      // the difference between an operator quitting deliberately and wondering why nothing changed.
      detail: `${version} downloaded. It will be installed when you quit Greenlight.`,
    };
  } catch (err) {
    return { status: "error", detail: `Update check failed: ${(err as Error).message}` };
  }
}

/**
 * Check once shortly after launch, quietly.
 *
 * Delayed rather than immediate so it cannot compete with the launcher's first paint, and
 * failures are swallowed: an unreachable update server is not something to interrupt an operator
 * with at startup. The launcher's explicit "Check for updates" reports errors properly.
 */
export function scheduleStartupUpdateCheck(): void {
  if (unsupportedReason() !== null) return;
  setTimeout(() => {
    void checkForUpdates().catch(() => undefined);
  }, 10_000);
}
