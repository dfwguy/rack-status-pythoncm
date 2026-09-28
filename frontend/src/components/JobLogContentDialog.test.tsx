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
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { JobLogContentDialog } from "./JobLogContentDialog";

function mockFetch(json: unknown, ok = true, status = 200) {
  return vi.fn().mockResolvedValue({
    ok,
    status,
    json: async () => json,
  } as Response);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("JobLogContentDialog", () => {
  it("fetches and renders the whole file content", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({ ok: true, too_large: false, size_bytes: 12, filename: "allreduce.txt", content: "hello full log body" }),
    );
    render(
      <JobLogContentDialog open onClose={() => {}} title="node / allreduce" path="/x/results/n/allreduce.txt" logDir="/logs" />,
    );
    await waitFor(() => expect(screen.getByText("hello full log body")).toBeInTheDocument());
    expect((fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0]).toContain(
      "/api/scheduler-job-output?path=",
    );
  });

  it("offers a download link when the file is too large", async () => {
    vi.stubGlobal("fetch", mockFetch({ ok: true, too_large: true, size_bytes: 20 * 1024 * 1024, filename: "big.txt" }));
    render(<JobLogContentDialog open onClose={() => {}} title="node / big" path="/x/results/n/big.txt" />);
    const link = await screen.findByRole("link", { name: /Download the full log/ });
    expect(link).toHaveAttribute("href", expect.stringContaining("/api/scheduler-job-output-download?path="));
  });

  it("renews the session before the browser downloads the full log", async () => {
    vi.stubGlobal("fetch", mockFetch({ ok: true, too_large: true, size_bytes: 20 * 1024 * 1024, filename: "big.txt" }));
    const tab = { opener: {} as unknown, location: { href: "about:blank" }, close: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    render(<JobLogContentDialog open onClose={() => {}} title="node / big" path="/x/results/n/big.txt" />);

    fireEvent.click(await screen.findByRole("link", { name: /Download the full log/ }));

    // A plain link cannot renew; the click opens a blank tab, checks the session, then navigates.
    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    await waitFor(() => expect(tab.location.href).toContain("/api/scheduler-job-output-download?path="));
    expect((fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0])).toContain("/api/auth-check");
  });

  it("surfaces an error from the endpoint", async () => {
    vi.stubGlobal("fetch", mockFetch({ ok: false, error: "path does not exist" }, false, 404));
    render(<JobLogContentDialog open onClose={() => {}} title="node / missing" path="/x/results/n/missing.txt" />);
    await waitFor(() => expect(screen.getByText("path does not exist")).toBeInTheDocument());
  });

  it("does not fetch when closed or path is null", () => {
    const f = mockFetch({ ok: true, content: "" });
    vi.stubGlobal("fetch", f);
    const { rerender } = render(<JobLogContentDialog open={false} onClose={() => {}} title="t" path="/x.txt" />);
    rerender(<JobLogContentDialog open onClose={() => {}} title="t" path={null} />);
    expect(f).not.toHaveBeenCalled();
  });
});
