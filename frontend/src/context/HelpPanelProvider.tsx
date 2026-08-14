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
