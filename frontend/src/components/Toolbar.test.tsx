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
 * Both toolbar exports go through `apiDownload`. They were a `window.open` and a raw `fetch`, which
 * work until the access token expires and then fail for good — only `apiFetch` can renew it.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const apiDownload = vi.fn<(url: string, name: string) => Promise<void>>();

vi.mock("../lib/apiFetch", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/apiFetch")>()),
  apiDownload: (url: string, name: string) => apiDownload(url, name),
}));
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: "admin", logout: vi.fn() }),
}));
vi.mock("../context/RackLocksContext", () => ({
  useRackLocksContext: () => ({ locks: {}, me: null }),
}));
vi.mock("./ProjectSwitcher", () => ({ ProjectSwitcher: () => null }));

import { Toolbar } from "./Toolbar";

function renderToolbar() {
  return render(
    <Toolbar
      title="Racks"
      theme="dark"
      onToggleTheme={() => {}}
      onRefresh={() => {}}
      loading={false}
      sidebarOpen
      onMenuToggle={() => {}}
      logDir="/logs/run 1"
    />,
  );
}

afterEach(() => {
  apiDownload.mockReset();
  vi.restoreAllMocks();
});

describe("Toolbar exports", () => {
  it("downloads the log zip through apiDownload, with the log directory", async () => {
    apiDownload.mockResolvedValue();
    const open = vi.spyOn(window, "open");
    renderToolbar();

    fireEvent.click(screen.getByRole("button", { name: "Export logs (zip)" }));

    await waitFor(() => expect(apiDownload).toHaveBeenCalledTimes(1));
    expect(apiDownload).toHaveBeenCalledWith("/api/export-logs-zip?log_dir=%2Flogs%2Frun+1", "rack-logs.zip");
    expect(open).not.toHaveBeenCalled();
  });

  it("downloads the Excel export through apiDownload", async () => {
    apiDownload.mockResolvedValue();
    renderToolbar();

    fireEvent.click(screen.getByRole("button", { name: "Export parser results to Excel" }));

    await waitFor(() => expect(apiDownload).toHaveBeenCalledTimes(1));
    expect(apiDownload).toHaveBeenCalledWith(
      "/api/parser-export-excel?log_dir=%2Flogs%2Frun+1",
      "rack-parser-export.xlsx",
    );
  });

  it("shows the server's error when an export fails", async () => {
    apiDownload.mockRejectedValue(new Error("no exportable .txt logs"));
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    renderToolbar();

    fireEvent.click(screen.getByRole("button", { name: "Export logs (zip)" }));

    await waitFor(() => expect(alert).toHaveBeenCalledWith("no exportable .txt logs"));
  });
});
