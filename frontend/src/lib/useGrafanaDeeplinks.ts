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
