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
 * RackTable's scheduler actions (drain, undrain, cancel, power up) go through `apiFetch`.
 *
 * They were a raw `fetch`, which had two consequences: the action failed outright once the access
 * token expired, and the request carried no `X-Rack-Operator` / `X-Rack-Client-Id` headers, so
 * rack-lock tracking could not attribute a drain to the operator who issued it.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { RackCommandsStoreContext } from "../context/RackCommandsStoreContext";
import type { RackCommandsStoreValue } from "../context/RackCommandsStoreContext";
import { OPERATOR_NAME_KEY } from "../lib/getOperatorName";
import type { RackRow } from "../types/api";
import { RackTable } from "./RackTable";

const originalFetch = globalThis.fetch;

type Seen = { url: string; method?: string; operator: string | null; clientId: string | null; body?: string };
let seen: Seen[];

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const row: RackRow = { rack: "g01", nemo: "done", last_job: "-", sinfo: "idle", ct_down_unk: "" };

// No async command is launched here, so the store only has to answer "nothing running".
const idleStore: RackCommandsStoreValue = {
  getRackLast: () => null,
  getPendingForRack: () => [],
  replayLastOutput: vi.fn(),
  openText: vi.fn(),
  openStream: vi.fn(async () => {}),
  runSyncJson: vi.fn(async () => {}),
  runAsyncReset: vi.fn(async () => {}),
  runAsyncValidationFamily: vi.fn(async () => {}),
  launchAsyncValidationFamily: vi.fn(async () => ""),
  openTailLogNemo: vi.fn(),
};

function Store({ children }: { children: ReactNode }) {
  return <RackCommandsStoreContext.Provider value={idleStore}>{children}</RackCommandsStoreContext.Provider>;
}

beforeEach(() => {
  seen = [];
  localStorage.setItem(OPERATOR_NAME_KEY, "alice");
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  localStorage.clear();
  vi.restoreAllMocks();
});

async function drainG01() {
  render(<RackTable rows={[row]} comments={{}} />, { wrapper: Store });
  fireEvent.click(screen.getByRole("button", { name: "Node state actions for g01" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: /Drain nodes/ }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "Drain" }));
}

describe("RackTable scheduler actions", () => {
  it("attributes the action to the operator", async () => {
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const h = new Headers(init?.headers);
      seen.push({
        url: String(input),
        method: init?.method,
        operator: h.get("X-Rack-Operator"),
        clientId: h.get("X-Rack-Client-Id"),
        body: init?.body as string | undefined,
      });
      return json({ ok: true });
    }) as typeof fetch;

    await drainG01();

    await waitFor(() => expect(seen.some((s) => s.url === "/api/scheduler-drain")).toBe(true));
    const drain = seen.find((s) => s.url === "/api/scheduler-drain")!;
    expect(drain.method).toBe("POST");
    expect(drain.body).toBe('{"rack":"g01"}');
    expect(drain.operator).toBe("alice");
    expect(drain.clientId).toMatch(/^spa-/);
    expect(await screen.findByText(/Drain Nodes succeeded on g01/)).toBeInTheDocument();
  });

  it("survives an expired access token by renewing it and retrying", async () => {
    let drains = 0;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      seen.push({ url, operator: null, clientId: null });
      if (url.includes("/api/auth/refresh")) return json({ ok: true });
      if (url === "/api/scheduler-drain") {
        drains += 1;
        return drains === 1 ? json({ error: "authentication required" }, 401) : json({ ok: true });
      }
      return json({ ok: true });
    }) as typeof fetch;

    await drainG01();

    expect(await screen.findByText(/Drain Nodes succeeded on g01/)).toBeInTheDocument();
    expect(seen.map((s) => s.url).filter((u) => u.includes("drain") || u.includes("refresh"))).toEqual([
      "/api/scheduler-drain",
      "/api/auth/refresh",
      "/api/scheduler-drain",
    ]);
  });
});
