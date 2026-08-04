/** Provider for the shared rack-locks poller; the hook lives in RackLocksContext.ts. */
import type { ReactNode } from "react";
import { useRackLocks } from "../hooks/useRackLocks";
import { RackLocksContext } from "./RackLocksContext";

export function RackLocksProvider({ children }: { children: ReactNode }) {
  const value = useRackLocks();
  return <RackLocksContext.Provider value={value}>{children}</RackLocksContext.Provider>;
}
