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
import { Accordion, Button, Flex, Panel, Stack, Text } from "@nvidia/foundations-react-core";
import type { FailureDiagnosis } from "./failureDiagnosis";

export function FailureDetails({ diagnosis, showNodes, showArtifacts }: {
  diagnosis: FailureDiagnosis;
  showNodes: () => void;
  showArtifacts: () => void;
}) {
  return (
    <Panel density="compact" className="ck-failure" role="region" aria-label="Failure explanation"
      slotHeading={diagnosis.title}>
      <Stack gap="3">
        <Flex gap="4" wrap="wrap">
          <Text kind="body/regular/md"><strong>Stage:</strong> {diagnosis.stage}
            {diagnosis.exitStatus != null ? ` · exit ${diagnosis.exitStatus}` : ""}</Text>
          <Text kind="body/regular/md"><strong>Local preflight:</strong> {diagnosis.preflight}</Text>
        </Flex>
        <Text kind="body/regular/md" className="ck-failure-nodes">
          <strong>{diagnosis.nodeLabel}:</strong>{" "}
          {diagnosis.nodes.length ? diagnosis.nodes.map((node, index) => (
            <span key={node}>{index ? ", " : ""}<span className="ck-mono">{node}</span></span>
          )) : "No node identified in the verified evidence"}
        </Text>
        <Text kind="body/regular/md">{diagnosis.explanation}</Text>
        {diagnosis.coverage ? <Text kind="body/regular/md">Coverage: {diagnosis.coverage}.
          {diagnosis.missingMeasurements ? " Missing measurements are uncollected pairs, not confirmed failed links." : ""}
        </Text> : null}
        <Accordion items={[{
          value: "evidence",
          slotTrigger: "Evidence and next step",
          slotContent: <Stack gap="3">
            {diagnosis.evidence.length ? diagnosis.evidence.map((entry, index) => (
              <Stack gap="2" key={index}>
                <Text kind="body/regular/md">{entry.artifact}{entry.line ? ` · line ${entry.line}` : ""}</Text>
                <pre className="ck-code ck-failure-evidence">{entry.excerpt}</pre>
              </Stack>
            )) : <Text kind="body/regular/md">No normalized log excerpt is available. Raw artifacts may contain more detail.</Text>}
            <Text kind="body/regular/md">{diagnosis.nextStep}</Text>
          </Stack>,
        }]} />
        <Flex gap="2" wrap="wrap">
          <Button kind="secondary" color="neutral" size="small" onClick={showNodes}>Show node diagnostics</Button>
          <Button kind="secondary" color="neutral" size="small" onClick={showArtifacts}>View artifacts</Button>
        </Flex>
      </Stack>
    </Panel>
  );
}
