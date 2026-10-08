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
// The NCCL-version selector is catalog-driven: the backend injects the builds a cluster staged,
// and the dialog renders them with the host "system" option always appended. These tests pin that
// contract and the fallback to the shipped list when the backend supplies nothing.
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { LaunchOptionsDialog, type NcclVersionOption } from "./LaunchOptionsDialog";

beforeAll(() => {
  if (!("ResizeObserver" in globalThis)) {
    (globalThis as Record<string, unknown>).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

const CATALOG_VERSIONS: NcclVersionOption[] = [
  { value: "2.32.3", label: "2.32.3 (latest)", order: 1 },
  { value: "2.30.7", label: "2.30.7 (validated)", order: 2 },
];

function openNcclSelect() {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: /NCCL version/i }));
  return within(screen.getByRole("listbox"));
}

describe("LaunchOptionsDialog NCCL version selector", () => {
  it("renders catalog versions plus the system option and defaults to the first catalog build", () => {
    render(
      <LaunchOptionsDialog
        open
        onClose={() => undefined}
        onConfirm={() => undefined}
        showNcclVersion
        ncclVersions={CATALOG_VERSIONS}
      />,
    );

    // The closed Select shows the default selection: the first catalog build.
    expect(screen.getByRole("combobox", { name: /NCCL version/i })).toHaveTextContent("2.32.3 (latest)");

    const options = openNcclSelect();
    expect(options.getByText("2.32.3 (latest)")).toBeInTheDocument();
    expect(options.getByText("2.30.7 (validated)")).toBeInTheDocument();
    expect(options.getByText("System default (2.28.9)")).toBeInTheDocument();
  });

  it("sorts catalog versions by order regardless of the array order it was given", () => {
    render(
      <LaunchOptionsDialog
        open
        onClose={() => undefined}
        onConfirm={() => undefined}
        showNcclVersion
        ncclVersions={[CATALOG_VERSIONS[1], CATALOG_VERSIONS[0]]}
      />,
    );
    // order:1 (2.32.3) still wins the default even though it was passed second.
    expect(screen.getByRole("combobox", { name: /NCCL version/i })).toHaveTextContent("2.32.3 (latest)");
  });

  it("confirms with the catalog default version when launched without changing the selection", () => {
    const onConfirm = vi.fn();
    render(
      <LaunchOptionsDialog
        open
        onClose={() => undefined}
        onConfirm={onConfirm}
        showNcclVersion
        ncclVersions={CATALOG_VERSIONS}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Launch" }));
    expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({ ncclVersion: "2.32.3" }));
  });

  it("falls back to the shipped list and default when no catalog versions are supplied", () => {
    render(
      <LaunchOptionsDialog open onClose={() => undefined} onConfirm={() => undefined} showNcclVersion />,
    );
    expect(screen.getByRole("combobox", { name: /NCCL version/i })).toHaveTextContent(
      "2.30.7 (vetted, default)",
    );
    const options = openNcclSelect();
    expect(options.getByText("2.30.7 (vetted, default)")).toBeInTheDocument();
    expect(options.getByText("System default (2.28.9)")).toBeInTheDocument();
  });
});
