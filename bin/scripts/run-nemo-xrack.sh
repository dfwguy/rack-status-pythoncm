#!/bin/bash
# Multinode NEMO launch. Pass rack targets as one comma-separated spec or multiple arguments, e.g.
#   p5-r[13-14]-ct[01-18],p5-r[15-16]-ct[01-18]
# Cwd should be the log dir (nvis-rack-status) so relative sbatch --output lands there.
#
# Slurm stdout basename (example):
#   root-NEMO-p5-r[13-14]-ct[01-18],p5-r[15-16]-ct[01-18]-72N-22403.txt

set -uo pipefail

# Force ssh transport: this cluster's pdsh defaults to rsh (refused on compute nodes).
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no"
export PDSH_RCMD_TYPE=ssh

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Make the Slurm CLI (scontrol/sbatch) usable in non-login SSH shells: the dashboard
# launches this over `ssh host '<cmd>'` where Environment Modules aren't initialized,
# so scontrol/sbatch would be "command not found". Idempotent; also on the head.
# shellcheck disable=SC1091
[[ -f "${SCRIPT_DIR}/slurm-init.inc.sh" ]] && source "${SCRIPT_DIR}/slurm-init.inc.sh"

# Filename tag: same specs as passed in (comma-separated if multiple argv).
spec_tag=""
for _a in "$@"; do
  [[ -n "$spec_tag" ]] && spec_tag+=","
  spec_tag+="$_a"
done

echo "Hosts: ${spec_tag}"

# Resolve targets to a REAL Slurm nodelist. The dashboard passes CT-validation specs
# like "bos01-b05-ct[01-18],bos01-b06-ct[01-18]"; on GB200 sites those are NOT Slurm
# hostnames (e.g. real = bos01-b05-11-s1-dgx-05-c01), so derive each spec's rack id and
# map it to real nodes via lib/nodes.inc.sh (RACK_TO_NODE_SED / RACK_NODE_MATCH_SUFFIX),
# the same mapping the NCCL launcher uses. Falls back to a direct hostlist expansion
# when the mapping yields nothing (sites where the passed names are already real Slurm
# nodes). Avoids clustershell `nodeset`, which is frequently absent on BCM head nodes.
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/nodes.inc.sh"

racks=()
IFS=',' read -ra _specs <<< "${spec_tag}"
for _s in "${_specs[@]}"; do
  racks+=( "$(printf '%s' "${_s}" | sed -E 's/-ct(\[[^]]*\]|[0-9]+)$//')" )
done

hosts=()
if nvis_nodes_resolve "${racks[@]}" 2>/dev/null && [[ -n "${NVIS_NODELIST:-}" ]]; then
  mapfile -t hosts < <(scontrol show hostname "${NVIS_NODELIST}" 2>/dev/null)
fi
if ((${#hosts[@]} == 0)); then
  # Fallback: expand the passed spec directly (sites where these ARE real Slurm nodes).
  mapfile -t hosts < <(scontrol show hostname "${spec_tag}" 2>/dev/null)
fi
if ((${#hosts[@]} == 0)); then
  echo "run-nemo-xrack: could not resolve any Slurm nodes for: ${spec_tag}" >&2
  echo "run-nemo-xrack: set RACK_TO_NODE_SED / RACK_NODE_MATCH_SUFFIX for this site, or pass a real nodelist." >&2
  exit 1
fi
echo "run-nemo-xrack: resolved ${#hosts[@]} nodes: ${hosts[0]} ... ${hosts[-1]}"

n=$(( ${#hosts[*]} / 2 * 2 ))

# Prefer the NCCL 2.30.7 image (libnccl + nccl-tests baked at 2.30.7) when present. This overrides
# the caller's NEMO_SQSH_SRC (incl. the app/UI default of the stock image) so every launch uses the
# matched-NCCL image without needing per-launch env. Set NVIS_NEMO_USE_VETTED=0 to force the caller's
# NEMO_SQSH_SRC (e.g. to compare vs stock).
#
# WHY 2.30.7 (not the older 2.28.9 vetted image): cross-rack (multi-domain NVL72) alltoall REQUIRES
# NCCL >= 2.30 — 2.28.9 fails the bootstrap regardless of env vars (validated 2026-07-07: same
# NCCL_NET_PLUGIN=none, 2.30.7 PASSES job 203 vs 2.28.9 FAILS job 210). 2.30.7 then needs
# NCCL_IGNORE_COLLNET_MISMATCH=1 (set below + in the batch script) to tolerate the stale HPC-X SHARP
# plugin's asymmetric CollNet device count while KEEPING SHARP (job 208 all-pass). See
# VALIDATION_TRACKER "Cross-rack alltoall". Override the path with NVIS_NEMO_VETTED_SQSH.
_vetted="${NVIS_NEMO_VETTED_SQSH:-/home/cmods/workspace/images/nemo-nccl2.30.7.sqsh}"
if [[ "${NVIS_NEMO_USE_VETTED:-1}" == "1" && -f "${_vetted}" ]]; then
  echo "run-nemo-xrack: using NCCL 2.30.7 image ${_vetted} (matched NCCL baked in; LD_PRELOAD off; IGNORE_COLLNET_MISMATCH keeps HPC-X/SHARP)"
  NEMO_SQSH_SRC="${_vetted}"
  export NVIS_NCCL_PRELOAD_DISABLE="${NVIS_NCCL_PRELOAD_DISABLE:-1}"
fi
# Cross-rack alltoall fix (keeps SHARP). The batch script defaults this to 1 too, but set it here so
# the launcher is self-documenting and forwards it explicitly. Override with NVIS_NCCL_IGNORE_COLLNET_MISMATCH.
export NVIS_NCCL_IGNORE_COLLNET_MISMATCH="${NVIS_NCCL_IGNORE_COLLNET_MISMATCH:-1}"
: "${NEMO_SQSH_SRC:?set NEMO_SQSH_SRC to the head-node path of the nemo .sqsh image to copy under /raid/data/ on compute nodes}"
_sqsh_b=$(basename "$NEMO_SQSH_SRC")
_hostcsv="$(echo "${hosts[*]}" | tr ' ' ',')"
# Stage the image to each node's LOCAL /raid/data (fast NVMe; avoids saturating NFS with a
# 36-node container-start read). /raid/data is root-owned on the compute trays, so use
# passwordless sudo (NOPASSWD is propagated on the b0x trays). Then VERIFY every node has the
# full image before submitting — otherwise pyxis dies with "No such file" mid-job.
_src_size=$(stat -c%s "$NEMO_SQSH_SRC" 2>/dev/null || echo 0)
echo "run-nemo-xrack: staging ${_sqsh_b} (${_src_size} bytes) to /raid/data on ${#hosts[@]} nodes (sudo rsync)…"
pdsh -R ssh -w "$_hostcsv" "sudo -n mkdir -p /raid/data && sudo -n rsync -ah '${NEMO_SQSH_SRC}' /raid/data/'${_sqsh_b}'" 2>&1 | dshbak -c 2>/dev/null || true
_bad=$(pdsh -R ssh -w "$_hostcsv" "s=\$(stat -c%s /raid/data/'${_sqsh_b}' 2>/dev/null || echo 0); [ \"\$s\" = '${_src_size}' ] || echo BAD" 2>/dev/null | grep -c BAD || true)
if [ "${_bad:-1}" -ne 0 ]; then
  echo "run-nemo-xrack: ERROR: image not fully staged to /raid/data on ${_bad} node(s) (check sudo -n / /raid space). Aborting before submit." >&2
  exit 1
fi
echo "run-nemo-xrack: staged OK on all ${#hosts[@]} nodes"
# Tell the batch script which staged image to run (it defaults to the stock name otherwise, so
# NEMO_SQSH_SRC=<vetted image> would be staged but not actually used).
export NVIS_NEMO_CONT="/raid/data/${_sqsh_b}"

_out_stem="root-NEMO-${spec_tag}-${n}N"

# Export the real scripts dir to the job so the batch script can find lib/fabric.inc.sh
# (Slurm spools only the batch script, not lib/). Without it the fabric classifier no-ops
# and NCCL falls back to a naive mlx5_0..3 set that mixes IB + RoCE and fails.
export NVIS_SCRIPTS_DIR="${SCRIPT_DIR}"
sbatch -N "${n}" \
  -w "$(echo "${hosts[*]}" | tr ' ' ',')" \
  -t 03:00:00 \
  --job-name="NEMO-${n}N" \
  --export="ALL,NVIS_SCRIPTS_DIR=${SCRIPT_DIR},NVIS_NEMO_CONT=${NVIS_NEMO_CONT},NVIS_NCCL_PRELOAD_DISABLE=${NVIS_NCCL_PRELOAD_DISABLE:-0},NVIS_NCCL_IGNORE_COLLNET_MISMATCH=${NVIS_NCCL_IGNORE_COLLNET_MISMATCH:-1}" \
  --output="${_out_stem}-%j.txt" \
  "$SCRIPT_DIR/nemo-nemotron5h_56b-mbridge-fp8.sh"
