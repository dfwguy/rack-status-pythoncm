#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Audited allocation-scoped entry point for the containerized LLMB NCCL 24-test sweep.
set -euo pipefail
umask 0002

usage() {
  echo "Usage: $0 --nodelist HOSTLIST [--storage local|nfs] [--rsync|--no-rsync] [--log-dir PATH] [--dry-run]" >&2
}
nodelist=""; storage=local; rsync_enabled=1
shared_container_root="${RACK_VALIDATION_CONTAINER_ROOT:-/cluster/OVA/containers/nvftp-8k-known-good/20260824}"
configured_local_dir="${RACK_VALIDATION_NODE_CONTAINER_DIR:-/opt/containers}"
local_dir="$configured_local_dir"
log_dir="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-/cluster/OVA/logs/nscaletx-16k-validation}}"
dry_run=0
while (($#)); do
  case "$1" in
    --nodelist) nodelist="${2:?}"; shift 2 ;;
    --nodelist=*) nodelist="${1#*=}"; shift ;;
    --storage) storage="${2:?}"; shift 2 ;;
    --storage=*) storage="${1#*=}"; shift ;;
    --rsync) rsync_enabled=1; shift ;;
    --no-rsync) rsync_enabled=0; shift ;;
    --local-container-dir) local_dir="${2:?}"; shift 2 ;;
    --local-container-dir=*) local_dir="${1#*=}"; shift ;;
    --log-dir) log_dir="${2:?}"; shift 2 ;;
    --log-dir=*) log_dir="${1#*=}"; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[[ -n "$nodelist" ]] || { echo "ERROR: --nodelist is required" >&2; exit 2; }
[[ "$storage" == local || "$storage" == nfs ]] || { echo "ERROR: storage must be local or nfs" >&2; exit 2; }
[[ "$shared_container_root" == /* && "$shared_container_root" != *".."* ]] || { echo "ERROR: invalid RACK_VALIDATION_CONTAINER_ROOT" >&2; exit 2; }
[[ "$configured_local_dir" == /* && "$configured_local_dir" != *".."* ]] || { echo "ERROR: invalid RACK_VALIDATION_NODE_CONTAINER_DIR" >&2; exit 2; }
[[ "$local_dir" == "$configured_local_dir" ]] || { echo "ERROR: local container dir is fixed to $configured_local_dir" >&2; exit 2; }
[[ "$storage" == local || "$rsync_enabled" == 0 ]] || { echo "ERROR: rsync requires local storage" >&2; exit 2; }
[[ "$log_dir" == /* && "$log_dir" != *","* && "$log_dir" != *$'\n'* ]] || {
  echo "ERROR: --log-dir must be an absolute path without commas or newlines" >&2
  exit 2
}

# This launcher makes its log directory setgid/group-writable. Refuse account
# home directories so that operation can never break OpenSSH StrictModes.
reject_account_home_log_dir() {
  local requested="$1" resolved="$1" account_home resolved_home
  requested="${requested%/}"
  [[ -n "$requested" ]] || requested=/
  if [[ -d "$requested" ]]; then
    resolved="$(cd -P -- "$requested" 2>/dev/null && pwd -P)" || resolved="$requested"
  else
    resolved="$requested"
  fi
  if [[ "$resolved" == / || "$resolved" == /root || "$resolved" =~ ^/home/[^/]+$ ]]; then
    echo "ERROR: refusing to use an account home directory as validation log directory: $1" >&2
    return 2
  fi
  if command -v getent >/dev/null 2>&1; then
    while IFS=: read -r _ _ _ _ _ account_home _; do
      [[ "$account_home" == /* && -d "$account_home" ]] || continue
      resolved_home="$(cd -P -- "$account_home" 2>/dev/null && pwd -P)" || continue
      if [[ "$resolved" == "$resolved_home" ]]; then
        echo "ERROR: refusing to use an account home directory as validation log directory: $1" >&2
        return 2
      fi
    done < <(getent passwd 2>/dev/null || true)
  fi
}

reject_account_home_log_dir "$log_dir"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_image="${shared_container_root}/nvidia+nemo+26.04.01.sqsh"
local_image="${local_dir}/nvidia+nemo+26.04.01.sqsh"
run_image="$source_image"; [[ "$storage" == local ]] && run_image="$local_image"
expected_size=42245001216
expected_sha=e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca
expected_nccl=22903
expected_cuda=13.1
submit_uid="${NVIS_SLURM_SUBMIT_UID:-}"
submit_gid="${NVIS_SLURM_SUBMIT_GID:-}"
submit_home="${NVIS_SLURM_SUBMIT_HOME:-}"
slurm_submit_as=()
if [[ -n "$submit_uid" || -n "$submit_gid" ]]; then
  [[ "$submit_uid" =~ ^[0-9]+$ && "$submit_gid" =~ ^[0-9]+$ ]] || {
    echo "ERROR: NVIS_SLURM_SUBMIT_UID and NVIS_SLURM_SUBMIT_GID must both be numeric" >&2
    exit 2
  }
  slurm_submit_as=(--uid="$submit_uid" --gid="$submit_gid")
fi
if [[ -n "$submit_home" ]]; then
  [[ "$submit_home" == /* && "$submit_home" != *","* && "$submit_home" != *$'\n'* ]] || {
    echo "ERROR: NVIS_SLURM_SUBMIT_HOME must be an absolute path without commas or newlines" >&2
    exit 2
  }
  export HOME="$submit_home"
fi
llmb_install="${LLMB_INSTALL:-}"
llmb_run="${LLMB_RUN:-${NVIS_LLMB_RUN:-}}"
adapter=llmb
if [[ -z "$llmb_install" || ! -d "$llmb_install" || -z "$llmb_run" || ! -x "$llmb_run" ]]; then
  adapter=direct-five-collective
fi
# The LLMB 24-test adapter does not carry the reviewed rack/multi-rack transport
# gates.  Always use the source-locked five-collective adapter below.
adapter=direct-five-collective
provenance_root="${RACK_VALIDATION_PROVENANCE_ROOT:-/cluster/OVA/scripts/nscaletx-16k-validation}"
node_script_dir="${NVIS_VALIDATION_NODE_SCRIPTS_DIR:-${provenance_root}/runtime/bin/scripts}"
if [[ "$node_script_dir" != /* || "$node_script_dir" == /app || "$node_script_dir" == /app/* || \
      "$node_script_dir" == *","* || "$node_script_dir" == *$'\n'* ]]; then
  echo "ERROR: NVIS_VALIDATION_NODE_SCRIPTS_DIR must be a compute-visible absolute path outside /app" >&2
  exit 2
fi
snapshot_recipe="${provenance_root}/working_HLT_NEMO_NCCL_LLAMA/20260824/NCCL/recipes/single-node/nccl_five_collective.sbatch"
snapshot_recipe_sha=b923a98e913230f1a0d0d208aa411b72a373147fdfba05fd6e0fe0915a5138d1
direct_recipe="${node_script_dir}/nvftp_nccl_five_collective_20260824.sbatch"
direct_recipe_sha=fc61f51a55c0c193f940fcce828a59aeddecc4f203239e7042fd5b086bfc810b
if [[ "$adapter" == direct-five-collective ]]; then
  [[ -r "$direct_recipe" ]] || {
    echo "ERROR: LLMB is unavailable and the reviewed direct NCCL fallback is missing" >&2
    exit 2
  }
  if [[ -r "$snapshot_recipe" ]]; then
    [[ "$(sha256sum "$snapshot_recipe" | awk '{print $1}')" == "$snapshot_recipe_sha" ]] || {
      echo "ERROR: immutable NCCL snapshot failed provenance verification: $snapshot_recipe" >&2
      exit 2
    }
  fi
  [[ "$(sha256sum "$direct_recipe" | awk '{print $1}')" == "$direct_recipe_sha" ]] || {
    echo "ERROR: portable direct NCCL adapter failed provenance verification: $direct_recipe" >&2
    exit 2
  }
fi

print_cmd() { printf 'PLAN:'; printf ' %q' "$@"; printf '\n'; }
if (( dry_run )); then
  node_count="${NVIS_PREPARED_NODE_COUNT:?NVIS_PREPARED_NODE_COUNT is required for offline dry-run}"
  print_cmd sbatch "${slurm_submit_as[@]}" --wait --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=1 --gpus-per-node=4 \
    --export="ALL,NVIS_STAGE_SOURCE=${source_image},NVIS_STAGE_DEST=${run_image},NVIS_STAGE_RSYNC=${rsync_enabled},NVIS_STAGE_CONTAINER_PREFLIGHT=1,NVIS_STAGE_ENSURE_IMEX_CHANNEL0=1,NVIS_STAGE_EXPECTED_GPUS=4,NVIS_STAGE_EXPECTED_NCCL=${expected_nccl},NVIS_STAGE_EXPECTED_CUDA=${expected_cuda},NVIS_STAGE_EXPECTED_SIZE=${expected_size},NVIS_STAGE_EXPECTED_SHA256=${expected_sha}" \
    "${script_dir}/validation_container_stage.sbatch"
  if [[ "$adapter" == llmb ]]; then
    print_cmd env RUN_CONF_IMAGE="$run_image" PMIX_MCA_gds=hash MELLANOX_VISIBLE_DEVICES=all \
      NVIS_LLMB_AUDITED_CONTAINER=1 NVIS_FABRIC_SKIP=1 NVIS_LLMB_TESTSET_APPLY=0 \
      bash "${script_dir}/run_llmb_nccl.sh" --nodelist "$nodelist"
  else
    if [[ "$nodelist" == *"["* || "$nodelist" == *"]"* ]]; then
      command -v scontrol >/dev/null || {
        echo "ERROR: compact --nodelist requires scontrol for truthful dry-run expansion" >&2
        exit 2
      }
      mapfile -t dry_nodes < <(scontrol show hostnames "$nodelist")
      (( ${#dry_nodes[@]} == node_count )) || {
        echo "ERROR: compact --nodelist expanded to ${#dry_nodes[@]} nodes; expected $node_count" >&2
        exit 2
      }
      printf -v nodelist '%s,' "${dry_nodes[@]}"
      nodelist=${nodelist%,}
    else
      IFS=, read -r -a dry_nodes <<<"$nodelist"
    fi
    declare -A dry_rack_members=()
    dry_rack_order=()
    for node in "${dry_nodes[@]}"; do
      rack="${node%-*}"
      [[ -n "${dry_rack_members[$rack]:-}" ]] || dry_rack_order+=("$rack")
      dry_rack_members["$rack"]="${dry_rack_members[$rack]:-}${dry_rack_members[$rack]:+,}${node}"
    done
    if (( ${#dry_rack_order[@]} > 1 )); then
      print_cmd sbatch "${slurm_submit_as[@]}" --parsable --wait --no-requeue --job-name=nccl-multirack-hybrid \
        --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=4 --gpus-per-node=4 \
        --output="${log_dir%/}/nvftp-nccl-five-collective-%j.out" \
        --export="ALL,AUG21_REPORT_DIR=${log_dir},NVIS_IMAGE=${run_image},NVIS_NCCL_TRANSPORT_CONTRACT=MULTIRACK_HYBRID,NVIS_SCRIPTS_DIR=${node_script_dir}" "$direct_recipe"
    elif (( node_count == 1 )); then
      dry_contract=TRAY_NVLS
      print_cmd sbatch "${slurm_submit_as[@]}" --parsable --wait --no-requeue --job-name=nccl-8k32g-500 --nodes="$node_count" --nodelist="$nodelist" \
        --ntasks-per-node=4 --gpus-per-node=4 \
        --output="${log_dir%/}/nvftp-nccl-five-collective-%j.out" \
        --export="ALL,AUG21_REPORT_DIR=${log_dir},NVIS_IMAGE=${run_image},NVIS_NCCL_TRANSPORT_CONTRACT=${dry_contract},NVIS_SCRIPTS_DIR=${node_script_dir}" "$direct_recipe"
    else
      dry_contract=RACK_NVLS
      print_cmd sbatch "${slurm_submit_as[@]}" --parsable --wait --no-requeue --job-name=nccl-8k32g-500 --nodes="$node_count" --nodelist="$nodelist" \
        --ntasks-per-node=4 --gpus-per-node=4 \
        --output="${log_dir%/}/nvftp-nccl-five-collective-%j.out" \
        --export="ALL,AUG21_REPORT_DIR=${log_dir},NVIS_IMAGE=${run_image},NVIS_NCCL_TRANSPORT_CONTRACT=${dry_contract},NVIS_SCRIPTS_DIR=${node_script_dir}" "$direct_recipe"
    fi
  fi
  echo "LLMB_NCCL_CONTAINER_DRY_RUN nodes=$node_count image=$run_image adapter=$adapter recipe_snapshot=20260824 image_sha256=$expected_sha"
  exit 0
fi

# The Rack Status container deliberately carries no ambient Slurm client on its
# default PATH.  Initialise the site-pinned client before nodes.inc.sh calls
# scontrol; otherwise `set -e` exits here with no launcher output.
source "${script_dir}/slurm-init.inc.sh"
source "${script_dir}/lib/nodes.inc.sh"
nvis_nodes_resolve --nodelist "$nodelist"
nodelist="$NVIS_NODELIST"; node_count="$NVIS_NNODES"
mkdir -p "$log_dir" || { echo "ERROR: cannot create shared log directory: $log_dir" >&2; exit 2; }
[[ -d "$log_dir" && -w "$log_dir" && -x "$log_dir" ]] || {
  echo "ERROR: shared log directory is not writable/searchable: $log_dir" >&2
  exit 2
}
chmod g+rws "$log_dir" 2>/dev/null || true

stage_export="ALL,NVIS_STAGE_SOURCE=${source_image},NVIS_STAGE_DEST=${run_image},NVIS_STAGE_RSYNC=${rsync_enabled},NVIS_STAGE_CONTAINER_PREFLIGHT=1,NVIS_STAGE_ENSURE_IMEX_CHANNEL0=1,NVIS_STAGE_EXPECTED_GPUS=4,NVIS_STAGE_EXPECTED_NCCL=${expected_nccl},NVIS_STAGE_EXPECTED_CUDA=${expected_cuda},NVIS_STAGE_EXPECTED_SIZE=${expected_size},NVIS_STAGE_EXPECTED_SHA256=${expected_sha}"
stage_out="${log_dir%/}/llmb-nccl-container-stage-%j.out"
if [[ "$storage" == local && "$rsync_enabled" == 0 && -n "${NVIS_LOCAL_STAGE_VERIFIED_SHA256:-}" ]]; then
  [[ "$NVIS_LOCAL_STAGE_VERIFIED_SHA256" == "$expected_sha" ]] || {
    echo "ERROR: pre-staged image attestation SHA256 does not match the NCCL image allowlist" >&2
    exit 2
  }
fi
stage_text="$(sbatch "${slurm_submit_as[@]}" --parsable --wait --no-requeue --nodes="$node_count" --nodelist="$nodelist" \
  --ntasks-per-node=1 --gpus-per-node=4 --output="$stage_out" --export="$stage_export" \
  "${script_dir}/validation_container_stage.sbatch")"
stage_id="${stage_text%%;*}"
[[ "$stage_id" =~ ^[0-9]+$ ]] || { echo "ERROR: invalid stage job id: $stage_text" >&2; exit 3; }
echo "SUBMITTED jobid=$stage_id kind=container-stage output=${stage_out//%j/$stage_id}"

if [[ "$adapter" == direct-five-collective ]]; then
  direct_out="${log_dir%/}/nvftp-nccl-five-collective-%j.out"
  mapfile -t exact_nodes < <(scontrol show hostnames "$nodelist")
  declare -A rack_members=()
  rack_order=()
  for node in "${exact_nodes[@]}"; do
    rack="${node%-*}"
    [[ -n "${rack_members[$rack]:-}" ]] || rack_order+=("$rack")
    rack_members["$rack"]="${rack_members[$rack]:-}${rack_members[$rack]:+,}${node}"
  done

  if (( ${#rack_order[@]} > 1 )); then
    direct_contract=MULTIRACK_HYBRID
  elif (( node_count == 1 )); then
    direct_contract=TRAY_NVLS
  else
    direct_contract=RACK_NVLS
  fi
  set +e
  direct_text="$(sbatch "${slurm_submit_as[@]}" --parsable --wait --no-requeue --job-name=nccl-8k32g-500 --nodes="$node_count" --nodelist="$nodelist" \
    --ntasks-per-node=4 --gpus-per-node=4 --output="$direct_out" \
    --export="ALL,AUG21_REPORT_DIR=${log_dir},NVIS_IMAGE=${run_image},NVIS_NCCL_TRANSPORT_CONTRACT=${direct_contract},NVIS_SCRIPTS_DIR=${node_script_dir}" "$direct_recipe" 2>&1)"
  direct_rc=$?
  set -e
  direct_id="${direct_text%%;*}"
  printf '%s\n' "$direct_text"
  [[ "$direct_id" =~ ^[0-9]+$ ]] && \
    echo "SUBMITTED jobid=$direct_id kind=direct-five-collective output=${direct_out//%j/$direct_id}"
  [[ "$direct_id" =~ ^[0-9]+$ ]] || {
    echo "LLMB_NCCL_CONTAINER_FAIL invalid direct fallback job id: $direct_text" >&2
    exit 44
  }
  direct_record="$(sacct -X -n -P -j "$direct_id" -o JobIDRaw,State,ExitCode | awk -F'|' -v id="$direct_id" '$1 == id {print; exit}')"
  IFS='|' read -r _ direct_state direct_exit <<<"$direct_record"
  printf 'COMPLETE: direct-five-collective jobid=%s state=%s exit=%s\n' \
    "$direct_id" "${direct_state:-missing}" "${direct_exit:-missing}"
  (( direct_rc == 0 )) || {
    echo "LLMB_NCCL_CONTAINER_FAIL adapter=direct-five-collective jobid=$direct_id rc=$direct_rc state=${direct_state:-missing} exit=${direct_exit:-missing}" >&2
    exit "$direct_rc"
  }
  [[ "$direct_state" == COMPLETED && "$direct_exit" == 0:0 ]] || {
    echo "LLMB_NCCL_CONTAINER_FAIL adapter=direct-five-collective jobid=$direct_id rc=0 state=${direct_state:-missing} exit=${direct_exit:-missing}" >&2
    exit 44
  }
  direct_log="${direct_out//%j/$direct_id}"
  echo "LLMB_NCCL_CONTAINER_PASS adapter=direct-five-collective contract=$direct_contract jobid=$direct_id nodes=$node_count ranks=$((node_count * 4)) tests=5 recipe_snapshot=20260824 image=$run_image image_sha256=$expected_sha output=$direct_log"
  exit 0
fi

export RUN_CONF_IMAGE="$run_image" PMIX_MCA_gds=hash MELLANOX_VISIBLE_DEVICES=all
export NVIS_LLMB_AUDITED_CONTAINER=1 NVIS_FORCE_EXPORT_VARS="RUN_CONF_IMAGE PMIX_MCA_gds MELLANOX_VISIBLE_DEVICES"
# Exact-node access on this cluster is allocation-only.  The stage/preflight
# allocation already checked the runtime and IB devices, so never perform the
# legacy head-node SSH fabric probe or rewrite the shared LLMB recipe config.
export NVIS_FABRIC_SKIP=1 NVIS_LLMB_TESTSET_APPLY=0 NVIS_LLMB_KEEP_UCX_ENV=0
export LLMB_INSTALL="$llmb_install"
export LLMB_REPO="${LLMB_REPO:-${LLMB_INSTALL}/llmb_repo}"
export LLMB_RUN="$llmb_run"
if [[ -n "${LLMB_VENV:-}" ]]; then
  export LLMB_VENV
else
  export LLMB_VENV="$(dirname "$(dirname "$llmb_run")")"
fi
export RACK_JOB_LOG_DIR="$log_dir"

set +e
llmb_output="$(bash "${script_dir}/run_llmb_nccl.sh" --nodelist "$nodelist" 2>&1)"
llmb_rc=$?
set -e
printf '%s\n' "$llmb_output"
job_id="$(printf '%s\n' "$llmb_output" | sed -n 's/^\([0-9][0-9]*\)\(;.*\)\{0,1\}$/\1/p' | tail -1)"
result_log="${log_dir%/}/llmb-nccl_nodes_${job_id}.out"
[[ -n "$job_id" ]] && echo "SUBMITTED jobid=$job_id kind=llmb-nccl-24test output=$result_log"
(( llmb_rc == 0 )) || { echo "LLMB_NCCL_CONTAINER_FAIL rc=$llmb_rc" >&2; exit "$llmb_rc"; }
[[ -n "$job_id" && -r "$result_log" ]] || { echo "LLMB_NCCL_CONTAINER_FAIL missing child job/output" >&2; exit 44; }
grep -q 'LLMB_NCCL_24TEST_PASS completed=24 failed=0' "$result_log" || {
  echo "LLMB_NCCL_CONTAINER_FAIL missing truthful 24-test pass marker" >&2; exit 44;
}
echo "LLMB_NCCL_CONTAINER_PASS nodes=$node_count ranks=$((node_count * 4)) tests=24 recipe=job147392-shape image=$run_image nccl=2.29.3 cuda=13.1 pmix_gds=hash-requested-scale-workaround image_provenance=fleet-staged-digest-embedded-runtime-match output=$result_log"
