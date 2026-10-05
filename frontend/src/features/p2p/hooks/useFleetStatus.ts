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
// Polling access to the fleet snapshot. Own the interval here rather than in a
// view, so two panes can never poll at different rates and disagree about what
// is running.
import { useCallback, useEffect, useRef, useState } from "react";
import { getFleetStatus } from "../services/fleetService";
import { ApiError } from "../lib/apiFetch";
import type { FleetStatus } from "../types/fleet";

/** Matches the cadence the fleet controller writes its run files at. */
const POLL_MS = 15_000;

export type FleetStatusState = {
  status: FleetStatus | null;
  error: string | null;
  /** True only for the first load, so a refresh does not blank the page. */
  loading: boolean;
  /** Set when the session is gone, which needs a sign-in rather than a retry. */
  unauthenticated: boolean;
  refresh: () => Promise<void>;
};

export function useFleetStatus(): FleetStatusState {
  const [status, setStatus] = useState<FleetStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [unauthenticated, setUnauthenticated] = useState(false);
  // Avoids a setState on an unmounted view when a poll lands during teardown.
  const live = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const next = await getFleetStatus();
      if (!live.current) return;
      setStatus(next);
      setError(null);
      setUnauthenticated(false);
    } catch (reason) {
      if (!live.current) return;
      if (reason instanceof ApiError && reason.isUnauthenticated) {
        setUnauthenticated(true);
        setError("Session expired or the root role is required.");
      } else {
        setError(reason instanceof Error ? reason.message : "Unable to load fleet status");
      }
    } finally {
      if (live.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    live.current = true;
    // The first load is scheduled rather than called straight from the effect
    // body. Both paths then apply state from a timer callback, which is the
    // shape `react-hooks/set-state-in-effect` asks for -- a synchronous call
    // here is reachable-setState and triggers cascading renders. Rack Status
    // carries this rule at `warn` because fixing it there means reworking data
    // loading in ~30 views; this console is new, so it keeps every rule at
    // error instead of inheriting that debt.
    const initial = window.setTimeout(() => void refresh(), 0);
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => {
      live.current = false;
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [refresh]);

  return { status, error, loading, unauthenticated, refresh };
}
