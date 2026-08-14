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

// Keep storage isolated between tests so state never leaks across cases.
afterEach(() => {
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {
    /* ignore */
  }
});
