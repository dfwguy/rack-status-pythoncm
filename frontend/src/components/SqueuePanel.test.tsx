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
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Column sorting on the Scheduler squeue table. The property that matters is that sorting spans
// the WHOLE queue, not the visible page: with 300 pending jobs an operator sorts by TIME to find
// the longest-running one, and it is worthless if it only reorders the 25 rows already on screen.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SqueuePanel } from "./SqueuePanel";

const HEADER = "             JOBID PARTITION     NAME     USER ST       TIME  NODES NODELIST(REASON)";

/** One squeue line, padded the way squeue pads it. */
const line = (jobid: string, name: string, user: string, st: string, time: string, nodes: string, nodelist: string) =>
  `${jobid.padStart(18)} ${"defq".padStart(9)} ${name.padStart(8)} ${user.padStart(8)} ${st.padStart(2)} ${time.padStart(10)} ${nodes.padStart(6)} ${nodelist}`;

/** 30 jobs so the table pages at the default 25, plus the two that must be found by sorting. */
function squeueStdout(): string {
  const rows = [
    line("140212", "microben", "cmods", "PD", "0:00", "18", "(ReqNodeNotAvail)"),
    line("99", "oldest", "alice", "R", "1-02:03:04", "9", "ink50-dh01-h9"),
  ];
  for (let i = 0; i < 28; i++) {
    rows.push(line(String(140300 + i), "bringup-", "cmods", "R", "2:59", "1", `ink50-dh01-j05-${10 + i}`));
  }
  return [HEADER, ...rows].join("\n");
}

function mockSqueue(stdout: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ ok: true, stdout, command_display: "squeue" }),
      json: async () => ({ ok: true, stdout, command_display: "squeue" }),
    }),
  );
}

/** Text of the first cell of every rendered body row. */
function firstColumn(): string[] {
  const rows = screen.getAllByRole("row").slice(1); // drop the header row
  return rows.map((r) => within(r).getAllByRole("cell")[0]?.textContent?.trim() ?? "");
}

describe("SqueuePanel column sorting", () => {
  beforeEach(() => {
    mockSqueue(squeueStdout());
  });

  it("sorts by job id numerically, not as text", async () => {
    render(<SqueuePanel active />);
    await waitFor(() => expect(screen.getByText("JOBID")).toBeInTheDocument());

    fireEvent.click(screen.getByText("JOBID"));
    // 99 must lead: as a string it would sort after every 140xxx id
    await waitFor(() => expect(firstColumn()[0]).toBe("99"));
  });

  it("reverses on a second click and restores the original order on a third", async () => {
    render(<SqueuePanel active />);
    await waitFor(() => expect(screen.getByText("JOBID")).toBeInTheDocument());
    const original = firstColumn()[0];

    fireEvent.click(screen.getByText("JOBID"));
    await waitFor(() => expect(firstColumn()[0]).toBe("99"));

    fireEvent.click(screen.getByText("JOBID"));
    await waitFor(() => expect(firstColumn()[0]).toBe("140327")); // highest of the 140300..140327 block

    fireEvent.click(screen.getByText("JOBID"));
    await waitFor(() => expect(firstColumn()[0]).toBe(original));
  });

  it("sorts the whole queue, so a row from a later page can reach page 1", async () => {
    render(<SqueuePanel active />);
    await waitFor(() => expect(screen.getByText("TIME")).toBeInTheDocument());

    // job 99 (1-02:03:04) is the 2nd row of 30 and the longest running; sorting TIME
    // descending must bring it to the top of page 1.
    fireEvent.click(screen.getByText("TIME"));
    fireEvent.click(screen.getByText("TIME"));
    await waitFor(() => expect(firstColumn()[0]).toBe("99"));
  });

  it("orders elapsed time by duration rather than lexically", async () => {
    render(<SqueuePanel active />);
    await waitFor(() => expect(screen.getByText("TIME")).toBeInTheDocument());

    fireEvent.click(screen.getByText("TIME"));
    // "0:00" first, and the day-long job last -- a text sort would put "1-02:03:04" first
    await waitFor(() => expect(firstColumn()[0]).toBe("140212"));
  });
});

describe("SqueuePanel open log", () => {
  it("says a pending job has not started instead of tailing a log that does not exist yet", async () => {
    // Job 194782 on Nscale NC: queued behind another 36-node run, so its StdOut file was never
    // created and the tail dialog said only "path does not exist".
    const scontrol =
      "JobId=140212 JobName=microben\n   JobState=PENDING Reason=Resources Dependency=(null)\n" +
      "   StdOut=/home/cmods/nvis/logs/root-NEMO-U20+U23-36N-140212.txt";
    const respond = (body: object) => ({
      ok: true,
      status: 200,
      text: async () => JSON.stringify(body),
      json: async () => body,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (url: RequestInfo | URL) =>
        String(url instanceof Request ? url.url : url).includes("scheduler-job-detail")
          ? respond({ ok: true, stdout: scontrol })
          : respond({ ok: true, stdout: squeueStdout(), command_display: "squeue" }),
      ),
    );
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    render(<SqueuePanel active />);
    await waitFor(() => expect(screen.getByText("JOBID")).toBeInTheDocument());
    const row = screen.getAllByRole("row").find((r) => within(r).queryAllByRole("cell")[0]?.textContent?.trim() === "140212") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /open log/i }));
    await waitFor(() => expect(alert).toHaveBeenCalled());
    expect(alert.mock.calls[0][0]).toBe(
      "Job 140212 has not started yet (Resources). Its log, " +
        "/home/cmods/nvis/logs/root-NEMO-U20+U23-36N-140212.txt, is created when the job starts.",
    );
    expect(screen.queryByText(/^tail -f/)).toBeNull();
    alert.mockRestore();
  });
});

describe("SqueuePanel status column", () => {
  beforeEach(() => {
    mockSqueue(squeueStdout());
  });

  it("calls squeue's ST column Status and spells each state out", async () => {
    // A queue of PD and R next to node lists that are sometimes hosts and sometimes a reason: the
    // words say at a glance which jobs have not started.
    render(<SqueuePanel active />);
    await waitFor(() => expect(screen.getByText("JOBID")).toBeInTheDocument());
    expect(screen.getByText("Status")).toBeInTheDocument();
    expect(screen.queryByText("ST")).toBeNull();
    const rows = screen.getAllByRole("row").slice(1);
    const statusOf = (jobId: string) => {
      const row = rows.find((r) => within(r).getAllByRole("cell")[0]?.textContent?.trim() === jobId);
      return within(row as HTMLElement).getAllByRole("cell")[4];
    };
    expect(statusOf("140212").textContent).toBe("Pending");
    expect(statusOf("140212").getAttribute("title")).toBe("PD"); // the code stays on hover
    expect(statusOf("140300").textContent).toBe("Running");
  });
});
