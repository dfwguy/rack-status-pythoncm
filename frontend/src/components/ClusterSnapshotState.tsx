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
import type { ClusterSnapshotMeta } from "../types/api";

interface ClusterSnapshotStateProps {
  snapshot: ClusterSnapshotMeta;
  onRetry?: () => void;
}

function ageLabel(ageSeconds: number | null): string {
  if (ageSeconds === null) return "unknown age";
  if (ageSeconds < 60) return `${Math.round(ageSeconds)}s old`;
  if (ageSeconds < 3600) return `${Math.round(ageSeconds / 60)}m old`;
  return `${Math.round(ageSeconds / 3600)}h old`;
}

export function ClusterSnapshotState({ snapshot, onRetry }: ClusterSnapshotStateProps) {
  if (snapshot.state === "fresh") return null;

  if (snapshot.state === "initializing") {
    return <Banner status="info">Loading cluster inventory…</Banner>;
  }

  if (snapshot.state === "failed") {
    return (
      <Banner status="error">
        <Flex direction="column" gap="2" align="start">
          <Text kind="body/bold/md">Cluster inventory unavailable</Text>
          {snapshot.last_error && <Text kind="body/regular/sm">{snapshot.last_error}</Text>}
          {snapshot.last_attempt_at && (
            <Text kind="body/regular/sm">Last attempt: {snapshot.last_attempt_at}</Text>
          )}
          {onRetry && (
            <Button kind="secondary" size="small" onClick={onRetry}>
              Retry inventory
            </Button>
          )}
        </Flex>
      </Banner>
    );
  }

  return (
    <Banner status="warning">
      Showing stale cluster inventory ({ageLabel(snapshot.age_seconds)}).
      {snapshot.last_error ? ` Last refresh failed: ${snapshot.last_error}` : ""}
    </Banner>
  );
}
