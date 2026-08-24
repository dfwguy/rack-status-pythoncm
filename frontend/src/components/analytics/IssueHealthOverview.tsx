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
import { useCallback, useEffect, useState } from "react";
import Box from "@mui/material/Box";
import { Banner, Button, Card, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import { RefreshCw } from "lucide-react";
import { fetchPostRunNodeReport } from "../../services/nodeIssuesService";
import type { PostRunNodeReportResponse } from "../../types/api";

export function IssueHealthOverview() {
  const [data, setData] = useState<PostRunNodeReportResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    try {
      setData(await fetchPostRunNodeReport({ refresh }));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  const s = data?.summary;
  const nodesWithIssues = data?.nodes.filter((node) => node.failure_count > 0).length ?? 0;
  const hardwareFindings = data?.nodes.reduce(
    (total, node) => total + node.findings.filter((finding) => finding.category === "hardware").length,
    0,
  ) ?? 0;
  const networkFindings = data?.nodes.reduce(
    (total, node) => total + node.findings.filter((finding) => finding.category === "network").length,
    0,
  ) ?? 0;
  return (
    <Card style={{ padding: 18, marginBottom: 16 }}>
      <Flex align="center" gap="2" wrap="wrap" style={{ marginBottom: 10 }}>
        <Text kind="title/sm" style={{ flex: 1 }}>Overall node issue health</Text>
        {data?.generated_at && (
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            Validation history refreshed {new Date(data.generated_at).toLocaleString()}
          </Text>
        )}
        <Button kind="secondary" size="small" disabled={loading} onClick={() => void load(true)}>
          <RefreshCw size={14} /> Refresh
        </Button>
      </Flex>
      {error && <Banner status="error" kind="inline">{error}</Banner>}
      {data?.refresh?.accounting_error && <Banner status="warning" kind="inline">Scheduler history refresh failed; showing retained node participation: {data.refresh.accounting_error}</Banner>}
      {s && (
        <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 1.25 }}>
          {[
            ["Validation participations", s.participations, "blue"],
            ["Nodes with issues", nodesWithIssues, "yellow"],
            ["Attributed failures", s.attributed_failures, "yellow"],
            ["Repeat nodes", s.flagged_nodes, "red"],
            ["Currently drained", s.drained_nodes, "red"],
            ["Hardware findings", hardwareFindings, "blue"],
            ["Network findings", networkFindings, "purple"],
          ].map(([label, value, color]) => (
            <Box key={String(label)} sx={{ p: 1.25, border: "1px solid", borderColor: "divider", borderRadius: 1 }}>
              <Text kind="body/regular/sm" style={{ display: "block", color: "var(--text-color-secondary)" }}>{String(label)}</Text>
              <Flex align="center" gap="2">
                <Text kind="title/md">{Number(value)}</Text>
                <Tag color={color as "yellow" | "red" | "blue" | "purple"} kind="outline" readOnly density="compact">
                  {String(label)}
                </Tag>
              </Flex>
            </Box>
          ))}
        </Box>
      )}
    </Card>
  );
}
