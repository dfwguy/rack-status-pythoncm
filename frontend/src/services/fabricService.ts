import { apiFetch } from "../lib/apiFetch";

/** One node's detected RDMA fabric classification (see bin/scripts/fabric_probe.sh). */
export interface FabricProbe {
  host: string;
  link: string; // "ib" | "roce"
  rate_gbps: number;
  compute_rails: number;
  gpus: number;
  nccl_ib_hca: string;
  ucx_net_devices: string;
  ns_ifname: string;
  expect_rails: string;
  expect_rate: string;
  discrepancies: string[];
}

export interface FabricProbeResponse {
  ok: boolean;
  node?: string;
  fabric?: FabricProbe;
  error?: string;
  stderr?: string;
}

/**
 * Classify a compute node's fabric (compute rails vs storage/NS plane) and
 * compare against the active platform's expected fabric profile.
 * With no node, the backend probes the first Slurm node (homogeneous cluster).
 */
export async function probeFabric(node?: string): Promise<FabricProbeResponse> {
  const q = node ? `?node=${encodeURIComponent(node)}` : "";
  const res = await apiFetch(`/api/fabric-probe${q}`);
  return (await res.json()) as FabricProbeResponse;
}
