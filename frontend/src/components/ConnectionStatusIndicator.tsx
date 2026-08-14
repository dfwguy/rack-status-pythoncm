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
import Box from "@mui/material/Box";
import Tooltip from "@mui/material/Tooltip";
import { Cable, Unplug, GlobeX } from "lucide-react";

interface ConnectionStatusIndicatorProps {
  /** UI ↔ Flask backend link (false = app server not responding). */
  backendConnected: boolean;
  /** Backend ↔ cluster head node over SSH (ignored in local mode). */
  clusterReachable: boolean;
  /** "ssh" shows cluster state; "local"/demo only tracks the backend. */
  execMode?: "ssh" | "local";
  /** Epoch ms of the last valid data update, for the tooltip. */
  lastUpdated?: number | null;
  onReconnect?: () => void;
}

type State = "connected" | "cluster-offline" | "disconnected";

const COLORS: Record<State, string> = {
  connected: "#76B900",
  "cluster-offline": "#f2c94c",
  disconnected: "var(--text-color-error, #f44336)",
};

const LABELS: Record<State, string> = {
  connected: "Connected",
  "cluster-offline": "Cluster offline",
  disconnected: "Disconnected",
};

function formatAgo(ts: number): string {
  const sec = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  return `${Math.round(min / 60)}h ago`;
}

export function ConnectionStatusIndicator({
  backendConnected,
  clusterReachable,
  execMode = "ssh",
  lastUpdated = null,
  onReconnect,
}: ConnectionStatusIndicatorProps) {
  const state: State = !backendConnected
    ? "disconnected"
    : execMode === "ssh" && !clusterReachable
      ? "cluster-offline"
      : "connected";

  const color = COLORS[state];
  // Cable = plugged/connected, Unplug = backend disconnected, GlobeX = cluster offline.
  const Icon = state === "disconnected" ? Unplug : state === "cluster-offline" ? GlobeX : Cable;

  const detail =
    state === "disconnected"
      ? "App server is not responding. Check that the backend is running."
      : state === "cluster-offline"
        ? "Backend is up, but the cluster head node is unreachable over SSH (e.g. VPN dropped). Showing last known status."
        : execMode === "ssh"
          ? "Backend and cluster head node are reachable."
          : "Backend is reachable.";

  const updatedLine = lastUpdated ? ` Last update: ${formatAgo(lastUpdated)}.` : "";
  const actionable = state !== "connected" && Boolean(onReconnect);

  return (
    <Tooltip title={`${LABELS[state]} — ${detail}${updatedLine}${actionable ? " Click to retry." : ""}`}>
      <Box
        component={actionable ? "button" : "span"}
        onClick={actionable ? onReconnect : undefined}
        aria-label={`Connection status: ${LABELS[state]}`}
        sx={{
          display: "inline-flex",
          alignItems: "center",
          mr: 0.5,
          px: 0.75,
          py: 0.5,
          border: 0,
          background: "transparent",
          color,
          cursor: actionable ? "pointer" : "default",
          font: "inherit",
        }}
      >
        <Icon size={18} />
      </Box>
    </Tooltip>
  );
}
