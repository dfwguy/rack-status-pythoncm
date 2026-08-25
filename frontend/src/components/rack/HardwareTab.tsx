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
import { useState, useMemo } from "react";
import Box from "@mui/material/Box";
import { Card, Flex, Text, Button as KuiButton } from "@nvidia/foundations-react-core";
import { ActionCard } from "./ActionCard";
import { MacChangeDialog } from "./MacChangeDialog";
import { HwCheckRunDialog } from "./HwCheckRunDialog";
import { Power, Cpu, ScanSearch, Bug, RotateCcw, CircleCheck, Zap, Network, ClipboardCheck } from "lucide-react";
import { useRackCommands } from "../../context/RackCommandContext";
import { attachLogDir, bmcRecoveryBody, specsCommandBody, hwCheckOptionsBody } from "../../lib/rackCommandBodies";
import { ValidationPresetDialog } from "./ValidationPresetDialog";
import { DeepAuditCard } from "./DeepAuditCard";
import type { ValidationPresetRequest, ValidationScope } from "../../lib/validationPresets";
import type { NvisNamingSchema } from "../../types/naming";

interface HardwareTabProps {
  rack: string;
  logDir?: string;
  naming?: NvisNamingSchema;
  selectedNodes: Set<string>;
  disabled: boolean;
  validationScope?: ValidationScope;
  /** Pre-select down trays before Power On confirm (legacy power dialog defaults). */
  onSelectPowerResetTargets: () => void;
}

export function HardwareTab({ rack, logDir, naming, selectedNodes, disabled, validationScope = "rack", onSelectPowerResetTargets }: HardwareTabProps) {
  const { runAsyncReset, launchAsyncValidationFamily } = useRackCommands();
  const nodeCount = selectedNodes.size;
  const [macDialogOpen, setMacDialogOpen] = useState(false);
  const [hwCheckDialogOpen, setHwCheckDialogOpen] = useState(false);
  const [sysinfoDialogOpen, setSysinfoDialogOpen] = useState(false);

  const specsBody = () => specsCommandBody(rack, logDir, selectedNodes, naming);
  const imexBody = () => attachLogDir({ rack }, logDir);

  const singleNode = useMemo(() => {
    if (selectedNodes.size !== 1) return null;
    const [only] = [...selectedNodes];
    return only ?? null;
  }, [selectedNodes]);

  const macChangeDescription =
    singleNode
      ? `Change MAC(s) on ${singleNode}. Auto-swaps if the new MAC is owned by exactly one other node.`
      : selectedNodes.size === 0
        ? "Select exactly 1 node to enable."
        : `Select exactly 1 node (currently ${selectedNodes.size}).`;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <DeepAuditCard
        rack={rack}
        selectedNodes={selectedNodes}
        naming={naming}
        disabled={disabled}
        kind="hardware"
        title="Hardware Deep Audit"
        description={`Deep GPU/thermal/ECC/recovery, Xid/PCIe/EDAC, service/IMEX and storage audit on ${nodeCount} selected node(s). Confirmed failures are commented and drained through the active scheduler automatically.`}
        icon={ScanSearch}
      />
      <ActionCard
        title="Power On"
        description={`Power-cycle ${nodeCount} selected node(s) in ${rack} through the PythonCM device power API.`}
        icon={<Power size={20} />}
        buttonLabel="Power On"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onBeforeConfirmDialog={onSelectPowerResetTargets}
        onRun={() => runAsyncReset("Power on", "power", "/api/power-on-async", specsBody())}
      />
      <ActionCard
        title="GPU Reset"
        description={`Reset GPUs on ${nodeCount} selected node(s). Quiesces cluster services, runs nvidia-smi -r, then restores services.`}
        icon={<Cpu size={20} />}
        buttonLabel="Reset GPUs"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runAsyncReset("GPU reset", "gpu", "/api/gpu-reset-async", specsBody())}
      />
      <Card style={{ padding: 20 }}>
        <Flex gap="3" align="start">
          <div style={{ color: "var(--text-color-secondary)", marginTop: 2 }}>
            <ScanSearch size={20} />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <Flex gap="2" align="center" style={{ marginBottom: 4 }}>
              <Text kind="body/bold/md">Hardware Check</Text>
            </Flex>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {`Run checker.sh on ${nodeCount} selected node(s) through PythonCM ProgramRunner. Choose a PythonCM-safe preset or custom arguments.`}
            </Text>
          </div>
          <KuiButton
            kind="secondary"
            size="small"
            onClick={() => setHwCheckDialogOpen(true)}
            disabled={disabled || nodeCount === 0}
          >
            Run Check
          </KuiButton>
        </Flex>
      </Card>
      <HwCheckRunDialog
        open={hwCheckDialogOpen}
        onClose={() => setHwCheckDialogOpen(false)}
        subtitle={`${nodeCount} selected node(s) in ${rack}`}
        onConfirm={(opts) => {
          setHwCheckDialogOpen(false);
          void runAsyncReset("Hardware check", "hw", "/api/hw-check-async", {
            ...specsBody(),
            ...hwCheckOptionsBody(opts),
          });
        }}
      />
      <ActionCard
        title="Exemplar System Info Validation"
        description={`Run NVIDIA DGXC v26.06.01 system_info on every one of the ${nodeCount} selected node(s), then validate each result against the Exemplar recipe.`}
        icon={<ClipboardCheck size={20} />}
        buttonLabel="Plan Sysinfo"
        disabled={disabled || nodeCount === 0}
        onRun={() => setSysinfoDialogOpen(true)}
      />
      <ValidationPresetDialog
        open={sysinfoDialogOpen}
        onClose={() => setSysinfoDialogOpen(false)}
        presetId="exemplar-system-info"
        scope={validationScope}
        nodes={selectedNodes}
        racks={[rack]}
        logDir={logDir}
        onLaunch={(request: ValidationPresetRequest) =>
          launchAsyncValidationFamily(
            "NVIDIA DGXC Exemplar system_info",
            "hw",
            "/api/run-validation-preset-async",
            (jid) => `/api/run-validation-preset-status?job_id=${encodeURIComponent(jid)}`,
            request,
          )
        }
      />
      <ActionCard
        title="Bug Report"
        description={`Collect nvidia-bug-report.sh from ${nodeCount} selected node(s). Archives stored on dashboard host.`}
        icon={<Bug size={20} />}
        buttonLabel="Collect"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runAsyncReset("Bug report", "hw", "/api/bug-report-async", specsBody())}
      />
      <ActionCard
        title="IMEX Reset"
        description={`Reset IMEX daemon on ${rack} (rack-level script).`}
        icon={<RotateCcw size={20} />}
        buttonLabel="Reset IMEX"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runAsyncReset("IMEX reset", "nmxc", "/api/imex-reset-async", imexBody())}
      />
      <ActionCard
        title="IMEX Check"
        description={`Verify IMEX daemon health on ${rack}.`}
        icon={<CircleCheck size={20} />}
        buttonLabel="Check IMEX"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runAsyncReset("IMEX check", "hw", "/api/imex-check-async", imexBody())}
      />
      <ActionCard
        title="BMC Recovery"
        description={`Cold-reset the BMC (rf0) on ${nodeCount} selected node(s) via ipmitool.`}
        icon={<Zap size={20} />}
        buttonLabel="Reset BMC"
        disabled={disabled}
        destructive
        selectedNodes={selectedNodes}
        onRun={() =>
          runAsyncReset("BMC recovery", "bmc", "/api/bmc-recovery-async", bmcRecoveryBody(rack, logDir, selectedNodes))
        }
      />
      <ActionCard
        title="MAC Change"
        description={macChangeDescription}
        icon={<Network size={20} />}
        buttonLabel="Change MAC"
        disabled={disabled || !singleNode}
        onRun={() => setMacDialogOpen(true)}
      />
      {singleNode && (
        <MacChangeDialog
          open={macDialogOpen}
          rack={rack}
          node={singleNode}
          onClose={() => setMacDialogOpen(false)}
        />
      )}
    </Box>
  );
}
