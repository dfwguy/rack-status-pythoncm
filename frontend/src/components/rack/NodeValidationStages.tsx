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
// A node's validation stages as Rack Status records them (GET /api/node-stages?node=), for the node
// page. That page showed only the bring-up automation's own results, so a node whose Rack Status
// Stage 1 NCCL run had passed (gpu63-su20, Slurm 207363) read as failed there: the only allreduce in
// view was bring-up's, which had failed on its own. Stage 1 passes on Rack Status's run or on the
// bring-up run of the same check (RACK_NODE_STAGE1_BRINGUP_JOBS), and says which; Stage 2 comes from
// the source the server names.
import { useEffect, useState } from "react";
import { Banner, Card, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import type { NodeStageInfo, NodeStagesRow } from "../../types/api";
import { fetchNodeStages } from "../../services/nodeStagesService";
import { stageSourceLabel, stageStateColor, stageStateLabel } from "../../lib/validationStages";

function StageLine({ title, info, detail }: { title: string; info?: NodeStageInfo; detail: string }) {
  const state = info?.state ?? "pending";
  const known = state !== "unknown";
  return (
    <Flex gap="3" align="center" wrap="wrap">
      <Text kind="body/bold/sm" style={{ minWidth: 220 }}>{title}</Text>
      <Tag color={known ? stageStateColor(state) : "gray"} kind="outline" readOnly density="compact">
        {known ? stageStateLabel(state) : "loading"}
      </Tag>
      {detail ? (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>{detail}</Text>
      ) : null}
    </Flex>
  );
}

export function NodeValidationStages({ node, logDir }: { node: string; logDir?: string }) {
  const [row, setRow] = useState<NodeStagesRow | undefined>();
  const [source, setSource] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    fetchNodeStages({ node, logDir })
      .then((res) => {
        if (!live) return;
        setRow(res.nodes.find((n) => n.node === node) ?? res.nodes[0]);
        setSource(res.stage2_source || "");
        setError("");
      })
      .catch((reason: unknown) => {
        if (live) setError(reason instanceof Error ? reason.message : "Unable to load the node's stages");
      });
    return () => {
      live = false;
    };
  }, [node, logDir]);

  const s1 = row?.stage1;
  const s2 = row?.stage2;
  const s1Via = stageSourceLabel(s1?.source);
  const s1Detail = [s1Via ? `via ${s1Via}` : "", s1?.job_id ? `Slurm ${s1.job_id}` : "", s1?.log ?? "", s1?.reason ?? ""]
    .filter(Boolean)
    .join(" · ");
  const s2Detail = [s2?.status ?? "", s2?.reason ?? ""].filter(Boolean).join(" · ");

  return (
    <Card style={{ padding: 16, marginBottom: 16 }}>
      <Text kind="title/sm" style={{ display: "block" }}>Validation stages</Text>
      <Text kind="body/regular/sm" style={{ display: "block", color: "var(--text-color-secondary)", marginBottom: 12 }}>
        The record Rack Status keeps for this node. Stage 1 passes when Rack Status&rsquo;s single-node NCCL
        run or the bring-up automation&rsquo;s run of the same check passed; it says which. The bring-up
        results below are the bring-up automation&rsquo;s own jobs.
      </Text>
      {error ? (
        <Banner status="error" kind="inline">{error}</Banner>
      ) : (
        <Flex direction="col" gap="2">
          <StageLine title="Stage 1 — single-node NCCL" info={s1} detail={s1Detail} />
          <StageLine title={`Stage 2 — single-node LLM${source ? ` (${source})` : ""}`} info={s2} detail={s2Detail} />
        </Flex>
      )}
    </Card>
  );
}
