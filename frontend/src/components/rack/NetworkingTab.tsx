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
import { ActionCard } from "./ActionCard";
import { Network, RotateCcw, ShieldCheck, ScanSearch, Map, Radio } from "lucide-react";
import { useRackCommands } from "../../context/RackCommandContext";
import { attachLogDir } from "../../lib/rackCommandBodies";
import { DeepAuditCard } from "./DeepAuditCard";
import type { NvisNamingSchema } from "../../types/naming";

interface NetworkingTabProps {
  rack: string;
  logDir?: string;
  selectedNodes: Set<string>;
  disabled: boolean;
  naming?: NvisNamingSchema;
}

export function NetworkingTab({ rack, logDir, selectedNodes, disabled, naming }: NetworkingTabProps) {
  const { runAsyncReset, openStream, openText } = useRackCommands();

  const rackLogBody = () => attachLogDir({ rack }, logDir);

  const runSystemReset = async () => {
    const res = await fetch(`/api/nvswitch-hosts?rack=${encodeURIComponent(rack)}`, { credentials: "same-origin" });
    let data: { ok?: boolean; hosts?: string[]; error?: string } = {};
    try {
      data = (await res.json()) as typeof data;
    } catch {
      openText("NVSwitch system reset", "Invalid JSON from nvswitch-hosts.", true);
      return;
    }
    if (!res.ok || !data.ok || !data.hosts?.length) {
      openText("NVSwitch system reset", data.error || "No NVSwitch hosts returned for this rack.", true);
      return;
    }
    const nvswitch_host = data.hosts[0];
    await runAsyncReset("NVSwitch system reset", "nmxc", "/api/nvswitch-system-reset-async", {
      ...rackLogBody(),
      nvswitch_host,
    });
  };

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <DeepAuditCard
        rack={rack}
        selectedNodes={selectedNodes}
        naming={naming}
        disabled={disabled}
        kind="network"
        title="NVLink / Network Deep Audit"
        description={`Deep NVLink fabric and forced GPU data-path test plus IMEX, IB rail/rate/error counters and mlx5 bond checks on ${selectedNodes.size} selected node(s). Confirmed failures are commented and drained through the active scheduler automatically.`}
        icon={ShieldCheck}
      />
      <ActionCard
        title="NVSwitch Fix"
        description={`Run NVSwitch recovery (NMXC cycle) for ${rack}.`}
        icon={<Network size={20} />}
        buttonLabel="Fix"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runAsyncReset("NVSwitch fix", "nmxc", "/api/nvswitch-fix-async", rackLogBody())}
      />
      <ActionCard
        title="NVSwitch SDN Reset"
        description={`Reset NVSwitch SDN controller for ${rack}.`}
        icon={<RotateCcw size={20} />}
        buttonLabel="SDN Reset"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runAsyncReset("NVSwitch SDN reset", "nmxc", "/api/nvswitch-sdn-reset-async", rackLogBody())}
      />
      <ActionCard
        title="NVSwitch System Reset"
        description={`Full NVLink system reset for ${rack} (first NVSwitch host from inventory).`}
        icon={<Radio size={20} />}
        buttonLabel="System Reset"
        destructive
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={runSystemReset}
      />
      <ActionCard
        title="NVSwitch Audit"
        description={`Stream live NVSwitch audit for ${rack} (check-only).`}
        icon={<ShieldCheck size={20} />}
        buttonLabel="Audit"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          openStream("NVSwitch audit (stream)", "/api/run-nvswitch-audit-stream", { ...rackLogBody(), fix: false })
        }
      />
      <ActionCard
        title="NVSwitch Check"
        description={`Async NVSwitch health check for ${rack}.`}
        icon={<ScanSearch size={20} />}
        buttonLabel="Check"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runAsyncReset("NVSwitch check", "nmxc", "/api/nvswitch-check-async", rackLogBody())}
      />
      <ActionCard
        title="NVLMapper"
        description={`Run NVLMapper topology analysis on ${rack}.`}
        icon={<Map size={20} />}
        buttonLabel="Run NVLMapper"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runAsyncReset("NVLMapper", "nmxc", "/api/nvlmapper-async", { rack })}
      />
    </Box>
  );
}
