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
// GrafanaLinksMenu.tsx — per-rack / per-job "Open in Grafana" deep-link menu.
// Author: Rafael M. Koike & Cursor Agent ("Ada")

import { useState, type MouseEvent } from "react";
import IconButton from "@mui/material/IconButton";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import ListItemIcon from "@mui/material/ListItemIcon";
import ListItemText from "@mui/material/ListItemText";
import Tooltip from "@mui/material/Tooltip";
import Divider from "@mui/material/Divider";
import Typography from "@mui/material/Typography";
import { Gauge, ExternalLink } from "lucide-react";
import { buildGrafanaDeepLink } from "../lib/grafanaLinks";
import { useGrafanaDeeplinks } from "../lib/useGrafanaDeeplinks";

export interface GrafanaLinksMenuProps {
  rack?: string;
  /** Node/host for host-scoped dashboards (optional). */
  host?: string;
  /** Optional time window (epoch ms) — e.g. a job's start/end. Omit for a live window. */
  fromMs?: number;
  toMs?: number;
  /** MUI IconButton size. */
  size?: "small" | "medium";
  disabled?: boolean;
}

/**
 * Icon button that opens a menu of "Open in Grafana" deep-links. Each entry launches a full
 * BCM/NMC dashboard in a new tab, scoped to this rack (and node, for host-scoped dashboards)
 * plus the supplied time window. Renders nothing when deep-links are disabled or unconfigured.
 */
export function GrafanaLinksMenu({ rack, host, fromMs, toMs, size = "small", disabled }: GrafanaLinksMenuProps) {
  const cfg = useGrafanaDeeplinks();
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);

  if (!cfg || !cfg.enabled || cfg.dashboards.length === 0) return null;

  const open = (e: MouseEvent<HTMLElement>) => {
    e.stopPropagation();
    setAnchor(e.currentTarget);
  };
  const close = () => setAnchor(null);
  const go = (uid: string) => {
    const dash = cfg.dashboards.find((d) => d.uid === uid);
    if (!dash) return;
    const url = buildGrafanaDeepLink(cfg, dash, { rack, host, fromMs, toMs });
    window.open(url, "_blank", "noopener,noreferrer");
    close();
  };

  const scopeCaption = host
    ? `Scoped to ${host}`
    : rack
      ? `Scoped to rack ${rack}`
      : "Cluster-wide";

  return (
    <>
      <Tooltip title="Open in Grafana">
        <span>
          <IconButton
            size={size}
            disabled={disabled}
            onClick={open}
            aria-label="Open in Grafana"
            sx={{ flexShrink: 0 }}
          >
            <Gauge size={size === "small" ? 18 : 20} />
          </IconButton>
        </span>
      </Tooltip>
      <Menu
        anchorEl={anchor}
        open={!!anchor}
        onClose={close}
        transformOrigin={{ horizontal: "right", vertical: "top" }}
        anchorOrigin={{ horizontal: "right", vertical: "bottom" }}
      >
        <Typography variant="caption" sx={{ px: 2, py: 0.5, display: "block", color: "text.secondary" }}>
          {scopeCaption}
        </Typography>
        <Divider />
        {cfg.dashboards.map((d) => (
          <MenuItem key={d.uid} onClick={() => go(d.uid)}>
            <ListItemIcon>
              <ExternalLink size={16} />
            </ListItemIcon>
            <ListItemText primary={d.label} secondary={d.desc || undefined} />
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}
