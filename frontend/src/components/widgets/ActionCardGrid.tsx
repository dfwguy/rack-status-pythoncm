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
// ActionCardGrid — a reflowing grid of action cards, for the views that do not need dragging.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// The same CSS grid the widget board reads with (`Grid colMinWidth`, so the browser sizes the
// columns), minus react-grid-layout, because these cards are never rearranged. That is the
// whole difference between a rack control and a board widget at the layout level.
//
// It carries the orientation in context rather than making every card take a prop. A card's
// shape is a fact about the container it landed in, not about the action — twenty-two call
// sites threading the same value through would be twenty-two chances to forget one, and a
// single card in the wrong orientation is a visibly broken row.
import type { ReactNode } from "react";
import { Grid } from "@nvidia/foundations-react-core";
import { CardOrientationContext, useCardOrientation } from "./useCardOrientation";
import { WIDGET_CARD_WIDTH } from "./widgetLayout";

export function ActionCardGrid({ children }: { children: ReactNode }) {
  const { ref, orientation } = useCardOrientation();
  return (
    // The measured element is the outer box, not the Grid: measuring the Grid would measure
    // something whose own width this decision can change, and a layout that measures its own
    // output is how you get a flip-flop on a window drag.
    <div ref={ref} style={{ width: "100%" }}>
      <CardOrientationContext.Provider value={orientation}>
        <Grid colMinWidth={WIDGET_CARD_WIDTH} gap="density-lg">
          {children}
        </Grid>
      </CardOrientationContext.Provider>
    </div>
  );
}
