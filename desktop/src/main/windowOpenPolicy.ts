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
 */
/**
 * What happens when a page in a cluster window tries to open another window.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 *
 * Its own module, with **no Electron import**, for one reason: this is the security-relevant
 * decision in `windows.ts` and it should be testable without launching a browser process.
 * `scripts/window-open-check.mjs` runs the table against it in plain node.
 *
 * The rule, and why it is not simply "deny everything":
 *
 * **Another origin goes to the operator's real browser.** An Electron popup has no address bar,
 * so a page that could open one pointing anywhere could draw a convincing login form with
 * nothing to contradict it. The system browser has the address bar that makes the difference.
 *
 * **This deployment's own origin opens as a window here.** The phishing argument does not apply:
 * the cluster window is already pinned to that origin, so such a popup can only show pages this
 * deployment already serves. Denying them cost the product its own documentation — "Open in a
 * new window" handed the operator to Safari, which on an air-gapped host means a second
 * certificate prompt, a second sign-in, and leaving the app to read its own manual.
 *
 * **Anything that is not http(s) is blocked outright** and not handed to the operating system.
 * `file:`, `data:` and custom schemes are how a page gets something *else* to open a payload;
 * `shell.openExternal` on an arbitrary scheme is a way to launch a local handler.
 */

/** `open-window` here, `open-external` in the operator's browser, or refused entirely. */
export type WindowOpenAction = "open-window" | "open-external" | "block";

/**
 * Whether *url* is on *origin*.
 *
 * Parsed origins, never a string prefix: `https://host:5056.evil.com/` *starts with*
 * `https://host:5056`, so a prefix check would hand an attacker's page the same-origin
 * treatment. That is the whole reason this is a named function with a test rather than an
 * inline `startsWith`.
 */
export function sameOrigin(url: string, origin: string): boolean {
  try {
    return new URL(url).origin === origin;
  } catch {
    return false;
  }
}

export function windowOpenDecision(url: string, allowedOrigin: string | null): WindowOpenAction {
  if (allowedOrigin !== null && sameOrigin(url, allowedOrigin)) return "open-window";
  let scheme: string;
  try {
    scheme = new URL(url).protocol;
  } catch {
    return "block";
  }
  return scheme === "http:" || scheme === "https:" ? "open-external" : "block";
}
