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
 * Polling hook that reads ``GET /api/rack-locks`` every 5 s. Used by both the global
 * ``RackLocksContext`` (single poller for the whole SPA) and ad-hoc consumers in tests.
 *
 * Server-side, locks combine live operations (job dicts + sync handler tracking) with
 * explicit manual claims. This hook is purely observational; the
 * ``RackCommandsStoreContext`` interceptor calls ``isLockedByOther`` before POSTing.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/apiFetch";

export type RackLockKind = "active_job" | "claim";

export interface RackLockEntry {
  operator: string;
  kind: RackLockKind;
  detail?: string;
  job_id?: string;
  since: string;
  expires_at?: string | null;
  sync?: boolean;
  /** When present, ``operator`` took this rack over from this previous owner. */
  stolen_from?: string;
}

export interface RackLocksSnapshot {
  ok: boolean;
  now: string;
  me: string;
  locks: Record<string, RackLockEntry>;
}

export interface UseRackLocksResult {
  locks: Record<string, RackLockEntry>;
  me: string;
  now: string;
  loading: boolean;
  error: string | null;
  /** Returns the lock entry when *rack* is held by someone other than ``me``. */
  isLockedByOther: (rack: string) => RackLockEntry | null;
  /** Any lock on *rack* (including own). */
  getLock: (rack: string) => RackLockEntry | null;
  refresh: () => Promise<void>;
}

const DEFAULT_POLL_MS = 5_000;

function normalizeRack(rack: string): string {
  return (rack || "").trim().toLowerCase();
}

export function useRackLocks(pollMs = DEFAULT_POLL_MS): UseRackLocksResult {
  const [snap, setSnap] = useState<RackLocksSnapshot>({
    ok: true,
    now: "",
    me: "",
    locks: {},
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await apiFetch("/api/rack-locks", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as RackLocksSnapshot;
      setSnap({
        ok: data.ok !== false,
        now: data.now || "",
        me: data.me || "",
        locks: data.locks || {},
      });
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    refresh();
    const tick = () => {
      timerRef.current = setTimeout(async () => {
        await refresh();
        tick();
      }, pollMs);
    };
    tick();
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [refresh, pollMs]);

  const getLock = useCallback(
    (rack: string): RackLockEntry | null => {
      const k = normalizeRack(rack);
      return snap.locks[k] || null;
    },
    [snap.locks],
  );

  const isLockedByOther = useCallback(
    (rack: string): RackLockEntry | null => {
      const entry = getLock(rack);
      if (!entry) return null;
      const me = (snap.me || "").trim();
      const owner = (entry.operator || "").trim();
      if (me && owner && me === owner) return null;
      return entry;
    },
    [getLock, snap.me],
  );

  return {
    locks: snap.locks,
    me: snap.me,
    now: snap.now,
    loading,
    error,
    isLockedByOther,
    getLock,
    refresh,
  };
}
