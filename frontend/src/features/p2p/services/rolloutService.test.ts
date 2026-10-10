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
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiJson } from "../lib/apiFetch";
import {
  cancelRollout, getRollout, listRollouts, planRollout, retryRollout, setRolloutPacing, startRollout,
} from "./rolloutService";

vi.mock("../lib/apiFetch", () => ({ apiJson: vi.fn(async () => ({ ok: true, rollout: { run_id: "r" } })) }));
const call = apiJson as unknown as ReturnType<typeof vi.fn>;
afterEach(() => call.mockClear());

describe("rolloutService", () => {
  it("speaks the /api/p2p/rollouts contract", async () => {
    await listRollouts();
    expect(call).toHaveBeenLastCalledWith("/api/p2p/rollouts");
    await getRollout("a/b");
    expect(call).toHaveBeenLastCalledWith("/api/p2p/rollouts/a%2Fb");
    await planRollout("img.sqsh", ["Q5", "Q23"]);
    expect(call).toHaveBeenLastCalledWith("/api/p2p/rollouts/plan?image=img.sqsh&racks=Q5%2CQ23");
    await startRollout("img.sqsh", ["Q5"], { max_active_racks: 2 });
    expect(call.mock.lastCall?.[1]).toMatchObject({ method: "POST" });
    expect(JSON.parse(call.mock.lastCall?.[1].body)).toEqual({ image: "img.sqsh", racks: ["Q5"], pacing: { max_active_racks: 2 } });
    await cancelRollout("r");
    expect(call.mock.lastCall?.[0]).toBe("/api/p2p/rollouts/r/cancel");
    await retryRollout("r", ["n1"]);
    expect(JSON.parse(call.mock.lastCall?.[1].body)).toEqual({ nodes: ["n1"] });
    await setRolloutPacing("r", { budget_seconds: 600 });
    expect(call.mock.lastCall?.[1]).toMatchObject({ method: "PUT" });
  });
});
