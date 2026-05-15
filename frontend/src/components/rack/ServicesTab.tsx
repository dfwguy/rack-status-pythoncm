import Box from "@mui/material/Box";
import { ActionCard } from "./ActionCard";
import { RefreshCw, Info, FileText } from "lucide-react";
import { useRackCommands } from "../../context/RackCommandContext";
import { specsCommandBody, validationFamilyBody } from "../../lib/rackCommandBodies";

interface ServicesTabProps {
  rack: string;
  logDir?: string;
  selectedNodes: Set<string>;
  disabled: boolean;
}

export function ServicesTab({ rack, logDir, selectedNodes, disabled }: ServicesTabProps) {
  const { runAsyncReset, openStream, openTailLogNemo } = useRackCommands();
  const nodeCount = selectedNodes.size;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <ActionCard
        title="Restart Services"
        description={`Restart services on ${nodeCount} selected node(s): mount -a, munge, slurmd, nvidia-imex, nvidia-dcgm.`}
        icon={<RefreshCw size={20} />}
        buttonLabel="Restart"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          runAsyncReset("Restart services", "services", "/api/services-restart-async", specsCommandBody(rack, logDir, selectedNodes))
        }
      />
      <ActionCard
        title="System Info"
        description={`Stream live system info from ${nodeCount} selected node(s). GPU status, driver, fabric manager.`}
        icon={<Info size={20} />}
        buttonLabel="View Sysinfo"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          openStream(
            "System info (stream)",
            "/api/run-sysinfo-stream",
            validationFamilyBody(rack, logDir, selectedNodes) as Record<string, unknown>,
          )
        }
      />
      <ActionCard
        title="Tail Log"
        description={`Live tail the latest NEMO validation log for ${rack}. Streams output in real time.`}
        icon={<FileText size={20} />}
        buttonLabel="Tail Log"
        onRun={() => openTailLogNemo()}
      />
    </Box>
  );
}
