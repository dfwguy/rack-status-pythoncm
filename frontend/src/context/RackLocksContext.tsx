/**
 * Single-instance ``useRackLocks`` poller exposed via React context so every consumer
 * (table icons, banner, conflict dialog interceptor) shares the same 5 s snapshot.
 */
import { createContext, useContext, type ReactNode } from "react";
import { useRackLocks, type UseRackLocksResult } from "../hooks/useRackLocks";

const RackLocksContext = createContext<UseRackLocksResult | null>(null);

export function useRackLocksContext(): UseRackLocksResult {
  const v = useContext(RackLocksContext);
  if (!v) {
    throw new Error("useRackLocksContext must be used under RackLocksProvider");
  }
  return v;
}

export function RackLocksProvider({ children }: { children: ReactNode }) {
  const value = useRackLocks();
  return <RackLocksContext.Provider value={value}>{children}</RackLocksContext.Provider>;
}
