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
 * The wizard as an operator meets it: stepping, backtracking, and the key that never lingers.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SetupWizard } from "./SetupWizard";

vi.mock("../../services/catalogService", () => ({
  listPlatforms: vi.fn(async () => [
    {
      id: "gb300-nvl72",
      name: "GB300 NVL72",
      gpu_model: "GB300",
      geometry: { gpus_per_node: 4, nodes_per_tray: 1, trays_per_rack: 18, gpus_per_rack: 72, nvlink_domain: 72 },
    },
  ]),
  saveProject: vi.fn(async (p: unknown) => p),
  setActiveContext: vi.fn(async () => ({})),
}));

vi.mock("../../services/setupService", () => ({
  proposeNaming: vi.fn(async () => ({
    ok: true,
    hostname: "ink50-dh01-h15-15",
    caveat: "Derived from one hostname, so check the rack id and tray below.",
    candidates: [
      {
        rack_id_template: "ink[01-99]-dh[01-99]-[a-z][01-99]",
        ct_id_template: "ink[01-99]-dh[01-99]-[a-z][01-99]-[01-99]",
        nvsw_id_template: "ink[01-99]-dh[01-99]-[a-z][01-99]-nvsw-[\\d+]",
        ct_joiner: "-",
        node_id_mode: "tray",
        parsed: { hostname: "ink50-dh01-h15-15", rack_id: "ink50-dh01-h15", tray: "15" },
        breakdown: { cluster: "ink50", row: "h", rack: "ink50-dh01-h15" },
        rack_digit_groups: 3,
        row_is_a_slot: true,
      },
    ],
  })),
  storeSshKey: vi.fn(async () => ({
    path: "/app/var/secrets/ssh/nscale-tx-nscale-tx.key",
    fingerprint: "sha256:abcd1234",
  })),
  bootstrapProjectScripts: vi.fn(async () => ({
    project: "nscale-tx",
    platform: "gb300-nvl72",
    dest: "/base/scripts/project-nscale-tx",
    copied: 108,
    differs_from_defaults: 0,
    forced: false,
    layout: {} as never,
  })),
  completeSetup: vi.fn(async () => ({ setup: { required: false, reason: "project-configured", forced: false } })),
  previewScriptDeploy: vi.fn(async () => ({
    ok: true,
    dry_run: true,
    dest: "/base/scripts",
    files: 109,
    bytes: 282879,
    layers: ["multi-platform"],
    chunks: 5,
    layout: {} as never,
  })),
  deployScripts: vi.fn(async () => ({
    ok: true,
    dry_run: false,
    dest: "/base/scripts",
    files: 109,
    bytes: 282879,
    layers: ["multi-platform"],
    chunks: 5,
    layout: {} as never,
  })),
}));

const type = (label: RegExp | string, value: string) => {
  const input = screen.getByLabelText(label);
  fireEvent.change(input, { target: { value } });
};

const clickNext = () => fireEvent.click(screen.getByRole("button", { name: /next/i }));

/**
 * Drive a KUI Select: open it, then click the option.
 *
 * Not a native `<select>`, so `fireEvent.change` on it does nothing at all — silently, which is
 * the trap. Every existing test in this repo only *reads* a combobox; this is the first to
 * operate one, and it works exactly like a user does.
 */
const choose = async (comboboxIndex: number, optionText: RegExp | string) => {
  fireEvent.click(screen.getAllByRole("combobox")[comboboxIndex]);
  // Driving a KUI Select under jsdom needs three concessions, each measured rather than guessed:
  //
  //   * `hidden: true` — the listbox stays in the DOM and is `aria-hidden` while closed, and
  //     none of click, the full pointer sequence, or the documented ArrowDown opens it here.
  //   * the trigger itself also carries `role="option"`, so matching on text is what separates
  //     the items from the control.
  //   * each item renders **twice** — one copy is inert (a measurement clone), and clicking the
  //     first match silently does nothing, which is what made this look like a wizard bug.
  //
  // So: click every match. A click on the inert copy is a no-op, and the live one selects.
  const matches = (text: string) =>
    typeof optionText === "string" ? text.includes(optionText) : optionText.test(text);
  const wanted = screen
    .getAllByRole("option", { hidden: true })
    .filter((o) => matches(o.textContent || ""));
  if (wanted.length === 0) throw new Error(`no option matching ${optionText}`);
  wanted.forEach((o) => fireEvent.click(o));
};

/** Steps 1 and 2, which every later step needs answered. */
const throughProject = async () => {
  type(/project name/i, "Nscale TX");
  type(/customer or site/i, "Nscale");
  await waitFor(() => expect(screen.getByRole("button", { name: /next/i })).toBeEnabled());
  clickNext();
  await screen.findByLabelText(/site prefix/i);
  await choose(0, /GB300 NVL72/);
  await waitFor(() => expect(screen.getByRole("button", { name: /next/i })).toBeEnabled());
  clickNext();
  await screen.findByLabelText(/example compute node hostname/i);
};

describe("SetupWizard", () => {
  beforeEach(() => vi.clearAllMocks());

  it("refuses to advance until the step is answered, and says why", async () => {
    render(<SetupWizard />);
    const next = screen.getByRole("button", { name: /next/i });
    expect(next).toBeDisabled();
    // The reason is on screen, not only in the disabled state.
    expect(screen.getByText(/give the project a name/i)).toBeInTheDocument();

    type(/project name/i, "Nscale TX");
    type(/customer or site/i, "Nscale");
    await waitFor(() => expect(screen.getByRole("button", { name: /next/i })).toBeEnabled());
  });

  it("keeps what was typed when stepping back", async () => {
    render(<SetupWizard />);
    type(/project name/i, "Nscale TX");
    type(/customer or site/i, "Nscale");
    await waitFor(() => expect(screen.getByRole("button", { name: /next/i })).toBeEnabled());
    clickNext();

    await screen.findByLabelText(/site prefix/i);
    fireEvent.click(screen.getByRole("button", { name: /back/i }));

    // A wizard that loses the previous step's answers is one nobody backtracks in.
    expect((await screen.findByLabelText(/project name/i)) as HTMLInputElement).toHaveValue(
      "Nscale TX",
    );
  });

  it("shows the parse, not only the regex, for each naming proposal", async () => {
    const { proposeNaming } = await import("../../services/setupService");
    render(<SetupWizard />);
    await throughProject();

    type(/example compute node hostname/i, "ink50-dh01-h15-15");
    fireEvent.click(screen.getByRole("button", { name: /derive/i }));

    await waitFor(() => expect(proposeNaming).toHaveBeenCalledWith("ink50-dh01-h15-15"));
    // The rack and tray it derived, against the operator's own hostname: the thing they can
    // actually check. A template alone asks them to approve a regex.
    expect(await screen.findByText(/rack ink50-dh01-h15 · tray 15/)).toBeInTheDocument();
    // And the ambiguity is stated rather than hidden.
    expect(screen.getByText(/Derived from one hostname/i)).toBeInTheDocument();
  });

  it("exchanges key material for a path and stops holding the material", async () => {
    const { storeSshKey } = await import("../../services/setupService");
    render(<SetupWizard />);
    await throughProject();
    type(/rack id template/i, "ink[01-99]");
    type(/compute node template/i, "ink[01-99]-[01-99]");
    type(/trays per rack/i, "18");
    await waitFor(() => expect(screen.getByRole("button", { name: /next/i })).toBeEnabled());
    clickNext();

    const box = await screen.findByPlaceholderText(/BEGIN OPENSSH PRIVATE KEY/i);
    fireEvent.change(box, {
      target: { value: "-----BEGIN OPENSSH PRIVATE KEY-----\nxxx\n-----END OPENSSH PRIVATE KEY-----" },
    });
    fireEvent.click(screen.getByRole("button", { name: /store key/i }));

    await waitFor(() => expect(storeSshKey).toHaveBeenCalled());
    // Dropped from component state the moment it has a home on disk: a private key sitting in a
    // React tree is readable by every error boundary and dev tool for the rest of the session.
    await waitFor(() => expect(box).toHaveValue(""));
    expect(await screen.findByText(/nscale-tx-nscale-tx\.key/)).toBeInTheDocument();
  });

  it("offers Cancel only when it was given somewhere to go", () => {
    // At first login there is nothing behind the wizard, so an escape hatch would be a dead end.
    const { rerender } = render(<SetupWizard />);
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
    rerender(<SetupWizard onCancel={() => {}} />);
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
  });

  it("completes setup without rack naming or layout", async () => {
    const { saveProject, setActiveContext } = await import("../../services/catalogService");
    const { completeSetup } = await import("../../services/setupService");
    const finished = vi.fn();
    render(<SetupWizard onFinished={finished} />);
    await throughProject();

    expect(screen.getByText(/without requiring rack assignments/i)).toBeInTheDocument();
    type(/trays per rack/i, "");
    expect(screen.getByRole("button", { name: /next/i })).toBeEnabled();
    clickNext();
    await screen.findByLabelText(/ssh host/i);
    type(/ssh host/i, "10.0.0.2");
    clickNext();
    fireEvent.click(await screen.findByLabelText(/send the shipped scripts/i));
    fireEvent.click(screen.getByLabelText(/copy the scripts into this project/i));
    clickNext();
    expect(await screen.findByText("Platform default")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /create project/i }));

    await waitFor(() => expect(finished).toHaveBeenCalledWith("nscale-tx"));
    expect(saveProject).toHaveBeenCalledWith(expect.objectContaining({
      clusters: [expect.objectContaining({
        naming: expect.objectContaining({ rack_id_template: "", ct_id_template: "" }),
        geometry: expect.objectContaining({ trays_per_rack: 0 }),
      })],
    }));
    expect(setActiveContext).toHaveBeenCalledWith({ project: "nscale-tx", cluster: "nscale-tx" });
    expect(completeSetup).toHaveBeenCalledWith("nscale-tx");
  });

  describe("the Scripts step", () => {
    /** Steps 1-4, leaving the wizard on Scripts. */
    const throughToScripts = async () => {
      await throughProject();
      type(/rack id template/i, "ink[01-99]");
      type(/compute node template/i, "ink[01-99]-[01-99]");
      type(/trays per rack/i, "18");
      await waitFor(() => expect(screen.getByRole("button", { name: /next/i })).toBeEnabled());
      clickNext();
      await screen.findByPlaceholderText(/BEGIN OPENSSH PRIVATE KEY/i);
      type(/ssh host/i, "10.0.0.2");
      await waitFor(() => expect(screen.getByRole("button", { name: /next/i })).toBeEnabled());
      clickNext();
      await screen.findByLabelText(/send the shipped scripts/i);
    };

    it("offers sending the shipped scripts as well as copying them into the project", async () => {
      render(<SetupWizard />);
      await throughToScripts();
      // Both halves are on by default: a fresh head needs the first, and the second is what
      // the project actually runs from.
      expect(screen.getByLabelText(/send the shipped scripts/i)).toBeChecked();
      expect(screen.getByLabelText(/copy the scripts into this project/i)).toBeChecked();
    });

    it("shows how much a deploy would send, without contacting the cluster", async () => {
      const { previewScriptDeploy } = await import("../../services/setupService");
      render(<SetupWizard />);
      await throughToScripts();
      expect(previewScriptDeploy).toHaveBeenCalled();
      expect(await screen.findByText(/109 file\(s\)/)).toBeInTheDocument();
    });

    it("warns when the project copy is asked for without the deploy that feeds it", async () => {
      // The bootstrap composes a project's copy FROM the shared layers, so this combination
      // fails on a head that has never had them -- the failure this step exists to prevent.
      render(<SetupWizard />);
      await throughToScripts();
      fireEvent.click(screen.getByLabelText(/send the shipped scripts/i));
      expect(await screen.findByText(/never had them/i)).toBeInTheDocument();
    });

    it("deploys before it bootstraps, because the copy is composed from what was sent",
      async () => {
        const { deployScripts, bootstrapProjectScripts, completeSetup } = await import(
          "../../services/setupService"
        );
        const order: string[] = [];
        vi.mocked(deployScripts).mockImplementation(async () => {
          order.push("deploy");
          return { ok: true, dry_run: false, dest: "/base/scripts", files: 109, bytes: 1,
            layers: ["multi-platform"], chunks: 5, layout: {} as never };
        });
        vi.mocked(bootstrapProjectScripts).mockImplementation(async () => {
          order.push("bootstrap");
          return { project: "p", platform: "gb300-nvl72", dest: "/base/scripts/project-p",
            copied: 108, differs_from_defaults: 0, forced: false, layout: {} as never };
        });

        render(<SetupWizard />);
        await throughToScripts();
        clickNext();
        fireEvent.click(await screen.findByRole("button", { name: /create project/i }));

        await waitFor(() => expect(completeSetup).toHaveBeenCalled());
        expect(order).toEqual(["deploy", "bootstrap"]);
      });

    it("still completes setup when the scripts fail, and says which half got through",
      async () => {
        // The project exists and is active by then. Discarding everything the operator typed
        // for a step that is repeatable from the Projects screen would be the worse failure.
        const { deployScripts, bootstrapProjectScripts, completeSetup } = await import(
          "../../services/setupService"
        );
        vi.mocked(deployScripts).mockResolvedValue({
          ok: true, dry_run: false, dest: "/base/scripts", files: 109, bytes: 1,
          layers: ["multi-platform"], chunks: 5, layout: {} as never,
        });
        vi.mocked(bootstrapProjectScripts).mockRejectedValue(new Error("no space left on device"));

        render(<SetupWizard />);
        await throughToScripts();
        clickNext();
        fireEvent.click(await screen.findByRole("button", { name: /create project/i }));

        await waitFor(() => expect(completeSetup).toHaveBeenCalled());
        const banner = await screen.findByText(/no space left on device/);
        expect(banner).toBeInTheDocument();
        // Naming the half that worked is the difference between a usable report and "scripts
        // failed" with two mechanisms behind it. It appears twice by design -- in the error
        // and in the review summary -- so the operator sees it wherever they are looking.
        expect(screen.getAllByText(/sent 109 shipped file\(s\)/).length).toBeGreaterThan(0);
      });
  });
});
