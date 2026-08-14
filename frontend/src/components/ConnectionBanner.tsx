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
import { Banner, Button, Flex, Text } from "@nvidia/foundations-react-core";
import { RefreshCw } from "lucide-react";

interface ConnectionBannerProps {
  /** True while the cluster head node is unreachable (e.g. VPN dropped). */
  unreachable: boolean;
  detail?: string | null;
  reconnecting?: boolean;
  onReconnect: () => void;
}

/**
 * Global banner shown when the backend reports the cluster link is down. The app
 * keeps showing the last-known status underneath; this just makes the degraded
 * state explicit and offers a manual reconnect (the app also auto-retries).
 */
export function ConnectionBanner({
  unreachable,
  detail,
  reconnecting = false,
  onReconnect,
}: ConnectionBannerProps) {
  if (!unreachable) return null;
  return (
    <div style={{ marginBottom: 16 }}>
      <Banner status="warning" kind="inline">
        <Flex align="center" justify="between" gap="3" wrap="wrap" style={{ width: "100%" }}>
          <Flex direction="column" gap="1">
            <Text kind="body/bold/sm">Cluster unreachable — showing last known status</Text>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {reconnecting
                ? "Reconnecting…"
                : `The head node is not responding over SSH${detail ? ` (${detail})` : ""}. Reconnect your VPN; the app retries automatically.`}
            </Text>
          </Flex>
          <Button
            size="small"
            kind="secondary"
            onClick={onReconnect}
            disabled={reconnecting}
          >
            <Flex align="center" gap="1">
              <RefreshCw size={14} />
              {reconnecting ? "Reconnecting…" : "Reconnect now"}
            </Flex>
          </Button>
        </Flex>
      </Banner>
    </div>
  );
}
