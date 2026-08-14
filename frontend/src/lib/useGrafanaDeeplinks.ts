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
// useGrafanaDeeplinks.ts — fetch the Grafana deep-link config once and share it.
// Author: Rafael M. Koike & Cursor Agent ("Ada")

import { useEffect, useState } from "react";
import { fetchGrafanaDeeplinkConfig, type GrafanaDeeplinkConfig } from "./grafanaLinks";

let _cache: GrafanaDeeplinkConfig | null = null;
let _inflight: Promise<GrafanaDeeplinkConfig> | null = null;

/** React hook returning the deep-link config (module-cached; fetched at most once per load). */
export function useGrafanaDeeplinks(): GrafanaDeeplinkConfig | null {
  const [cfg, setCfg] = useState<GrafanaDeeplinkConfig | null>(_cache);

  useEffect(() => {
    // Initial state already reflects the module cache; only fetch when it's empty.
    if (_cache) return;
    let alive = true;
    if (!_inflight) _inflight = fetchGrafanaDeeplinkConfig();
    _inflight
      .then((c) => {
        _cache = c;
        if (alive) setCfg(c);
      })
      .catch(() => {
        if (alive) setCfg(null);
      })
      .finally(() => {
        _inflight = null;
      });
    return () => {
      alive = false;
    };
  }, []);

  return cfg;
}
