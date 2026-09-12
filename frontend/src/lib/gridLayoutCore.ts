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
// gridLayoutCore — placement facts shared by every react-grid-layout surface in the app.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// The terminal workspace and the tool-widget board are different products with different
// placement rules, but they mount the same library and so inherit the same echo problem
// below. One implementation, imported by both.
import type { LayoutItem } from "react-grid-layout";

/**
 * Whether two layouts describe the same arrangement.
 *
 * react-grid-layout emits onLayoutChange when it mounts, and again whenever it re-normalises,
 * handing back a layout that is frequently identical to the one it was given. A consumer that
 * stores and persists what it receives would therefore write to localStorage on every mount --
 * and, because each callback carries a fresh object, stand one dependency array away from a
 * render loop.
 */
export function sameLayout(a: readonly LayoutItem[], b: readonly LayoutItem[]): boolean {
  if (a.length !== b.length) return false;
  const byId = new Map(a.map((l) => [l.i, l]));
  return b.every((item) => {
    const prev = byId.get(item.i);
    return (
      !!prev && prev.x === item.x && prev.y === item.y && prev.w === item.w && prev.h === item.h
    );
  });
}
