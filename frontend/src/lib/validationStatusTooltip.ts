import type { RackRow } from "../types/api";

type ValidationTooltipRow = Pick<RackRow, "nemo" | "last_job" | "nemo_hint">;

/** Hover text for validation column; empty string means no tooltip (e.g. done). */
export function validationStatusTooltip(row: ValidationTooltipRow): string {
  const n = (row.nemo || "").trim();
  if (n === "done") return "";
  const fromServer = (row.nemo_hint || "").trim();
  if (fromServer) return fromServer;
  if (n === "pending") {
    return (
      "No NEMO or NCCL validation log matches this rack in the log directory yet " +
      "(or the native probe has not finished)."
    );
  }
  if (n.startsWith("ongoing")) {
    const lj = (row.last_job || "").trim();
    if (lj && lj !== "-") {
      return `Slurm job ${lj} is running; the validation log is still being written.`;
    }
    return "Slurm reports a running validation job.";
  }
  if (n === "fail") {
    return (
      "Validation did not complete successfully; inspect the log or VALIDATION_TEST in Settings " +
      "for required NCCL / LLM signals."
    );
  }
  return "";
}
