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
// The catalog is also what launchers check node images against, via a copy published into the
// scripts' shared config directory. These pin that an operator sees whether that copy was updated on
// save, and that it can be re-published on its own.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getCatalog, publishCatalogToScripts, saveCatalog } from "../services/fleetService";
import CatalogEditorView from "./CatalogEditorView";

vi.mock("../services/fleetService", () => ({
  getCatalog: vi.fn(),
  saveCatalog: vi.fn(),
  publishCatalogToScripts: vi.fn(),
  startOperation: vi.fn(),
}));
vi.mock("../../../context/ActiveClusterContext", () => ({
  useActiveCluster: () => ({ activeCluster: { name: "Nscale NC" }, version: 0 }),
}));

const m = {
  getCatalog: getCatalog as unknown as ReturnType<typeof vi.fn>,
  saveCatalog: saveCatalog as unknown as ReturnType<typeof vi.fn>,
  publishCatalogToScripts: publishCatalogToScripts as unknown as ReturnType<typeof vi.fn>,
};

const image = {
  order: 1,
  name: "NeMo",
  filename: "nvidia+nemo+26.04.01-arm.sqsh",
  bytes: 42245001216,
  sha256: "ab".repeat(32),
  required_by: [],
};
const DEST = "/home/rkoike/nvis-rack-status/scripts/project-default/config/default";

beforeEach(() => {
  m.getCatalog.mockResolvedValue({ ok: true, cluster_id: "default", authored: true, images: [image] });
});
afterEach(() => vi.clearAllMocks());

describe("CatalogEditorView — the launchers' copy", () => {
  it("says where the launchers' copy went after a save", async () => {
    m.saveCatalog.mockResolvedValue({
      ok: true, cluster_id: "default", authored: true, images: [image],
      scripts_catalog: { ok: true, dest: DEST, images: 1 },
    });
    render(<CatalogEditorView />);
    // Save is rendered before the catalog loads and does nothing until it has; wait for the image.
    await screen.findByText(image.name);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(new RegExp(`Launchers now check node images against it \\(${DEST}\\)`))).toBeInTheDocument();
  });

  it("warns when the save landed but the launchers' copy did not", async () => {
    m.saveCatalog.mockResolvedValue({
      ok: true, cluster_id: "default", authored: true, images: [image],
      scripts_catalog: { ok: false, error: "OSError: no route" },
    });
    render(<CatalogEditorView />);
    // Save is rendered before the catalog loads and does nothing until it has; wait for the image.
    await screen.findByText(image.name);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/launchers' copy was NOT updated \(OSError: no route\)/)).toBeInTheDocument();
  });

  it("re-publishes on its own", async () => {
    m.publishCatalogToScripts.mockResolvedValue({ ok: true, dest: DEST, images: 1 });
    render(<CatalogEditorView />);
    const publish = await screen.findByRole("button", { name: "Publish to scripts" });
    // Enabled only once the authored catalog has loaded.
    await waitFor(() => expect(publish).toBeEnabled());
    fireEvent.click(publish);
    expect(await screen.findByText(`Catalog published for the launchers to ${DEST} (1 image(s)).`)).toBeInTheDocument();
    expect(m.publishCatalogToScripts).toHaveBeenCalledTimes(1);
  });
});

describe("CatalogEditorView — Required by", () => {
  it("picks the launches from the names the backend reports and saves them unchanged", async () => {
    const named = { ...image, required_by: ["nemo-26.04.01", "nemo-xrack"] };
    m.getCatalog.mockResolvedValue({
      ok: true, cluster_id: "default", authored: true, images: [named],
      consumers: [
        { id: "nemo-26.04.01", label: "NeMo 26.04.01" },
        { id: "nemo-xrack", label: "Multirack NEMO" },
      ],
    });
    m.saveCatalog.mockResolvedValue({ ok: true, cluster_id: "default", authored: true, images: [named] });
    render(<CatalogEditorView />);
    // A picker, not a free-text field: a typo there leaves a launch with no image.
    await screen.findByText("Required by");
    // The selected launches show as the picker's tags (its options are requiredByItems, tested in
    // lib/requiredBy.test.ts -- the KUI listbox does not render under jsdom).
    const tags = screen.getAllByRole("option", { hidden: true }).map((o) => o.textContent);
    expect(tags).toEqual(["nemo-26.04.01", "nemo-xrack"]);
    expect(screen.queryByPlaceholderText(/Comma-separated/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(m.saveCatalog).toHaveBeenCalledWith([named]));
  });
});
