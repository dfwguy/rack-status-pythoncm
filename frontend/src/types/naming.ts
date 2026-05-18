/** Naming hints from GET /api/status ``naming`` (Settings → NVIS_* templates). */
export interface NvisNamingSchema {
  rack_id: string;
  ct_id: string;
  nvsw_id: string;
  /** Literal between rack id and ``[tray-ranges]`` (e.g. ``-ct``); empty for CBR-style. */
  ct_joiner: string;
  node_id_mode: "rack_ct_suffix" | "full_hostname";
}
