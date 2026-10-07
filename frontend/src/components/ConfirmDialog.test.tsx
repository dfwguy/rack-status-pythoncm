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
import { fireEvent, render, screen, waitFor, waitForElementToBeRemoved } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useConfirm } from "./useConfirm";

function Harness({ onResult }: { onResult: (v: boolean) => void }) {
  const { confirm, showAlert, confirmDialog } = useConfirm();
  return (
    <div>
      <button onClick={() => void confirm({ message: "Really remove it?", danger: true }).then(onResult)}>ask</button>
      <button onClick={() => void showAlert({ title: "Oops", message: "it failed" }).then(() => onResult(true))}>warn</button>
      {confirmDialog}
    </div>
  );
}

describe("useConfirm", () => {
  it("resolves true when the user confirms", async () => {
    const onResult = vi.fn();
    render(<Harness onResult={onResult} />);
    fireEvent.click(screen.getByText("ask"));
    await screen.findByText("Really remove it?");
    fireEvent.click(screen.getByText("Delete")); // danger default label
    await waitFor(() => expect(onResult).toHaveBeenCalledWith(true));
  });

  it("resolves false when the user cancels", async () => {
    const onResult = vi.fn();
    render(<Harness onResult={onResult} />);
    fireEvent.click(screen.getByText("ask"));
    await screen.findByText("Really remove it?");
    fireEvent.click(screen.getByText("Cancel"));
    await waitFor(() => expect(onResult).toHaveBeenCalledWith(false));
  });

  it("alert mode hides Cancel and resolves on acknowledge", async () => {
    const onResult = vi.fn();
    render(<Harness onResult={onResult} />);
    fireEvent.click(screen.getByText("warn"));
    await screen.findByText("it failed");
    expect(screen.queryByText("Cancel")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("OK"));
    await waitFor(() => expect(onResult).toHaveBeenCalledWith(true));
  });

  it("keeps what it asked on screen while it closes, then leaves the page usable", async () => {
    const onResult = vi.fn();
    render(<Harness onResult={onResult} />);
    fireEvent.click(screen.getByText("ask"));
    await screen.findByText("Really remove it?");
    fireEvent.click(screen.getByText("Delete"));
    // Mid-exit the dialog is still mounted: it must still show the question and its danger label,
    // not repaint as a blank default "Confirm" dialog.
    expect(screen.getByText("Really remove it?")).toBeInTheDocument();
    expect(screen.getByText("Delete")).toBeInTheDocument();
    await waitForElementToBeRemoved(() => screen.queryByRole("dialog", { hidden: true }));
    expect(screen.getByRole("button", { name: "ask" })).toBeInTheDocument(); // no longer aria-hidden
    expect(onResult).toHaveBeenCalledWith(true);
  });
});
