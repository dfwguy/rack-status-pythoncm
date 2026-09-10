/* SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */

/**
 * `AuditTrailView` — TC-24, TC-25 and TC-26 of the audit-reader test plan.
 *
 * The three properties asserted here are the ones whose failure is *silent* — the view would look
 * entirely reasonable in a screenshot while telling an operator something untrue:
 *
 * 1. **A collapsed event must show its count.** One line can stand for forty-seven refusals.
 *    Rendering it as a single row under-reports a burst in exactly the review where the count is
 *    the finding.
 * 2. **"No events match" and "we no longer hold that period" must be different sentences.** An
 *    empty result for last month is ambiguous unless the view says how far back the trail reaches,
 *    and the wrong reading — "nothing happened" — is the dangerous one.
 * 3. **A truncated scan must say so.** A short page can mean "we stopped looking".
 *
 * `fireEvent` rather than `user-event`, matching the other 29 files in this suite.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as service from "../services/auditTrailService";
import type { AuditEvent, AuditPage } from "../types/auditTrail";
import AuditTrailView from "./AuditTrailView";

vi.mock("../services/auditTrailService", async (importOriginal) => {
  // `defaultSince` and ROWS_PER_PAGE_OPTIONS stay real: both are pure, and the component's
  // initial filter and page-size choices depend on them. Mocking them would test the mock's
  // idea of the defaults rather than the product's.
  const actual = await importOriginal<typeof service>();
  return { ...actual, readTrail: vi.fn(), downloadExport: vi.fn() };
});

const event = (over: Partial<AuditEvent> = {}): AuditEvent => ({
  event_id: "id-1",
  ts: "2026-09-10T12:00:00.000+00:00",
  principal: "user:rkoike",
  auth_method: "token",
  service: "rack.power",
  action: "rack.power:off",
  variant: "sync",
  resource: "rack.power:p17-r01",
  decision: "allow",
  policy_engine: "rbac-v1",
  result: "success",
  src_ip: "127.0.0.1",
  src_ip_trust: "peer",
  user_agent: "UA",
  request_id: "r".repeat(32),
  cluster: "nscale-tx",
  latency_ms: 12.3,
  ...over,
});

const page = (over: Partial<AuditPage> = {}): AuditPage => ({
  ok: true,
  events: [event()],
  cursor: "id-1",
  truncated: false,
  cursor_expired: false,
  scanned_events: 1,
  scanned_bytes: 490,
  skipped_lines: 0,
  oldest_held: "2026-06-12T08:00:00.000+00:00",
  files: 4,
  ...over,
});

beforeEach(() => {
  vi.mocked(service.readTrail).mockReset();
  vi.mocked(service.downloadExport).mockReset();
  vi.mocked(service.downloadExport).mockResolvedValue(undefined);
});

describe("rendering the trail", () => {
  it("lists the events it was given", async () => {
    vi.mocked(service.readTrail).mockResolvedValue(page());
    render(<AuditTrailView />);
    expect(await screen.findByText("rack.power:off")).toBeTruthy();
    expect(screen.getByText("user:rkoike")).toBeTruthy();
    expect(screen.getByText("rack.power:p17-r01")).toBeTruthy();
  });

  it("asks for a bounded window rather than the whole trail", async () => {
    // Retention is ~90 days and a filtered scan stops after 20,000 events, so an unbounded
    // default would make the ordinary case the expensive one -- and would show `truncated` to an
    // operator who never asked a hard question.
    vi.mocked(service.readTrail).mockResolvedValue(page());
    render(<AuditTrailView />);
    await waitFor(() => expect(service.readTrail).toHaveBeenCalled());
    const call = vi.mocked(service.readTrail).mock.calls[0];
    expect(call, "readTrail was never called").toBeTruthy();
    expect(call?.[0]?.since, "the view requested unbounded history").toBeTruthy();
  });

  it("does not poll", async () => {
    // The server's 50 ms budget assumes an on-demand read, and every read of the trail is itself
    // audited -- so a background timer would fill the record with this view looking at itself.
    vi.useFakeTimers();
    try {
      vi.mocked(service.readTrail).mockResolvedValue(page());
      render(<AuditTrailView />);
      await vi.advanceTimersByTimeAsync(120_000);
      expect(vi.mocked(service.readTrail).mock.calls.length).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports a failure instead of rendering an empty table", async () => {
    // An empty table and a failed request look identical, and "nothing happened" is the wrong
    // conclusion to let an operator reach.
    vi.mocked(service.readTrail).mockRejectedValue(new Error("not authorized"));
    render(<AuditTrailView />);
    expect(await screen.findByText(/not authorized/)).toBeTruthy();
  });
});

describe("a collapsed event", () => {
  it("shows how many occurrences it stands for", async () => {
    vi.mocked(service.readTrail).mockResolvedValue(
      page({ events: [event({ collapsed: true, count: 47, result: "refused", decision: "deny" })] }),
    );
    render(<AuditTrailView />);
    expect(await screen.findByText("×47")).toBeTruthy();
  });

  it("does not decorate an ordinary event with a count", async () => {
    vi.mocked(service.readTrail).mockResolvedValue(page());
    render(<AuditTrailView />);
    await screen.findByText("rack.power:off");
    expect(screen.queryByText(/^×/)).toBeNull();
  });
});

describe("the two empty states are different sentences", () => {
  it("an empty filtered result names how far back the trail reaches", async () => {
    vi.mocked(service.readTrail).mockResolvedValue(
      page({ events: [], oldest_held: "2026-06-12T08:00:00.000+00:00" }),
    );
    render(<AuditTrailView />);
    expect(await screen.findByText(/rotated away/)).toBeTruthy();
  });

  it("an empty trail says the record is empty, not that nothing matched", async () => {
    // The distinction TC-25 exists for: with no retention boundary to blame, "no events match"
    // would imply the filter excluded something.
    vi.mocked(service.readTrail).mockResolvedValue(page({ events: [], oldest_held: "" }));
    render(<AuditTrailView />);
    expect(await screen.findByText(/audit trail is empty/)).toBeTruthy();
    expect(screen.queryByText(/rotated away/)).toBeNull();
  });
});

describe("honesty about a partial answer", () => {
  it("says when the scan stopped early", async () => {
    vi.mocked(service.readTrail).mockResolvedValue(
      page({ events: [], truncated: true, scanned_events: 20000 }),
    );
    render(<AuditTrailView />);
    expect(await screen.findByText(/Stopped after scanning/)).toBeTruthy();
    expect(screen.getByText(/Narrow the time range/)).toBeTruthy();
  });

  it("says when a cursor was rotated away rather than showing an empty page", async () => {
    vi.mocked(service.readTrail).mockResolvedValue(page({ cursor_expired: true }));
    render(<AuditTrailView />);
    expect(await screen.findByText(/rotated out of the trail/)).toBeTruthy();
  });

  it("reports unreadable lines as expected rather than alarming", async () => {
    vi.mocked(service.readTrail).mockResolvedValue(page({ skipped_lines: 1 }));
    render(<AuditTrailView />);
    expect(await screen.findByText(/unreadable line/)).toBeTruthy();
    expect(screen.getByText(/normal after an unclean shutdown/)).toBeTruthy();
  });
});

describe("an attachment event", () => {
  it("names the principal a policy was granted to", async () => {
    // Without `target` the trail says a policy was attached and never to whom -- the one question
    // an attachment audit exists to answer.
    vi.mocked(service.readTrail).mockResolvedValue(
      page({
        events: [
          event({ action: "policy:attach", resource: "policy:administrators", target: "user:bob" }),
        ],
      }),
    );
    render(<AuditTrailView />);
    expect(await screen.findByText(/user:bob/)).toBeTruthy();
  });
});

describe("pagination", () => {
  /** A full page plus a cursor is what tells the view more exists. */
  const fullPage = (ids: string[], cursor: string) =>
    page({ events: ids.map((id) => event({ event_id: id })), cursor });

  it("asks for the next page by cursor, not by offset", async () => {
    // The property the whole design rests on. An offset over an append-only trail shifts between
    // requests and would both repeat and SKIP events; the server pages by `event_id` instead.
    const first = fullPage(Array.from({ length: 50 }, (_, i) => `id-${i}`), "cursor-50");
    vi.mocked(service.readTrail).mockResolvedValue(first);
    render(<AuditTrailView />);
    await screen.findByRole("table");

    fireEvent.click(screen.getByRole("button", { name: /next page/i }));

    await waitFor(() => expect(service.readTrail).toHaveBeenCalledTimes(2));
    const [, options] = vi.mocked(service.readTrail).mock.calls[1]!;
    expect(options?.cursor).toBe("cursor-50");
  });

  it("returns to the newest page with no cursor when going back", async () => {
    const first = fullPage(Array.from({ length: 50 }, (_, i) => `a-${i}`), "cursor-a");
    vi.mocked(service.readTrail).mockResolvedValue(first);
    render(<AuditTrailView />);
    await screen.findByRole("table");

    fireEvent.click(screen.getByRole("button", { name: /next page/i }));
    await waitFor(() => expect(service.readTrail).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole("button", { name: /previous page/i }));

    await waitFor(() => expect(service.readTrail).toHaveBeenCalledTimes(3));
    const [, options] = vi.mocked(service.readTrail).mock.calls[2]!;
    expect(options?.cursor).toBe("");
  });

  it("disables Next on the last page", async () => {
    // A short page is the end. Leaving Next enabled would send an operator to an empty page and
    // leave them unsure whether that meant "no more" or "something failed".
    vi.mocked(service.readTrail).mockResolvedValue(page({ cursor: "" }));
    render(<AuditTrailView />);
    await screen.findByRole("table");
    expect(screen.getByRole("button", { name: /next page/i })).toHaveProperty("disabled", true);
  });

  it("changing the page size returns to the newest page", async () => {
    // A held cursor anchors into the PREVIOUS result set; resuming from it after a size change
    // would page through an answer nobody asked for.
    const first = fullPage(Array.from({ length: 50 }, (_, i) => `b-${i}`), "cursor-b");
    vi.mocked(service.readTrail).mockResolvedValue(first);
    render(<AuditTrailView />);
    await screen.findByRole("table");

    fireEvent.click(screen.getByRole("button", { name: /next page/i }));
    await waitFor(() => expect(service.readTrail).toHaveBeenCalledTimes(2));

    fireEvent.mouseDown(screen.getByRole("combobox"));
    fireEvent.click(screen.getByRole("option", { name: "100" }));

    await waitFor(() => {
      const last = vi.mocked(service.readTrail).mock.calls.at(-1)!;
      expect(last[1]?.cursor).toBe("");
      expect(last[1]?.limit).toBe(100);
    });
  });
});

describe("export", () => {
  it("exports the filter rather than the visible page", async () => {
    // The screen holds one page. Exporting that would hand somebody 50 rows of a 3,000-row answer
    // and look entirely right, so the filter is what travels to the server.
    vi.mocked(service.readTrail).mockResolvedValue(page());
    render(<AuditTrailView />);
    await screen.findByRole("table");

    fireEvent.click(screen.getByRole("button", { name: /export the audit trail as csv/i }));

    await waitFor(() => expect(service.downloadExport).toHaveBeenCalled());
    const [filter, format] = vi.mocked(service.downloadExport).mock.calls[0]!;
    expect(format).toBe("csv");
    expect(filter.since, "the export dropped the active filter").toBeTruthy();
  });

  it("offers both formats", async () => {
    vi.mocked(service.readTrail).mockResolvedValue(page());
    render(<AuditTrailView />);
    await screen.findByRole("table");

    fireEvent.click(screen.getByRole("button", { name: /export the audit trail as json/i }));
    await waitFor(() => expect(service.downloadExport).toHaveBeenCalled());
    expect(vi.mocked(service.downloadExport).mock.calls[0]![1]).toBe("json");
  });

  it("reports a refused export instead of failing silently", async () => {
    // A refusal must not be a file: `downloadExport` fetches rather than following a link so a 403
    // surfaces here, instead of the operator receiving `audit-trail.json` containing an error.
    vi.mocked(service.readTrail).mockResolvedValue(page());
    vi.mocked(service.downloadExport).mockRejectedValue(new Error("not authorized"));
    render(<AuditTrailView />);
    await screen.findByRole("table");

    fireEvent.click(screen.getByRole("button", { name: /export the audit trail as csv/i }));
    expect(await screen.findByText(/not authorized/)).toBeTruthy();
  });
});

describe("the panel", () => {
  it("keeps the pager inside the table's panel, not in a row beneath it", async () => {
    // Requested to match NodesView: the table and its footer should read as one panel. Asserted
    // structurally, because "looks integrated" is otherwise only checkable by eye -- the pager has
    // to be a descendant of the same container as the table.
    vi.mocked(service.readTrail).mockResolvedValue(page());
    render(<AuditTrailView />);
    const table = await screen.findByRole("table");

    const panel = table.closest(".MuiTableContainer-root");
    expect(panel, "the table is not inside a TableContainer").toBeTruthy();
    expect(
      panel!.querySelector(".MuiTablePagination-root"),
      "the pager sits outside the table's panel",
    ).toBeTruthy();
  });

  it("states the retention bound beside the pager", async () => {
    // FR-10, and it had no test until now: the view must name the oldest event actually HELD, so
    // an empty page a month back reads as "we no longer hold that" rather than "nothing happened".
    vi.mocked(service.readTrail).mockResolvedValue(
      page({ oldest_held: "2026-06-12T08:00:00.000+00:00" }),
    );
    render(<AuditTrailView />);
    expect(await screen.findByText(/Trail reaches back to/)).toBeTruthy();
  });

  it("says nothing about retention when the server reports none", async () => {
    vi.mocked(service.readTrail).mockResolvedValue(page({ oldest_held: "" }));
    render(<AuditTrailView />);
    await screen.findByRole("table");
    expect(screen.queryByText(/Trail reaches back to/)).toBeNull();
  });
});
