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
// GreenlightMark.tsx - the NVIS Greenlight product mark.
// Author: Rafael M. Koike & Cursor Agent ("Ada")

/**
 * The product mark: a rack cabinet in silhouette that is also a signal head,
 * cleared on the bottom lamp.
 *
 * Inlined rather than loaded from `public/brand/` so the cabinet and unlit lamps
 * can inherit `currentColor` and track the KUI light/dark theme. The browser tab
 * cannot do that — a favicon renders with no CSS context — and this mark is too
 * tall and narrow to fill a 16px square anyway, so `public/favicon.svg` carries
 * the wider single-rack mark instead.
 *
 * The unlit lamps sit at 0.45. Grey on white is much fainter than the same grey
 * on near-black, and at the 0.28 they were first drawn at the mark collapsed to a
 * lone green dot on the light theme while still reading correctly on dark.
 */
export function GreenlightMark({ size = 32 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      xmlns="http://www.w3.org/2000/svg"
      role="img"
      aria-label="NVIS Greenlight"
      style={{ flexShrink: 0 }}
    >
      <rect
        x="6.4"
        y="1.9"
        width="11.2"
        height="20.2"
        rx="2.8"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        opacity="0.75"
      />
      <g fill="currentColor" opacity="0.45">
        <circle cx="12" cy="6.6" r="2.25" />
        <circle cx="12" cy="12" r="2.25" />
      </g>
      <circle cx="12" cy="17.4" r="2.7" fill="#76B900" />
    </svg>
  );
}
