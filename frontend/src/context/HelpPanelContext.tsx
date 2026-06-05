import { createContext, useContext } from "react";

/**
 * Controls the global right-hand Help slide-out panel. Any component can open
 * the panel (optionally to a specific topic id) and close it. The panel itself
 * is rendered once near the app root (see HelpPanel). The provider component
 * lives in HelpPanelProvider.tsx so this module stays hooks/context-only
 * (keeps React Fast Refresh happy).
 */
export interface HelpPanelState {
  isOpen: boolean;
  /** Currently shown topic id, or null to show the topic list. */
  selectedId: string | null;
}

export interface HelpPanelActions {
  /** Opens the panel; pass a content id to jump straight to that topic. */
  openHelp: (id?: string) => void;
  closeHelp: () => void;
  selectTopic: (id: string | null) => void;
}

export type HelpPanelContextType = HelpPanelState & HelpPanelActions;

export const HelpPanelCtx = createContext<HelpPanelContextType | null>(null);

export function useHelpPanel(): HelpPanelContextType {
  const ctx = useContext(HelpPanelCtx);
  if (!ctx) throw new Error("useHelpPanel must be used within HelpPanelProvider");
  return ctx;
}

/** Provider-safe variant: returns null instead of throwing (for isolated renders/tests). */
export function useOptionalHelpPanel(): HelpPanelContextType | null {
  return useContext(HelpPanelCtx);
}
