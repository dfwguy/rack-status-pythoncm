/**
 * Verifies that the RackCommandsStoreProvider intercepts ``runSyncJson`` when the rack is
 * locked by another operator, opens the LockConflictDialog, and skips/runs the underlying
 * fetch based on the user's choice.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { useEffect } from "react";

import { useRackCommandsStore } from "./RackCommandsStoreContext";
import { RackCommandsStoreProvider } from "./RackCommandsStoreProvider";

vi.mock("./RackLocksContext", () => ({
  useRackLocksContext: () => ({
    locks: {
      "p5-r07": {
        operator: "bob",
        kind: "active_job",
        detail: "running gpu-reset",
        since: "2026-05-18T11:59:00Z",
      },
    },
    me: "alice",
    now: "",
    loading: false,
    error: null,
    isLockedByOther: (rack: string) =>
      rack === "p5-r07"
        ? {
            operator: "bob",
            kind: "active_job" as const,
            detail: "running gpu-reset",
            since: "2026-05-18T11:59:00Z",
          }
        : null,
    getLock: () => null,
    refresh: async () => {},
  }),
}));

function Trigger({ rack, onDone }: { rack: string; onDone?: () => void }) {
  const { runSyncJson } = useRackCommandsStore();
  useEffect(() => {
    void runSyncJson(rack, "Power on", "/api/power-on", { rack }).then(() => onDone?.());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return null;
}

describe("RackCommandsStoreProvider interceptor", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ ok: true, output: "done" }),
      json: async () => ({ ok: true }),
    } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    localStorage.clear();
    localStorage.setItem("nvis-rack-operator", "alice");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("opens the lock conflict dialog and skips the fetch when the user cancels", async () => {
    render(
      <RackCommandsStoreProvider>
        <Trigger rack="p5-r07" />
      </RackCommandsStoreProvider>,
    );

    await waitFor(() => expect(screen.getByText(/rack busy/i)).toBeInTheDocument());
    expect(screen.getByText(/p5-r07/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("runs the underlying fetch when the user picks 'Run anyway'", async () => {
    render(
      <RackCommandsStoreProvider>
        <Trigger rack="p5-r07" />
      </RackCommandsStoreProvider>,
    );

    await waitFor(() => expect(screen.getByText(/rack busy/i)).toBeInTheDocument());
    expect(fetchMock).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /run anyway/i }));
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/power-on");
    const headers = (init as RequestInit).headers as Headers;
    expect(headers.get("X-Rack-Operator")).toBe("alice");
  });

  it("does not prompt when the rack is not locked", async () => {
    render(
      <RackCommandsStoreProvider>
        <Trigger rack="p5-r99" />
      </RackCommandsStoreProvider>,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByText(/rack busy/i)).not.toBeInTheDocument();
  });
});
