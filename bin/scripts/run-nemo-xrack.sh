#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Multinode NEMO launch. Pass rack targets as one comma-separated spec or multiple arguments, e.g.
#   p5-r[13-14]-ct[01-18],p5-r[15-16]-ct[01-18]
# Dashboard and manual launches use the same shared cluster log root.
#
# Slurm stdout basename (example):
#   root-NEMO-p5-r[13-14]-ct[01-18],p5-r[15-16]-ct[01-18]-72N-22403.txt

set -uo pipefail

# Force ssh transport: this cluster's pdsh defaults to rsh (refused on compute nodes).
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no"
export PDSH_RCMD_TYPE=ssh

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-/cluster/benchmark/harish/rack_status/logs}}"
mkdir -p "${JOB_LOG_DIR}" || { echo "run-nemo-xrack: cannot create log directory: ${JOB_LOG_DIR}" >&2; exit 2; }

# Which NeMo batch script to submit. Default = the Stage-2 soak / Stage-3 LLM engine
# (nemo-nemotron5h_56b-mbridge-fp8.sh, now parametrized by NVIS_NEMO_DTYPE/MAX_STEPS/GBS/MODEL_SIZE).
# Override with NVIS_NEMO_BATCH_SCRIPT to point at a different launcher.
NVIS_NEMO_BATCH_SCRIPT="${NVIS_NEMO_BATCH_SCRIPT:-nemo-nemotron5h_56b-mbridge-fp8.sh}"

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

# Split the comma-joined specs on commas that are OUTSIDE [...]. A plain IFS=',' split tears a
# bracketed range apart wherever the range itself contains a comma: this site's tray range is
# 11-18,28-37, so "ink50-dh01-h02-[11-18,28-37]" became the two fragments
# "ink50-dh01-h02-[11-18" and "28-37]". Neither is a rack id, so nvis_nodes_resolve found nothing
# and we fell through to the per-spec fallback — which works here only because the CT template
# happens to equal the real hostname, and would produce wrong names on a site where it does not
# Exact Nscale hostnames are inventory-backed. Simple ranges like [11-18] have no inner comma,
# which is why this stayed hidden.
_specs=()
_buf=""
_depth=0
for ((_i = 0; _i < ${#spec_tag}; _i++)); do
  _c="${spec_tag:_i:1}"
  case "$_c" in
    '[') _depth=$((_depth + 1)); _buf+="$_c" ;;
    ']') _depth=$((_depth > 0 ? _depth - 1 : 0)); _buf+="$_c" ;;
    ',')
      if ((_depth > 0)); then _buf+="$_c"; else
        [[ -n "$_buf" ]] && _specs+=("$_buf")
        _buf=""
      fi
      ;;
    *) _buf+="$_c" ;;
  esac
done
[[ -n "$_buf" ]] && _specs+=("$_buf")

racks=()
for _s in "${_specs[@]}"; do
  # Strip the tray part to leave the rack id. The joiner is site-specific (NVIS_CT_ID): Bright
  # is <rack>-ct[…] but Nscale TX is <rack>-[…], so matching a literal "-ct" left the whole
  # spec as the "rack id", nvis_nodes_resolve found nothing, and we silently fell through to the
  # per-spec fallback below — which bypasses this site's rack->node mapping entirely and only
  # happens to work where the CT template already equals the real hostname.
  #
  # Drop a trailing [ranges] group with an optional "ct" joiner, plus the legacy single-tray
  # -ct<N> form. Deliberately NOT a bare trailing -<N>: that would turn a real hostname
  # (ink50-dh01-h02-11) into a truncated prefix instead of leaving it for the fallback.
  racks+=( "$(printf '%s' "${_s}" | sed -E 's/-(ct)?\[[^]]*\]$//; s/-ct[0-9]+$//')" )
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

# Force the batch script's multi-rack (IB) path when this allocation spans >1 rack, even if it is
# <=18 nodes. The batch script otherwise infers single-rack from node count and would disable IB +
# force MNNVL (fatal across a rack boundary). Explicit NVIS_NEMO_XRACK from the caller wins.
#
# Group hosts by rack via NVIS_RACK_ID (nvis_nodes_rack_of). This used to hardcode
# a fixed site regex, which matches nothing outside that layout: every hostname
# stayed distinct, so an 18-node SINGLE-rack run counted 18 "racks" and was pushed
# onto the cross-rack IB path. Without the template, fall back to the number of rack
# specs the caller passed (the dashboard always passes one spec per rack).
_nracks=$(for _h in "${hosts[@]}"; do nvis_nodes_rack_of "${_h}"; echo; done 2>/dev/null | sort -u | grep -c .)
if (( _nracks == 0 )); then
  _nracks=$(printf '%s\n' "${racks[@]}" | sort -u | grep -c .)
fi
if [[ -z "${NVIS_NEMO_XRACK:-}" ]]; then
  if (( _nracks > 1 )); then NVIS_NEMO_XRACK=1; else NVIS_NEMO_XRACK=0; fi
fi
export NVIS_NEMO_XRACK
echo "run-nemo-xrack: ${_nracks} distinct rack(s) -> NVIS_NEMO_XRACK=${NVIS_NEMO_XRACK}"

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
: "${NEMO_SQSH_SRC:?set NEMO_SQSH_SRC to the configured nemo .sqsh image}"
_sqsh_b=$(basename "$NEMO_SQSH_SRC")
_hostcsv="$(echo "${hosts[*]}" | tr ' ' ',')"
_src_size=$(stat -c%s "$NEMO_SQSH_SRC" 2>/dev/null || echo 0)
if [[ ! "${_src_size}" =~ ^[1-9][0-9]*$ ]]; then
  echo "run-nemo-xrack: ERROR: configured image is missing or empty: ${NEMO_SQSH_SRC}" >&2
  exit 1
fi

# Bryan's bringup-agent/FileCopyTrigger keeps every image in the shared container directory
# warm under /tmp/containers on each compute node. Prefer that copy directly: copying the same
# 40+ GiB image from the head node at launch defeats the cache and creates a fleet-wide NFS
# burst before Slurm even sees the job.
_shared_cache_dir="${NVIS_NEMO_SHARED_CACHE_DIR:-/cluster/containers}"
_node_cache_dir="${NVIS_NEMO_NODE_CACHE_DIR:-/tmp/containers}"
_cache_b="${NVIS_NEMO_CACHE_BASENAME:-}"

# build_containers.sh adds an architecture suffix, while the LLMB installer names the same
# logical image without one. Prefer an exact basename, then the ARM spelling used on this GB300
# fleet. An explicit NVIS_NEMO_CACHE_BASENAME always wins (needed for a separately vetted image).
if [[ -z "${_cache_b}" ]]; then
  _cache_candidates=("${_sqsh_b}")
  if [[ "${_sqsh_b}" == *.sqsh ]]; then
    _cache_candidates+=("${_sqsh_b%.sqsh}-arm.sqsh")
  fi
  for _candidate in "${_cache_candidates[@]}"; do
    _candidate_size=$(stat -c%s "${_shared_cache_dir}/${_candidate}" 2>/dev/null || echo 0)
    if [[ "${_candidate_size}" == "${_src_size}" ]]; then
      _cache_b="${_candidate}"
      break
    fi
  done
fi

_shared_cache_path="${_shared_cache_dir}/${_cache_b}"
_cache_size=$(stat -c%s "${_shared_cache_path}" 2>/dev/null || echo 0)
if [[ -z "${_cache_b}" || "${_cache_size}" != "${_src_size}" ]]; then
  echo "run-nemo-xrack: ERROR: no matching pre-cached image for ${NEMO_SQSH_SRC}." >&2
  echo "run-nemo-xrack: expected ${_src_size} bytes under ${_shared_cache_dir}; refusing the legacy head-node fan-out." >&2
  echo "run-nemo-xrack: stage the image through FileCopyTrigger, or set NVIS_NEMO_CACHE_BASENAME explicitly." >&2
  exit 1
fi

_node_cache_path="${_node_cache_dir}/${_cache_b}"
echo "run-nemo-xrack: checking pre-cached ${_cache_b} (${_cache_size} bytes) on ${#hosts[@]} node(s)…"

# Verify by counting nodes that POSITIVELY confirm the full readable file, and require all of
# them. Unreachable nodes and short/absent files cannot accidentally pass. Do not fall back to
# rsync from /home: a green-button click must never trigger an unbounded 40+ GiB fan-out.
#
# This used to count nodes that answered with a WRONG size ("echo BAD") and pass when that count
# was zero, with pdsh's stderr sent to /dev/null. A node that could not be reached at all never
# ran the remote command, so it printed no BAD and was silently counted as staged: a run where
# ssh failed 255 on all 36 nodes still reported "staged OK on all 36 nodes" and went on to
# submit. Requiring one OK per node inverts that — unreachable or short/missing files fail the
# same way, because none of them produce CACHE_OK.
_verify_out=$(pdsh -R ssh -w "$_hostcsv" \
  "s=\$(stat -c%s '${_node_cache_path}' 2>/dev/null || echo 0); if [ \"\$s\" = '${_cache_size}' ] && [ -r '${_node_cache_path}' ]; then echo CACHE_OK; else echo CACHE_MISS_size=\$s; fi" 2>&1)
_ok=$(printf '%s\n' "${_verify_out}" | grep -c 'CACHE_OK' || true)
if [ "${_ok:-0}" -ne "${#hosts[@]}" ]; then
  echo "run-nemo-xrack: ERROR: local image verified on only ${_ok}/${#hosts[@]} nodes at ${_node_cache_path}." >&2
  echo "run-nemo-xrack: first cache misses or transport failures:" >&2
  printf '%s\n' "${_verify_out}" | grep -v 'CACHE_OK' | head -10 | sed 's/^/  /' >&2
  echo "run-nemo-xrack: aborting before submit; wait for bringup-agent/FileCopyTrigger rather than reading the image from the head node." >&2
  exit 1
fi
echo "run-nemo-xrack: cache ready on all ${#hosts[@]} nodes (verified ${_ok}/${#hosts[@]}); skipping head-node rsync"
export NVIS_NEMO_CONT="${_node_cache_path}"

# A node can remain IDLE in Slurm and still have every GPU marked reset-required. In that state
# nvidia-smi inventory queries succeed, but CUDA context creation fails with "device busy or
# unavailable" and one bad rack cancels the entire multi-rack step. Require all four GB300 GPUs on
# every target node to report no pending recovery action before consuming a large allocation.
# Keep the override explicit for emergency diagnosis; normal UI launches fail closed.
if [[ "${NVIS_NEMO_GPU_RECOVERY_CHECK:-1}" == "1" ]]; then
  echo "run-nemo-xrack: checking GPU recovery state on ${#hosts[@]} node(s)…"
  _gpu_out=$(pdsh -R ssh -w "$_hostcsv" \
    'a=$(nvidia-smi --query-gpu=gpu_recovery_action --format=csv,noheader 2>&1); rc=$?; n=$(printf "%s\n" "$a" | grep -c "^None$"); if [ "$rc" -eq 0 ] && [ "$n" -eq 4 ]; then echo GPU_READY; else printf "GPU_BLOCKED recovery=%s\n" "$(printf "%s" "$a" | tr "\n" ",")"; fi' 2>&1)
  _gpu_ok=$(printf '%s\n' "${_gpu_out}" | grep -c 'GPU_READY' || true)
  if [ "${_gpu_ok:-0}" -ne "${#hosts[@]}" ]; then
    echo "run-nemo-xrack: ERROR: healthy GPU recovery state confirmed on only ${_gpu_ok}/${#hosts[@]} nodes." >&2
    echo "run-nemo-xrack: first reset-required, query, or transport failures:" >&2
    printf '%s\n' "${_gpu_out}" | grep -v 'GPU_READY' | head -20 | sed 's/^/  /' >&2
    echo "run-nemo-xrack: aborting before submit; recover the affected nodes and rerun the preflight." >&2
    exit 1
  fi
  echo "run-nemo-xrack: GPU recovery state ready on all ${#hosts[@]} nodes (verified ${_gpu_ok}/${#hosts[@]})"
else
  echo "run-nemo-xrack: WARNING: GPU recovery check disabled by NVIS_NEMO_GPU_RECOVERY_CHECK=0" >&2
fi

# Safe preflight mode for operators and automation: perform the same target resolution and cache
# and GPU-recovery verification as the green button, but stop before sbatch.
if [[ "${NVIS_NEMO_CACHE_CHECK_ONLY:-0}" == "1" ]]; then
  echo "run-nemo-xrack: cache-check-only complete; no Slurm job submitted"
  exit 0
fi

# Log filename uses just the racks included in the test. The arg may be a full curated nodelist
# (real hostnames) rather than rack-ct specs; using it verbatim would blow past the 255-byte
# filename limit for --output and make sbatch fail before writing any log (job dies ~2s,
# ExitCode 0:53). Derive the distinct rack ids from the resolved hosts instead.
#
# Via nvis_nodes_rack_of (NVIS_RACK_ID), the same way the rack count above does. This was the
# second place hardcoding one site prefix; only the count was converted. On Nscale the
# sed matched nothing, so every hostname stayed distinct and _racktag became all N hostnames
# joined by '+' — roughly 700 characters for 36 nodes, which is exactly the >255-byte --output
# name this comment warns about.
_racktag=$(for _h in "${hosts[@]}"; do nvis_nodes_rack_of "${_h}"; echo; done 2>/dev/null \
  | sort -u | grep -v '^$' | paste -sd+ | tr -d ' ')
# No template match: fall back to the rack specs the caller passed (the dashboard passes one per
# rack), never to the raw hostname list.
if [[ -z "${_racktag}" ]]; then
  _racktag=$(printf '%s\n' "${racks[@]}" | sort -u | paste -sd+ | tr -d ' ')
fi
[[ -n "${_racktag}" ]] || _racktag="${spec_tag}"

# Even a correct rack-only tag exceeds NAME_MAX for a full 16-rack SU (the old tag was 239
# characters before adding root-NEMO-, node count, job id and .txt). Keep the endpoints and rack
# count for humans, plus a digest for uniqueness. Slurm otherwise accepts the job, allocates every
# node, then fails in one second with ExitCode 0:53 because stdout cannot be opened.
if (( ${#_racktag} > 120 )); then
  _racktag_full="${_racktag}"
  _racktag_first="${_racktag_full%%+*}"
  _racktag_last="${_racktag_full##*+}"
  _racktag_hash=$(printf '%s' "${_racktag_full}" | sha256sum | cut -c1-10)
  _racktag="${_racktag_first}+${_racktag_last}+${_nracks}r-${_racktag_hash}"
  echo "run-nemo-xrack: compacted long rack tag to ${_racktag}"
fi
_out_stem="root-NEMO-${_racktag}-${n}N"
_out_path="${JOB_LOG_DIR%/}/${_out_stem}-%j.txt"

# Export the real scripts dir to the job so the batch script can find lib/fabric.inc.sh
# (Slurm spools only the batch script, not lib/). Without it the fabric classifier no-ops
# and NCCL falls back to a naive mlx5_0..3 set that mixes IB + RoCE and fails.
export NVIS_SCRIPTS_DIR="${SCRIPT_DIR}"
cd "${JOB_LOG_DIR}" || exit 2
if [[ "${NVIS_NEMO_SUBMIT_DRY_RUN:-0}" == "1" ]]; then
  echo "run-nemo-xrack: submit-dry-run output=${_out_path} nodes=${n}; no Slurm job submitted"
  exit 0
fi
sbatch -N "${n}" \
  -w "$(echo "${hosts[*]}" | tr ' ' ',')" \
  -t "${NVIS_NEMO_WALLTIME:-03:00:00}" \
  --job-name="NEMO-${n}N" \
  --export="ALL,NVIS_SCRIPTS_DIR=${SCRIPT_DIR},NVIS_NEMO_CONT=${NVIS_NEMO_CONT},NVIS_NEMO_XRACK=${NVIS_NEMO_XRACK},NVIS_NCCL_PRELOAD_DISABLE=${NVIS_NCCL_PRELOAD_DISABLE:-0},NVIS_NCCL_IGNORE_COLLNET_MISMATCH=${NVIS_NCCL_IGNORE_COLLNET_MISMATCH:-1},NVIS_NCCL_DEBUG=${NVIS_NCCL_DEBUG:-},NVIS_NEMO_MAX_STEPS=${NVIS_NEMO_MAX_STEPS:-},NVIS_NEMO_DTYPE=${NVIS_NEMO_DTYPE:-},NVIS_NEMO_MODEL_SIZE=${NVIS_NEMO_MODEL_SIZE:-},NVIS_NEMO_GBS=${NVIS_NEMO_GBS:-},NVIS_NEMO_UB_SKIPMC=${NVIS_NEMO_UB_SKIPMC:-},NVIS_NEMO_NVLS_ENABLE=${NVIS_NEMO_NVLS_ENABLE:-},NVIS_NEMO_RECIPE_DEFAULT=${NVIS_NEMO_RECIPE_DEFAULT:-},NVIS_NCCL_PRELOAD_SRC=${NVIS_NCCL_PRELOAD_SRC:-},NVIS_NCCL_NET_PLUGIN=${NVIS_NCCL_NET_PLUGIN:-},NVIS_NEMO_CLI=${NVIS_NEMO_CLI:-},NVIS_NEMO_GPU=${NVIS_NEMO_GPU:-},NVIS_NEMO_FORCE_BF16_ALIAS=${NVIS_NEMO_FORCE_BF16_ALIAS:-},NVIS_NEMO_RECIPE_NAME=${NVIS_NEMO_RECIPE_NAME:-},NVIS_NEMO_HF_HOME=${NVIS_NEMO_HF_HOME:-},NVIS_HF_HOME_CONT=${NVIS_HF_HOME_CONT:-}" \
  --output="${_out_path}" \
  "$SCRIPT_DIR/${NVIS_NEMO_BATCH_SCRIPT}"
