import { useState, useCallback, type ReactNode } from "react";
import { HelpPanelCtx } from "./HelpPanelContext";

/** Hosts the Help slide-out panel state; render once near the app root. */
export function HelpPanelProvider({ children }: { children: ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const openHelp = useCallback((id?: string) => {
    setSelectedId(id ?? null);
    setIsOpen(true);
  }, []);

  const closeHelp = useCallback(() => setIsOpen(false), []);
  const selectTopic = useCallback((id: string | null) => setSelectedId(id), []);

  return (
    <HelpPanelCtx.Provider value={{ isOpen, selectedId, openHelp, closeHelp, selectTopic }}>
      {children}
    </HelpPanelCtx.Provider>
  );
}

export default HelpPanelProvider;
