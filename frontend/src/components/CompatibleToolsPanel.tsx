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
import { useEffect, useMemo, useState } from "react";
import { Banner, Card, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import { useActiveCluster } from "../context/ActiveClusterContext";
import { listTools } from "../services/catalogService";
import type { Tool } from "../types/catalog";

/**
 * Read-only panel listing the catalog tools compatible with the active cluster's
 * platform. Surfaces the registry in the operational UI so operators see exactly
 * which scripts apply to the current generation (e.g. NVSwitch/IMEX/MNNVL tools
 * disappear on HGX x86 platforms). Tools run with platform-aware parameters.
 */
export function CompatibleToolsPanel() {
  const { activeCluster, activePlatform, version } = useActiveCluster();
  const [tools, setTools] = useState<Tool[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!activeCluster?.id) {
      setTools([]);
      return;
    }
    setLoading(true);
    listTools(activeCluster.id)
      .then((items) => {
        if (!cancelled) {
          setTools(items);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load tools");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeCluster?.id, version]);

  const grouped = useMemo(() => {
    const m = new Map<string, Tool[]>();
    for (const t of tools) {
      const list = m.get(t.category) || [];
      list.push(t);
      m.set(t.category, list);
    }
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [tools]);

  if (!activeCluster) return null;

  return (
    <Card style={{ padding: 16, marginBottom: 16 }}>
      <Flex justify="between" align="center" wrap="wrap" gap="2">
        <Text kind="body/bold/md">
          Tools for {activePlatform?.name || activeCluster.name}
        </Text>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          Filtered to the active platform's capabilities · run with platform-aware parameters
        </Text>
      </Flex>

      {error && (
        <Banner status="error" kind="inline" style={{ marginTop: 12 }}>
          {error}
        </Banner>
      )}
      {loading && tools.length === 0 && (
        <Text kind="body/regular/sm" style={{ display: "block", marginTop: 8 }}>Loading…</Text>
      )}

      {grouped.map(([category, items]) => (
        <div key={category} style={{ marginTop: 12 }}>
          <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 6, textTransform: "capitalize" }}>
            {category}
          </Text>
          <Flex gap="2" wrap="wrap">
            {items.map((t) => (
              <Tag key={t.id} color="teal" kind="outline" readOnly density="compact" title={t.description}>
                {t.name}
              </Tag>
            ))}
          </Flex>
        </div>
      ))}
    </Card>
  );
}
