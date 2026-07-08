/** Slurm job id suitable for scancel (digits, optional _task for job arrays). Legacy-aligned. */
export function slurmJobIdForScancel(raw: unknown): string {
  const t = String(raw ?? "").trim();
  return /^[1-9]\d*(_\d+)?$/.test(t) ? t : "";
}

/** Parse ``StdOut=`` from ``scontrol show job`` stdout (multiline). */
export function parseSlurmStdOutPathFromScontrolText(text: string): string {
  const m = /^\s*StdOut\s*=\s*(.+)$/im.exec(String(text || ""));
  if (!m) return "";
  let p = String(m[1] || "").trim();
  if (!p || /^\(none\)$/i.test(p) || /^\/dev\/null$/i.test(p)) return "";
  return p;
}

export type SlurmSacctJob = {
  job_id: string;
  name: string;
  partition: string;
  nnodes: string;
  nodelist: string;
  start: string;
  end: string;
  elapsed: string;
  state: string;
  exit_code: string;
};

export type SlurmCmdJson = {
  ok?: boolean;
  error?: string;
  stdout?: string;
  stderr?: string;
  command_display?: string;
  returncode?: number;
  job_id?: string;
  /** /api/slurm-sacct-recent */
  jobs?: SlurmSacctJob[];
  /** /api/slurm-job-log-path */
  path?: string;
  source?: string;
};

export function formatSlurmCmdDetail(data: SlurmCmdJson): string {
  const lines: string[] = [];
  if (data.command_display) lines.push(String(data.command_display));
  lines.push(`exit ${data.returncode ?? "?"}`);
  if (data.stdout) lines.push(`stdout:\n${data.stdout}`);
  if (data.stderr) lines.push(`stderr:\n${data.stderr}`);
  return lines.join("\n").trim();
}

export async function fetchSlurmJson(url: string, init?: RequestInit): Promise<{ res: Response; data: SlurmCmdJson }> {
  const res = await fetch(url, { credentials: "same-origin", ...init });
  const raw = await res.text();
  let data: SlurmCmdJson = {};
  try {
    data = raw ? (JSON.parse(raw) as SlurmCmdJson) : {};
  } catch {
    throw new Error(`Invalid JSON (HTTP ${res.status}): ${raw.slice(0, 300)}`);
  }
  return { res, data };
}

export function findSqueueJobColumnIndex(headers: string[]): number {
  for (let hi = 0; hi < headers.length; hi++) {
    const h = String(headers[hi] || "")
      .trim()
      .toUpperCase()
      .replace(/\s+/g, "_");
    if (h === "JOBID" || h === "JOB_ID") return hi;
  }
  return -1;
}
