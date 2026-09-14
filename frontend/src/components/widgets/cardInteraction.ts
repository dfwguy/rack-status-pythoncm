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
// cardInteraction — the rule that makes a clickable card safe.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Its own module, not a member of `ActionCardFace`: a file exporting a component may not
// also export a function (`react-refresh/only-export-components`, which Vite needs to hot-
// replace the component). Three card components import this, so it was never private anyway.
/**
 * Run a handler without letting the click reach the card.
 *
 * The card itself is clickable (see `onDetails`), so every control drawn on top of it has to
 * stop the bubble or pressing Run would also open the modal behind it.
 */
export function withoutBubbling(fn: () => void) {
  return (e: { stopPropagation: () => void }) => {
    e.stopPropagation();
    fn();
  };
}
