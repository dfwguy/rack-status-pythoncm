import Box from "@mui/material/Box";
import { ActionCard } from "./ActionCard";
import { RefreshCw, Info, FileText } from "lucide-react";

interface ServicesTabProps {
  rack: string;
  selectedNodes: Set<string>;
  disabled: boolean;
}

export function ServicesTab({ rack, selectedNodes, disabled }: ServicesTabProps) {
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
      />
      <ActionCard
        title="System Info"
        description={`Stream live system info from ${nodeCount} selected node(s). GPU status, driver, fabric manager.`}
        icon={<Info size={20} />}
        buttonLabel="View Sysinfo"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="Tail Log"
        description={`Live tail the latest NEMO validation log for ${rack}. Streams output in real time.`}
        icon={<FileText size={20} />}
        buttonLabel="Tail Log"
      />
    </Box>
  );
}
