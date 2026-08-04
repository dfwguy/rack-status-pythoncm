/**
 * Single-instance ``useRackLocks`` poller exposed via React context so every consumer
 * (table icons, banner, conflict dialog interceptor) shares the same 5 s snapshot.
 *
 * Context object and hook only — the provider component lives in RackLocksProvider.tsx so that
 * React Refresh can hot-swap it. See ThemeContext.ts for the reasoning.
 */
import { createContext, useContext } from "react";
import type { UseRackLocksResult } from "../hooks/useRackLocks";

export const RackLocksContext = createContext<UseRackLocksResult | null>(null);

export function useRackLocksContext(): UseRackLocksResult {
  const v = useContext(RackLocksContext);
  if (!v) {
    throw new Error("useRackLocksContext must be used under RackLocksProvider");
  }
  return v;
}
