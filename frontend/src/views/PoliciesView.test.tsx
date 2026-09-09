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
 * `PoliciesView` — AC-15j: the table lists each policy and what it is attached to.
 *
 * The attachment column is the reason this test exists rather than a screenshot. In the IAM-style
 * model a policy document names no principals, so **the document tells you nothing about who it
 * affects** — the attachment relation is the only thing that does. An operator narrowing or
 * deleting a policy is therefore reading this column to decide whether the change is safe, and the
 * two failures that matter are silent ones:
 *
 * 1. an attached policy rendering as unattached, which makes a live grant look inert;
 * 2. a delete confirmation that does not say who loses access, which turns "remove an unused
 *    document" into "revoke four people's permissions" with the same two clicks.
 *
 * `fireEvent` rather than `user-event`, matching the other 28 files in this suite — the package is
 * not a dependency and adding one for a single file is not worth the install.
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as policyService from "../services/policyService";
import type { PolicyRow } from "../types/policy";
import PoliciesView from "./PoliciesView";

vi.mock("../services/policyService");

/** A row shaped like the list endpoint's, so a field rename in the type breaks this too. */
const policy = (over: Partial<PolicyRow> = {}): PolicyRow => ({
  id: "rack-operators",
  description: "Power and status on the operator racks.",
  statements: [
    {
      sid: "power",
      effect: "allow",
      actions: ["rack.power:*"],
      resources: ["rack.power:*"],
    },
  ],
  attached_to: [],
  attachment_count: 0,
  ...over,
});

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(policyService.fetchVocabulary).mockResolvedValue({
    services: {},
    actions: ["rack.power:on", "rack.power:off", "rack.status:read"],
  } as never);
  vi.mocked(policyService.fetchPolicySchema).mockResolvedValue({});
});

describe("the policy table", () => {
  it("lists every policy the endpoint returns", async () => {
    vi.mocked(policyService.listPolicies).mockResolvedValue([
      policy({ id: "rack-operators" }),
      policy({ id: "read-only" }),
    ]);

    render(<PoliciesView />);

    expect(await screen.findByText("rack-operators")).toBeTruthy();
    expect(screen.getByText("read-only")).toBeTruthy();
  });

  it("names the subjects a policy is attached to", async () => {
    // The load-bearing case: the document does not name principals, so this column is the only
    // place an operator can learn that narrowing this policy affects Alice and the ops group.
    vi.mocked(policyService.listPolicies).mockResolvedValue([
      policy({ attached_to: ["user:alice", "group:ops"], attachment_count: 2 }),
    ]);

    render(<PoliciesView />);

    await waitFor(() => expect(screen.getByText(/user:alice/)).toBeTruthy());
    expect(screen.getByText(/group:ops/)).toBeTruthy();
  });

  it("distinguishes an unattached policy from an attached one", async () => {
    // A policy attached to nobody grants nothing. Rendering the two states identically would make
    // a live grant look inert and an inert one look live -- the reading an operator acts on.
    vi.mocked(policyService.listPolicies).mockResolvedValue([
      policy({ id: "unused", attached_to: [], attachment_count: 0 }),
    ]);

    render(<PoliciesView />);
    await screen.findByText("unused");

    // Whatever the empty state says, it must not be a subject-shaped string.
    expect(screen.queryByText(/user:/)).toBeNull();
    expect(screen.queryByText(/group:/)).toBeNull();
  });

  it("reports a load failure instead of rendering an empty table", async () => {
    // An empty table and a failed request look the same to an operator, and the wrong conclusion
    // -- "there are no policies, so nothing is enforced" -- is the dangerous one.
    vi.mocked(policyService.listPolicies).mockRejectedValue(new Error("network is down"));

    render(<PoliciesView />);

    await waitFor(() => expect(screen.getByText(/network is down/)).toBeTruthy());
  });
});

describe("deleting a policy", () => {
  it("says who loses access, not just 'are you sure'", async () => {
    vi.mocked(policyService.listPolicies).mockResolvedValue([
      policy({ attached_to: ["user:alice", "group:ops"], attachment_count: 2 }),
    ]);

    render(<PoliciesView />);
    await screen.findByText("rack-operators");

    // Addressed by its accessible name. Writing this test is what surfaced that the control had
    // none: an icon-only `Trash2` button, so the one action on the row that can revoke access was
    // the one a screen reader announced as just "button". Labelling it fixed the product, and
    // means this case now also guards that label.
    fireEvent.click(screen.getByRole("button", { name: /delete policy rack-operators/i }));

    // Scoped to the dialog. An unscoped query matches twice -- the row and the dialog -- and
    // `getByText` throws on multiple matches, so the assertion has to say *where* it expects the
    // subjects. Scoping is also what makes it meaningful: finding `user:alice` somewhere on the
    // page would be satisfied by the table behind the dialog, which proves nothing about the
    // confirmation.
    const dialog = within(await screen.findByRole("dialog"));

    // The count and the subjects both, because "attached to 2 subjects" without naming them
    // still leaves the operator guessing which two.
    expect(dialog.getByText(/user:alice/)).toBeTruthy();
    expect(dialog.getByText(/group:ops/)).toBeTruthy();
    expect(dialog.getByText(/changes what they can do|attached to 2/i)).toBeTruthy();
  });
});
