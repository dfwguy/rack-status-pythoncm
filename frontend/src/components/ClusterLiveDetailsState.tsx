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
import type { ClusterOverviewLiveResponse } from "../types/api";

interface ClusterLiveDetailsStateProps {
  liveData: ClusterOverviewLiveResponse | null;
  liveError: string | null;
  onLiveRetry?: () => void;
}

export function ClusterLiveDetailsState({
  liveData,
  liveError,
  onLiveRetry,
}: ClusterLiveDetailsStateProps) {
  const sections = liveData?.sections;
  const hasStale = sections
    ? Object.values(sections).some((section) => section.state === "stale")
    : false;
  const hasFailed = sections
    ? Object.values(sections).some((section) => section.state === "failed")
    : false;
  const errors = sections
    ? Object.values(sections)
        .map((section) => section.last_error)
        .filter(Boolean)
    : [];

  if (hasFailed || liveError) {
    return (
      <Banner status="error">
        <Flex direction="column" gap="2" align="start">
          <Text kind="body/bold/md">Live cluster details unavailable</Text>
          {liveError && <Text kind="body/regular/sm">{liveError}</Text>}
          {errors.map((message) => (
            <Text key={message} kind="body/regular/sm">
              {message}
            </Text>
          ))}
          {onLiveRetry && (
            <Button kind="secondary" size="small" onClick={onLiveRetry}>
              Retry live details
            </Button>
          )}
        </Flex>
      </Banner>
    );
  }

  if (hasStale) {
    return (
      <Banner status="warning">
        Live cluster details may be stale.
        {errors.length > 0 ? ` Last refresh issue: ${errors.join("; ")}` : ""}
      </Banner>
    );
  }

  if (liveData && !liveData.reachable && liveData.unreachable_detail) {
    return (
      <Banner status="warning">
        Cluster unreachable — showing cached live details when available.{" "}
        {liveData.unreachable_detail}
      </Banner>
    );
  }

  return null;
}
