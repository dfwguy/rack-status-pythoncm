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
