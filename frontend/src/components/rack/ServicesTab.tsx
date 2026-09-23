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
import { ActionCard } from "./ActionCard";
import { ActionCardGrid } from "../widgets/ActionCardGrid";
import { RefreshCw, Info, FileText } from "lucide-react";
import { useRackCommands } from "../../context/RackCommandContext";
import { specsCommandBody, validationFamilyBody } from "../../lib/rackCommandBodies";
import type { NvisNamingSchema } from "../../types/naming";

interface ServicesTabProps {
  rack: string;
  logDir?: string;
  naming?: NvisNamingSchema;
  selectedNodes: Set<string>;
  disabled: boolean;
}

export function ServicesTab({ rack, logDir, naming, selectedNodes, disabled }: ServicesTabProps) {
  const { runAsyncReset, openStream, openTailLogNemo } = useRackCommands();
  const nodeCount = selectedNodes.size;

  return (
    <ActionCardGrid>
      <ActionCard
        title="Restart Services"
        category="maintenance"
        description={`Restart services on ${nodeCount} selected node(s): mount -a, munge, slurmd, nvidia-imex, nvidia-dcgm.`}
        icon={<RefreshCw size={20} />}
        buttonLabel="Restart"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          runAsyncReset("Restart services", "services", "/api/services-restart-async", specsCommandBody(rack, logDir, selectedNodes, naming))
        }
      />
      <ActionCard
        title="System Info"
        category="hardware"
        description={`Stream live system info from ${nodeCount} selected node(s). GPU status, driver, fabric manager.`}
        icon={<Info size={20} />}
        buttonLabel="View Sysinfo"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          openStream(
            "System info (stream)",
            "/api/run-sysinfo-stream",
            validationFamilyBody(rack, logDir, selectedNodes, naming) as Record<string, unknown>,
          )
        }
      />
      <ActionCard
        title="Tail Log"
        category="maintenance"
        description={rack ? `Live tail the latest NEMO validation log for ${rack}. Streams output in real time.` : "Requires a physical rack target."}
        icon={<FileText size={20} />}
        buttonLabel="Tail Log"
        disabled={!rack}
        onRun={() => openTailLogNemo()}
      />
    </ActionCardGrid>
  );
}
