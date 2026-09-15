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
 * Window construction, and the trust boundary that goes with it.
 *
 * There are exactly two kinds of window, and the difference is who wrote the code inside:
 *
 * - the **launcher** runs our own local HTML and gets a preload with a narrow IPC surface;
 * - a **cluster window** loads the deployment's own origin and gets **no preload at all**.
 *
 * That asymmetry is the whole security design. A cluster window renders code served over a tunnel
 * from a machine in a customer data centre; handing it a `contextBridge` API would put "open an
 * SSH tunnel" and "read the deployment list" one XSS away from a page we do not build. It needs
 * nothing from us — it is the same SPA a browser would load — so it is given nothing.
 *
 * **We load the deployment's origin rather than bundling the SPA.** Every API call in the frontend
 * is a root-relative `/api/...` with `credentials: "same-origin"`, the session cookie is
 * `SameSite=Lax`, the CSP is `default-src 'self'`, and `test_no_cors_headers_are_ever_emitted`
 * exists specifically to stop anyone relaxing that. Serving the bundle from `app://` or a local
 * port would require CORS with credentials and `SameSite=None`, dismantling the same-origin
 * protection the product gets for free. Pointing a window at the real origin keeps all of it.
 */

import { BrowserWindow, app, dialog, shell, type Certificate } from "electron";
import { join } from "node:path";

import { loadDeployments, originFor, saveDeployments, type Deployment } from "./deployments";
import { windowOpenDecision } from "./windowOpenPolicy";

/** Cluster windows by deployment id, so Connect twice focuses rather than duplicates. */
const clusterWindows = new Map<string, BrowserWindow>();
let launcher: BrowserWindow | null = null;

const APP_ICON = join(__dirname, "..", "..", "resources", "icon.png");

/**
 * Harden a window's `webContents` against everything a remote page might try.
 *
 * Applied to both window kinds. The launcher is our own HTML and needs none of it today, but a
 * guard that is only on the dangerous window is one that silently stops applying the moment
 * somebody adds a link to the safe one.
 */
function harden(win: BrowserWindow, allowedOrigin: string | null): void {
  const contents = win.webContents;

  // A target=_blank or window.open to **another origin** goes to the operator's real browser,
  // never a chromeless Electron window: an Electron popup has no address bar, so a page could
  // draw a convincing login form with nothing to contradict it.
  //
  // **Same-origin is the exception, and the reason is that the phishing argument does not
  // apply to it.** The window is already pinned to this deployment's origin by `will-navigate`;
  // a popup that cannot leave that origin can only show pages this deployment serves, which is
  // exactly what the window is showing anyway. Denying those bought no safety and cost the
  // product its own documentation: "Open in a new window" handed the operator to Safari, which
  // on an air-gapped host means a second certificate prompt, a second sign-in, and leaving the
  // application to read its own manual.
  //
  // The child is hardened identically (`did-create-window` below) rather than inheriting
  // anything, because a window created this way starts with Electron's defaults, not ours.
  contents.setWindowOpenHandler(({ url }) => {
    const decision = windowOpenDecision(url, allowedOrigin);
    if (decision === "open-window") {
      return {
        action: "allow",
        overrideBrowserWindowOptions: {
          width: 1280,
          height: 900,
          icon: APP_ICON,
          backgroundColor: "#0d0d0d",
          // macOS groups windows sharing an identifier into native tabs when the operator has
          // "Prefer tabs when opening documents" set — so on a Mac this *is* the new tab, with
          // no tab strip for us to build and maintain. Elsewhere it is ignored and the window is
          // a window, which is the honest outcome: Electron has no cross-platform tab UI.
          tabbingIdentifier: "nvis-greenlight",
          webPreferences: {
            // The same posture as the cluster window: no preload, no bridge, sandboxed. This
            // renders code served from a customer data centre.
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: true,
            webSecurity: true,
          },
        },
      };
    }
    // Only http(s) reaches the operating system: `file:`, `data:` and custom schemes are how a
    // page gets something *else* to open a payload, and `openExternal` on an arbitrary scheme
    // launches whatever local handler claims it.
    if (decision === "open-external") void shell.openExternal(url);
    return { action: "deny" };
  });

  // Harden the child, and keep the deployment named in its title.
  //
  // A second window showing a *different* cluster's documentation than the one you think you are
  // reading is the confusion this whole shell exists to prevent, so the child carries the label
  // too. Unlike the cluster window, its page title is allowed through — which document you are
  // on is the useful part — with the deployment appended rather than replaced.
  contents.on("did-create-window", (child) => {
    harden(child, allowedOrigin);
    const label = win.getTitle();
    const stamp = (title: string) => {
      const suffix = label ? ` — ${label}` : "";
      if (!title.endsWith(suffix)) child.setTitle(`${title}${suffix}`);
    };
    stamp(child.webContents.getTitle() || "Documentation");
    child.webContents.on("page-title-updated", (event, title) => {
      event.preventDefault();
      stamp(title);
    });
  });

  // Keep a cluster window on its own origin. Grafana links, docs and NVIDIA URLs leave for the
  // system browser instead; the SPA's own routing is client-side and never triggers this.
  contents.on("will-navigate", (event, url) => {
    if (allowedOrigin === null) return;
    try {
      if (new URL(url).origin !== allowedOrigin) {
        event.preventDefault();
        void shell.openExternal(url);
      }
    } catch {
      event.preventDefault();
    }
  });

  // `<webview>` can be given its own preload and node integration, so a page that manages to
  // inject one would escape the sandbox this window is in. Nothing here uses them.
  contents.on("will-attach-webview", (event) => event.preventDefault());

  // Deny every permission by default rather than enumerating the dangerous ones -- the list of
  // permissions grows with Chromium, and a default-allow would inherit each new one silently.
  // The dashboard asks for none of them; its terminal is a WebSocket, not getUserMedia.
  contents.session.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
}

/**
 * Trust-on-first-use for the self-signed certificate, with the operator making the decision.
 *
 * **Why this exists rather than `ignoreCertificateErrors`.** Greenlight's startup gate generates a
 * self-signed certificate when none is configured, so in practice every deployment presents one no
 * CA vouches for. The options are: refuse (nothing connects), blanket-accept (a tunnel to the
 * wrong host is indistinguishable from the right one), or pin. Pinning is the only one that both
 * works and means something.
 *
 * The fingerprint is shown to the operator once and then stored on the deployment record. On every
 * later launch a mismatch is refused outright and *not* re-offered, because at that point the
 * plausible causes are a redeployed certificate and a machine-in-the-middle, and the operator
 * cannot tell those apart from a dialog. Clearing the pin is a deliberate edit in the launcher.
 */
async function decideCertificate(
  dep: Deployment,
  url: string,
  certificate: Certificate,
  error: string,
): Promise<boolean> {
  const presented = certificate.fingerprint;

  if (dep.certFingerprint) {
    if (dep.certFingerprint === presented) return true;
    await dialog.showMessageBox({
      type: "error",
      title: "Certificate changed",
      message: `${dep.label} presented a different certificate than the one you trusted.`,
      detail:
        `Trusted: ${dep.certFingerprint}\nPresented: ${presented}\n\n` +
        "This happens when the deployment is rebuilt and regenerates its certificate — and it " +
        "also happens when something is intercepting the connection. Confirm out of band which " +
        "one it is, then clear the pinned fingerprint for this deployment to trust the new one.",
      buttons: ["Cancel"],
    });
    return false;
  }

  const { response } = await dialog.showMessageBox({
    type: "warning",
    title: "Trust this deployment's certificate?",
    message: `${dep.label} presented a certificate that no authority vouches for.`,
    detail:
      `${url}\n${error}\n\nFingerprint:\n${presented}\n\n` +
      "Greenlight generates a self-signed certificate by default, so this is expected on a new " +
      "deployment. Compare the fingerprint against the deployment host before trusting it:\n" +
      "  openssl x509 -in var/secrets/tls/server.crt -noout -fingerprint -sha256\n\n" +
      "Trusting it pins this exact certificate; you will be warned if it ever changes.",
    buttons: ["Cancel", "Trust and remember"],
    defaultId: 0,
    cancelId: 0,
  });
  if (response !== 1) return false;

  const { file } = loadDeployments();
  const record = file.deployments.find((d) => d.id === dep.id);
  if (record) {
    record.certFingerprint = presented;
    saveDeployments(file);
  }
  return true;
}

/**
 * Register the certificate handler.
 *
 * App-level rather than per-window because Electron emits `certificate-error` on `app`, and
 * because the decision is per *deployment* — which the URL identifies via its port.
 */
export function installCertificateHandler(): void {
  app.on("certificate-error", (event, _contents, url, error, certificate, callback) => {
    const { file } = loadDeployments();
    const dep = file.deployments.find((d) => originFor(d) === safeOrigin(url));
    if (!dep) {
      // A certificate error on an origin we do not manage is not ours to approve.
      callback(false);
      return;
    }
    event.preventDefault();
    void decideCertificate(dep, url, certificate, error).then(callback);
  });
}

function safeOrigin(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}

/** The deployment picker: our own HTML, so it gets a preload and an IPC surface. */
export function createLauncherWindow(): BrowserWindow {
  if (launcher && !launcher.isDestroyed()) {
    launcher.focus();
    return launcher;
  }

  launcher = new BrowserWindow({
    width: 720,
    height: 620,
    title: "NVIS Greenlight",
    icon: APP_ICON,
    backgroundColor: "#0d0d0d",
    webPreferences: {
      preload: join(__dirname, "..", "preload", "launcher.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  harden(launcher, null);
  void launcher.loadFile(join(__dirname, "..", "launcher", "index.html"));
  launcher.on("closed", () => {
    launcher = null;
  });
  return launcher;
}

/**
 * A window on a live cluster.
 *
 * The title carries the customer and cluster label, which is the fix for the one thing an
 * installed PWA could never do: two tunnels differing only by port install as two identical icons,
 * so an operator cannot tell which cluster a window is driving. Here it is in the title bar.
 */
export function createClusterWindow(dep: Deployment): BrowserWindow {
  const existing = clusterWindows.get(dep.id);
  if (existing && !existing.isDestroyed()) {
    existing.focus();
    return existing;
  }

  const origin = originFor(dep);
  const win = new BrowserWindow({
    width: 1600,
    height: 1000,
    title: `${dep.label} — ${dep.customer}`,
    icon: APP_ICON,
    backgroundColor: "#0d0d0d",
    // Shared with the same-origin child windows, so on macOS a documentation window opens as a
    // native tab beside the cluster it belongs to instead of a second floating window. Ignored
    // on Windows and Linux, where Electron has no tab concept.
    tabbingIdentifier: "nvis-greenlight",
    webPreferences: {
      // No preload. See the module docstring: this window renders code served from a customer
      // data centre and must have no bridge into this process.
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      // The SPA sets its own title; keeping ours is what makes the cluster identifiable.
      // (Enforced below rather than here -- Electron has no "ignore document title" option.)
    },
  });

  harden(win, origin);

  // The SPA calls document.title, which would replace the cluster name with "NVIS Greenlight" and
  // undo the one thing this window has that a browser tab does not.
  win.on("page-title-updated", (event) => event.preventDefault());

  void win.loadURL(origin);
  clusterWindows.set(dep.id, win);
  win.on("closed", () => clusterWindows.delete(dep.id));
  return win;
}

export function closeClusterWindow(deploymentId: string): void {
  const win = clusterWindows.get(deploymentId);
  if (win && !win.isDestroyed()) win.close();
}

export function reloadClusterWindow(deploymentId: string): void {
  const win = clusterWindows.get(deploymentId);
  if (win && !win.isDestroyed()) win.webContents.reloadIgnoringCache();
}

export function hasClusterWindow(deploymentId: string): boolean {
  const win = clusterWindows.get(deploymentId);
  return win !== undefined && !win.isDestroyed();
}
