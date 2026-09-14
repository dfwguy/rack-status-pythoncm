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
// useCardOrientation — the one thing about a card grid that CSS cannot decide.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Sizing a card grid is the browser's job: `Grid colMinWidth` reflows with the container and
// needs no measurement. But `layout="horizontal" | "vertical"` is a *component prop*, and
// there is no CSS that sets a prop — so the single question "has this collapsed to one
// column?" has to be answered in JavaScript, and this hook is the only place that asks it.
//
// It measures with `getBoundingClientRect().width`, which is always resolved pixels. That is
// deliberate: react-grid-layout's own `useContainerWidth` measures with
// `parseFloat(getComputedStyle(node).width)`, so a container computed as `width: 100%` reports
// **100** — a plausible-looking number that is not a width, which is exactly how a 1440px
// board came to render as a single stacked card. `usableWidth` is still applied as a second
// guard, so an unmeasurable container falls back to the desktop grid rather than to a stack.
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { cardOrientation, usableWidth, type CardOrientation } from "./widgetLayout";

export function useCardOrientation(): {
  ref: React.RefObject<HTMLDivElement | null>;
  orientation: CardOrientation;
} {
  const ref = useRef<HTMLDivElement | null>(null);
  // Vertical until measured: it is the desktop case, and the wrong first paint here is a
  // visible flip of every card on the page.
  const [orientation, setOrientation] = useState<CardOrientation>("vertical");

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const measure = () => {
      const next = cardOrientation(usableWidth(node.getBoundingClientRect().width));
      // Compare before setting: a ResizeObserver fires on every pixel of a window drag, and
      // this decision changes at exactly one of them.
      setOrientation((prev) => (prev === next ? prev : next));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return { ref, orientation };
}

// --- reading it further down the tree ---------------------------------------------------
//
// The context and its reader live here rather than in `ActionCardGrid` because that file
// exports a component, and a component file may not also export a hook or a context without
// breaking Vite's fast refresh (`react-refresh/only-export-components`). Keeping all three
// together — measure it, provide it, read it — is also the clearer home.

/** Vertical outside a grid: a lone card on a page is a grid of one. */
export const CardOrientationContext = createContext<CardOrientation>("vertical");

export function useOrientationFromGrid(): CardOrientation {
  return useContext(CardOrientationContext);
}
