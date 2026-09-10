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
 *
 * Launcher renderer. A plain script, not a module: it is loaded by a `<script src>` tag under a
 * CSP with no `unsafe-inline`, and it needs no imports because everything it can do arrives on
 * `window.greenlight` from the preload.
 *
 * Rendering is by `document.createElement` and `textContent` throughout — never `innerHTML`. These
 * strings include operator-entered labels and ssh stderr, and this is the one window in the app
 * that holds an IPC bridge, so it is the last place to interpolate untrusted text into markup.
 */

interface LauncherDeployment {
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
  origin: string;
  connected: boolean;
}

interface LauncherTunnelStatus {
  deploymentId: string;
  state: "idle" | "starting" | "up" | "failed";
  origin: string;
  detail: string;
  remedy?: string;
}

interface GreenlightBridge {
  listDeployments(): Promise<{
    deployments: LauncherDeployment[];
    warning?: string;
    configPath: string;
  }>;
  saveDeployment(dep: unknown): Promise<{ ok: boolean; problems: string[] }>;
  removeDeployment(id: string): Promise<void>;
  clearCertificatePin(id: string): Promise<void>;
  connect(id: string): Promise<LauncherTunnelStatus>;
  disconnect(id: string): Promise<void>;
  appInfo(): Promise<{ version: string; electron: string; node: string; chrome: string }>;
  checkForUpdates(): Promise<{ status: string; detail: string }>;
  onTunnelStatus(listener: (status: LauncherTunnelStatus) => void): () => void;
}

interface Window {
  greenlight: GreenlightBridge;
}

const bridge = (window as unknown as Window).greenlight;

const listEl = document.getElementById("list") as HTMLUListElement;
const emptyEl = document.getElementById("empty") as HTMLParagraphElement;
const warningEl = document.getElementById("warning") as HTMLParagraphElement;
const editor = document.getElementById("editor") as HTMLDialogElement;
const form = document.getElementById("form") as HTMLFormElement;
const problemsEl = document.getElementById("form-problems") as HTMLParagraphElement;
const aboutEl = document.getElementById("about") as HTMLSpanElement;

/** Latest status per deployment, so a re-render does not lose what the operator was reading. */
const statuses = new Map<string, LauncherTunnelStatus>();
let deployments: LauncherDeployment[] = [];
let editingId: string | null = null;

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderCard(dep: LauncherDeployment): HTMLLIElement {
  const li = el("li");

  const row = el("div", "row");
  row.append(
    el("span", "label", dep.label),
    el("span", "customer", dep.customer),
    el("span", "origin", dep.origin),
  );

  const actions = el("div", "actions");
  const status = statuses.get(dep.id);
  const busy = status?.state === "starting";

  const connect = el("button", undefined, dep.connected ? "Focus" : "Connect");
  connect.disabled = busy;
  connect.addEventListener("click", () => void doConnect(dep.id));

  const disconnect = el("button", "secondary", "Disconnect");
  disconnect.disabled = busy || !(dep.connected || status?.state === "up");
  disconnect.addEventListener("click", () => void doDisconnect(dep.id));

  const edit = el("button", "secondary", "Edit");
  edit.addEventListener("click", () => openEditor(dep));

  const remove = el("button", "danger", "Remove");
  remove.addEventListener("click", () => void doRemove(dep));

  actions.append(connect, disconnect, edit, remove);
  row.append(actions);
  li.append(row);

  if (dep.notes) li.append(el("div", "status", dep.notes));

  if (status && status.state !== "idle") {
    li.append(el("div", `status ${status.state}`, status.detail));
    if (status.remedy) li.append(el("div", "remedy", status.remedy));
  }

  if (dep.certFingerprint) {
    const pin = el("div", "pin", `pinned cert ${dep.certFingerprint}  `);
    const clear = el("button", "link", "Clear pin");
    clear.addEventListener("click", () => void doClearPin(dep.id));
    pin.append(clear);
    li.append(pin);
  }

  return li;
}

function render(): void {
  listEl.replaceChildren(...deployments.map(renderCard));
  emptyEl.hidden = deployments.length > 0;
}

async function reload(): Promise<void> {
  const { deployments: list, warning, configPath } = await bridge.listDeployments();
  deployments = list;
  if (warning) {
    warningEl.textContent = warning;
    warningEl.hidden = false;
  } else {
    warningEl.hidden = true;
  }
  warningEl.title = configPath;
  render();
}

async function doConnect(id: string): Promise<void> {
  // Optimistic, because opening a tunnel through a jumphost can take seconds and a button that
  // does not visibly change invites a second click -- which `openTunnel` would have to dedupe.
  statuses.set(id, { deploymentId: id, state: "starting", origin: "", detail: "Connecting…" });
  render();
  const status = await bridge.connect(id);
  statuses.set(id, status);
  await reload();
}

async function doDisconnect(id: string): Promise<void> {
  await bridge.disconnect(id);
  statuses.delete(id);
  await reload();
}

async function doRemove(dep: LauncherDeployment): Promise<void> {
  if (!confirm(`Remove ${dep.label}? This closes its window and tunnel.`)) return;
  await bridge.removeDeployment(dep.id);
  statuses.delete(dep.id);
  await reload();
}

async function doClearPin(id: string): Promise<void> {
  await bridge.clearCertificatePin(id);
  await reload();
}

function setKindVisibility(kind: string): void {
  (document.getElementById("tunnel-fields") as HTMLFieldSetElement).hidden = kind !== "ssh-tunnel";
  (document.getElementById("direct-fields") as HTMLFieldSetElement).hidden = kind !== "direct";
}

function openEditor(dep?: LauncherDeployment): void {
  editingId = dep?.id ?? null;
  (document.getElementById("editor-title") as HTMLElement).textContent = dep
    ? "Edit deployment"
    : "Add deployment";
  form.reset();
  problemsEl.hidden = true;

  const set = (name: string, value: string) => {
    const field = form.elements.namedItem(name) as HTMLInputElement | null;
    if (field) field.value = value;
  };
  set("label", dep?.label ?? "");
  set("customer", dep?.customer ?? "");
  set("kind", dep?.kind ?? "ssh-tunnel");
  set("sshAlias", dep?.sshAlias ?? "");
  set("remoteHost", dep?.remoteHost ?? "localhost");
  set("remotePort", dep?.remotePort ? String(dep.remotePort) : "");
  set("localPort", dep?.localPort ? String(dep.localPort) : "");
  set("scheme", dep?.scheme ?? "https");
  set("url", dep?.url ?? "");
  set("notes", dep?.notes ?? "");

  setKindVisibility(dep?.kind ?? "ssh-tunnel");
  editor.showModal();
}

function collect(): Record<string, unknown> {
  const data = new FormData(form);
  const text = (name: string) => String(data.get(name) ?? "").trim();
  const port = (name: string) => {
    const raw = text(name);
    return raw === "" ? undefined : Number(raw);
  };
  const kind = text("kind");

  const base: Record<string, unknown> = {
    // `crypto.randomUUID` needs a secure context; a `file://` page is one, so this is available
    // here. The id is only ever generated for a *new* record -- an edit keeps its id, because the
    // id is the window identity and the certificate pin's key.
    id: editingId ?? crypto.randomUUID(),
    label: text("label"),
    customer: text("customer"),
    kind,
    notes: text("notes") || undefined,
  };
  if (kind === "direct") return { ...base, url: text("url") };
  return {
    ...base,
    sshAlias: text("sshAlias"),
    remoteHost: text("remoteHost"),
    remotePort: port("remotePort"),
    localPort: port("localPort"),
    scheme: text("scheme"),
  };
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void (async () => {
    const { ok, problems } = await bridge.saveDeployment(collect());
    if (!ok) {
      // Rendered as a list, not a joined string: these come from `validateDeployment` and several
      // at once is the normal case for a half-filled form.
      problemsEl.replaceChildren(...problems.map((p) => el("div", undefined, `• ${p}`)));
      problemsEl.hidden = false;
      return;
    }
    editor.close();
    await reload();
  })();
});

(document.getElementById("cancel") as HTMLButtonElement).addEventListener("click", () =>
  editor.close(),
);
(document.getElementById("add") as HTMLButtonElement).addEventListener("click", () => openEditor());
(form.elements.namedItem("kind") as HTMLSelectElement).addEventListener("change", (event) =>
  setKindVisibility((event.target as HTMLSelectElement).value),
);

(document.getElementById("check-updates") as HTMLButtonElement).addEventListener("click", () => {
  void (async () => {
    const { detail } = await bridge.checkForUpdates();
    aboutEl.textContent = detail;
  })();
});

// Pushed transitions, so a tunnel that drops later updates the card without the operator acting.
bridge.onTunnelStatus((status) => {
  statuses.set(status.deploymentId, status);
  void reload();
});

void (async () => {
  const info = await bridge.appInfo();
  aboutEl.textContent = `Greenlight ${info.version} · Electron ${info.electron} · Node ${info.node}`;
  await reload();
})();
