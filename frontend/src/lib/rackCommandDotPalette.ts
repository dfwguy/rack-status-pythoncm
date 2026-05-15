import type { NodeCmdKind } from "./rackCommandTypes";

const DOT_PALETTE: Record<NodeCmdKind, { bg: string; ring: string }> = {
  validation: { bg: "#38bdf8", ring: "rgba(56, 189, 248, 0.45)" },
  slurm: { bg: "#38bdf8", ring: "rgba(56, 189, 248, 0.45)" },
  stress: { bg: "#fb923c", ring: "rgba(194, 65, 12, 0.5)" },
  power: { bg: "#4ade80", ring: "rgba(22, 101, 52, 0.55)" },
  gpu: { bg: "#60a5fa", ring: "rgba(29, 78, 216, 0.55)" },
  services: { bg: "#fbbf24", ring: "rgba(161, 98, 7, 0.55)" },
  hw: { bg: "#34d399", ring: "rgba(4, 120, 87, 0.55)" },
  nmxc: { bg: "#c084fc", ring: "rgba(126, 34, 206, 0.55)" },
  bmc: { bg: "#f87171", ring: "rgba(185, 28, 28, 0.55)" },
  mac: { bg: "#a78bfa", ring: "rgba(91, 33, 182, 0.55)" },
};

export function rackCommandDotColors(kind: NodeCmdKind, isErr: boolean) {
  if (isErr) return { bg: "#ef4444", ring: "rgba(239, 68, 68, 0.4)" };
  return DOT_PALETTE[kind] ?? DOT_PALETTE.validation;
}
