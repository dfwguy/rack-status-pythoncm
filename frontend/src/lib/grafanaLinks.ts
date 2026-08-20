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
// grafanaLinks.ts — build/open "Open in Grafana" deep-links to full BCM/NMC dashboards.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Deep-links open a full Grafana dashboard in a new browser tab, scoped to a rack (and,
// where the dashboard supports it, a node/host) plus an optional time window. Unlike the
// /d-solo/ parser embeds, this needs no embedding config — it links to dashboards that
// already exist on the head node (default catalog = the stock NMC GB200 dashboards).

import { apiFetch } from "./apiFetch";

export type GrafanaDeeplinkScope = "host" | "rack" | "cluster";

export interface GrafanaDeeplinkDashboard {
  id: string;
  label: string;
  uid: string;
  slug: string;
  scope: GrafanaDeeplinkScope;
  desc?: string;
}

export interface GrafanaDeeplinkConfig {
  enabled: boolean;
  /** Grafana origin (no trailing slash); empty = resolve to same host as the browser. */
  base: string;
  /** Port used only when ``base`` is empty. */
  port: string;
  /** Template variable name for rack scoping (default "Rack"). */
  varRack: string;
  /** Template variable name for host scoping (default "hostname"). */
  varHost: string;
  /** Grafana ``timezone`` query value (utc / browser / IANA). */
  timezone: string;
  dashboards: GrafanaDeeplinkDashboard[];
}

interface RawConfig {
  ok?: boolean;
  enabled?: boolean;
  base?: string;
  port?: string | number;
  var_rack?: string;
  var_host?: string;
  timezone?: string;
  dashboards?: GrafanaDeeplinkDashboard[];
}

function trimSlash(s: string): string {
  return s.replace(/\/+$/, "");
}

/** Resolve the Grafana origin: explicit base, else same host as the browser + port.
 *
 * The backend derives the same origin for CSP's `frame-src` (`security_headers.py`
 * `grafana_frame_origin`). The two must agree or the iframe is blocked by policy — which looks
 * identical to the mixed-content failure below, so a mismatch means debugging one blank panel
 * twice for two unrelated reasons.
 */
export function resolveGrafanaBase(base: string | undefined, port: string | number | undefined): string {
  const explicit = (base != null && String(base).trim()) || "";
  if (explicit) return trimSlash(explicit);
  const portStr = port != null && String(port).trim() ? String(port).trim() : "3000";
  if (typeof window !== "undefined") {
    return trimSlash(`${window.location.protocol}//${window.location.hostname}:${portStr}`);
  }
  return `http://localhost:${portStr}`;
}

/**
 * Why an embedded Grafana panel cannot load from this page, or `null` when it can.
 *
 * Exists because enabling HTTPS on the dashboard breaks these embeds, and it does so *silently*:
 * the browser refuses an `http` iframe inside an `https` page as mixed content, with no request
 * and no error a user can interpret — just an empty panel. Returning a reason lets the UI say so
 * instead of rendering a blank rectangle.
 */
export function grafanaEmbedBlockedReason(base: string): string | null {
  if (typeof window === "undefined") return null;
  if (window.location.protocol !== "https:") return null;
  if (!/^http:\/\//i.test(base.trim())) return null;
  return (
    `This dashboard is served over HTTPS, so the browser blocks the panel at ${base} as mixed ` +
    `content. Serve Grafana over HTTPS too, or set the Grafana base URL (Settings → ` +
    `RACK_WEB_GRAFANA_URI) to its https origin. The "Open in new tab" links below still work.`
  );
}

export interface DeepLinkScopeArgs {
  rack?: string;
  /** Single node/host for ``scope: host`` dashboards (optional). */
  host?: string;
  /** Time window in epoch ms (e.g. a job's start/end). Omit for a live "now-1h" window. */
  fromMs?: number;
  toMs?: number;
}

/**
 * Build a Grafana ``/d/<uid>/<slug>`` URL scoped per the dashboard's scope and the supplied
 * rack/host/time window. Rack/host are sent as ``var-<varRack>`` / ``var-<varHost>`` so they
 * pre-select the dashboard's template variables (best-effort: the value must match a real
 * Prometheus label value, otherwise Grafana falls back to the dashboard default).
 */
export function buildGrafanaDeepLink(
  cfg: GrafanaDeeplinkConfig,
  dash: GrafanaDeeplinkDashboard,
  scope: DeepLinkScopeArgs,
): string {
  const base = resolveGrafanaBase(cfg.base, cfg.port);
  const params: string[] = ["orgId=1"];
  if (dash.scope !== "cluster" && scope.rack) {
    params.push(`var-${encodeURIComponent(cfg.varRack || "Rack")}=${encodeURIComponent(scope.rack)}`);
  }
  if (dash.scope === "host" && scope.host) {
    params.push(`var-${encodeURIComponent(cfg.varHost || "hostname")}=${encodeURIComponent(scope.host)}`);
  }
  if (scope.fromMs != null && scope.toMs != null && scope.toMs > 0) {
    params.push(`from=${Math.floor(scope.fromMs)}`, `to=${Math.floor(scope.toMs)}`);
  } else {
    params.push("from=now-1h", "to=now");
  }
  const tz = (cfg.timezone || "utc").trim() || "utc";
  params.push(`timezone=${encodeURIComponent(tz)}`);
  const uid = encodeURIComponent(dash.uid);
  const slug = encodeURIComponent(dash.slug || "d");
  return `${base}/d/${uid}/${slug}?${params.join("&")}`;
}

/** Fetch the deep-link config from the backend. Returns a disabled config on any failure. */
export async function fetchGrafanaDeeplinkConfig(): Promise<GrafanaDeeplinkConfig> {
  const disabled: GrafanaDeeplinkConfig = {
    enabled: false,
    base: "",
    port: "3000",
    varRack: "Rack",
    varHost: "hostname",
    timezone: "utc",
    dashboards: [],
  };
  try {
    const res = await apiFetch("/api/grafana/deeplinks", { cache: "no-store" });
    if (!res.ok) return disabled;
    const raw = (await res.json()) as RawConfig;
    if (!raw || raw.ok === false) return disabled;
    return {
      enabled: raw.enabled !== false,
      base: (raw.base || "").toString(),
      port: (raw.port != null ? String(raw.port) : "3000") || "3000",
      varRack: (raw.var_rack || "Rack").toString(),
      varHost: (raw.var_host || "hostname").toString(),
      timezone: (raw.timezone || "utc").toString(),
      dashboards: Array.isArray(raw.dashboards) ? raw.dashboards : [],
    };
  } catch {
    return disabled;
  }
}
