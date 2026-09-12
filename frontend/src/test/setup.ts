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
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";

/**
 * The test runtime exposes a non-functional `localStorage`/`sessionStorage`
 * (a bare object with no Storage methods), so calls like `localStorage.clear()`
 * throw `TypeError: ... is not a function`. Install a spec-complete in-memory
 * Web Storage implementation on both `globalThis` and `window` so tests can use
 * the Storage API normally.
 */
function createMemoryStorage(): Storage {
  const store = new Map<string, string>();
  const storage = {
    get length(): number {
      return store.size;
    },
    clear(): void {
      store.clear();
    },
    getItem(key: string): string | null {
      return store.has(key) ? (store.get(key) as string) : null;
    },
    key(index: number): string | null {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(key: string): void {
      store.delete(key);
    },
    setItem(key: string, value: string): void {
      store.set(key, String(value));
    },
  };
  return storage as unknown as Storage;
}

function installStorage(name: "localStorage" | "sessionStorage"): void {
  const existing = (globalThis as Record<string, unknown>)[name] as Partial<Storage> | undefined;
  // Skip if the runtime already provides a working Storage implementation.
  if (existing && typeof existing.clear === "function" && typeof existing.getItem === "function") {
    return;
  }

  const storage = createMemoryStorage();
  const descriptor: PropertyDescriptor = { value: storage, configurable: true, writable: true };
  try {
    Object.defineProperty(globalThis, name, descriptor);
  } catch {
    /* non-configurable global; fall back to window below */
  }
  if (typeof window !== "undefined") {
    try {
      Object.defineProperty(window, name, descriptor);
    } catch {
      /* ignore */
    }
  }
}

installStorage("localStorage");
installStorage("sessionStorage");

/**
 * jsdom implements `<dialog>` as an element but not as a dialog: `showModal` and `close` are
 * simply absent. KUI's `Modal` calls `showModal()` in an effect, so *rendering* a component that
 * contains one throws `dialog.showModal is not a function` — and because the throw happens in a
 * passive effect, the failure surfaces as a stack inside react-dom with no mention of the modal,
 * which is a long way to travel for a missing method.
 *
 * The minimum that makes the element behave: toggle `open`, which is what Testing Library's
 * queries and KUI's own styling key off. Guarded, so it becomes a no-op the day jsdom ships the
 * real thing rather than quietly shadowing it.
 */
function installDialog(): void {
  const proto = globalThis.HTMLDialogElement?.prototype;
  if (!proto) return;
  if (typeof proto.showModal !== "function") {
    proto.showModal = function showModal(this: HTMLDialogElement) {
      this.open = true;
    };
  }
  if (typeof proto.show !== "function") {
    proto.show = function show(this: HTMLDialogElement) {
      this.open = true;
    };
  }
  if (typeof proto.close !== "function") {
    proto.close = function close(this: HTMLDialogElement, returnValue?: string) {
      this.open = false;
      if (returnValue !== undefined) this.returnValue = returnValue;
      this.dispatchEvent(new Event("close"));
    };
  }
}

installDialog();

/**
 * jsdom has no CSS feature detection, so `CSS.supports` is missing and KUI's auto-sizing
 * `TextArea` throws on mount when it probes for `field-sizing`.
 *
 * It answers **false**, which is the honest reply: jsdom supports none of what a caller is
 * probing for, and false selects the fallback path a component keeps for older browsers. `true`
 * would claim support for a feature with no layout engine behind it, so a component would take
 * its modern path and then behave nothing like it does in a browser.
 */
if (typeof globalThis.CSS === "undefined") {
  (globalThis as Record<string, unknown>).CSS = { supports: () => false };
} else if (typeof globalThis.CSS.supports !== "function") {
  globalThis.CSS.supports = (() => false) as typeof globalThis.CSS.supports;
}

// Keep storage isolated between tests so state never leaks across cases.
afterEach(() => {
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {
    /* ignore */
  }
});
