export type NodeCmdKind =
  | "validation"
  | "slurm"
  | "stress"
  | "power"
  | "gpu"
  | "services"
  | "hw"
  | "nmxc";

/** Client-only replay buffer per rack (legacy `rackLastAsyncOutput`). */
export interface RackLastAsyncOutput {
  cmdKind: NodeCmdKind;
  isErr: boolean;
  preview: string;
  full: string;
  at: number;
  noticeTitle: string;
}

export function validationRackKey(rack: string): string {
  return (rack || "").trim().toLowerCase();
}
