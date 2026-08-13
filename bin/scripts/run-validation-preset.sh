#!/usr/bin/env bash
# Audited dispatcher for Rack Status validation presets.  The browser chooses only
# a preset and exact targets; commands/images/version gates stay code-owned here.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: run-validation-preset.sh --preset ID --nodelist HOSTLIST
       [--storage nfs|local] [--rsync] [--local-container-dir /tmp/containers]
       [--log-dir PATH] [--dry-run]
EOF
}

preset=""; nodelist=""; storage="nfs"; rsync_enabled=0
local_container_dir="/tmp/containers"; log_dir="${RACK_STATUS_LOG_DIR:-$PWD}"; dry_run=0
while (($#)); do
  case "$1" in
    --preset) preset="${2:?missing --preset value}"; shift 2 ;;
    --preset=*) preset="${1#*=}"; shift ;;
    --nodelist) nodelist="${2:?missing --nodelist value}"; shift 2 ;;
    --nodelist=*) nodelist="${1#*=}"; shift ;;
    --storage) storage="${2:?missing --storage value}"; shift 2 ;;
    --storage=*) storage="${1#*=}"; shift ;;
    --rsync) rsync_enabled=1; shift ;;
    --local-container-dir) local_container_dir="${2:?missing --local-container-dir value}"; shift 2 ;;
    --local-container-dir=*) local_container_dir="${1#*=}"; shift ;;
    --log-dir) log_dir="${2:?missing --log-dir value}"; shift 2 ;;
    --log-dir=*) log_dir="${1#*=}"; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$preset" ]] || { echo "ERROR: --preset is required" >&2; exit 2; }
[[ -n "$nodelist" ]] || { echo "ERROR: --nodelist is required" >&2; exit 2; }
[[ "$storage" == nfs || "$storage" == local ]] || { echo "ERROR: storage must be nfs or local" >&2; exit 2; }
[[ "$local_container_dir" == /tmp/containers ]] || { echo "ERROR: local container directory is fixed to /tmp/containers" >&2; exit 2; }
[[ "$storage" == local || "$rsync_enabled" == 0 ]] || { echo "ERROR: --rsync requires --storage local" >&2; exit 2; }

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if (( dry_run )); then
  node_count="${NVIS_PREPARED_NODE_COUNT:?NVIS_PREPARED_NODE_COUNT is required for offline dry-run}"
  IFS=, read -r -a nodes <<<"$nodelist"
else
# shellcheck disable=SC1091
  [[ -f "${script_dir}/slurm-init.inc.sh" ]] && source "${script_dir}/slurm-init.inc.sh"
# shellcheck disable=SC1091
  source "${script_dir}/lib/nodes.inc.sh"
  command -v scontrol >/dev/null || { echo "ERROR: scontrol not found" >&2; exit 2; }
  command -v sbatch >/dev/null || { echo "ERROR: sbatch not found" >&2; exit 2; }
  nvis_nodes_resolve --nodelist "$nodelist"
  nodelist="$NVIS_NODELIST"
  node_count="$NVIS_NNODES"
  mapfile -t nodes < <(scontrol show hostnames "$nodelist")
  mkdir -p "$log_dir"
fi

print_cmd() { printf 'PLAN:'; printf ' %q' "$@"; printf '\n'; }
run_cmd() {
  print_cmd "$@"
  (( dry_run )) || "$@"
}

stage_image() {
  local source_image="$1" local_image="$2" expected_size="$3" expected_sha="$4" needs_ib="$5" run_image
  if [[ "$storage" == local ]]; then run_image="$local_image"; else run_image="$source_image"; fi
  STAGE_DEPENDENCY=()
  if [[ "$storage" == nfs ]]; then
    print_cmd verify-container --path "$source_image" --size "$expected_size" --sha256 "$expected_sha"
    if (( ! dry_run )); then
      [[ "$(stat -c %s "$source_image" 2>/dev/null || true)" == "$expected_size" ]] || { echo "ERROR: NFS image size gate failed: $source_image" >&2; exit 2; }
      [[ "$(sha256sum "$source_image" | awk '{print $1}')" == "$expected_sha" ]] || { echo "ERROR: NFS image SHA256 gate failed: $source_image" >&2; exit 2; }
    fi
    if [[ "$needs_ib" == 1 ]]; then
      local preflight="${script_dir}/validation_container_preflight.sbatch"
      run_cmd sbatch --wait --no-requeue --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=1 --gpus-per-node=4 \
        --output="${log_dir%/}/validation-container-preflight-%j.out" \
        --export="ALL,NVIS_PREFLIGHT_IMAGE=${source_image}" "$preflight"
    fi
    return
  fi
  local stage_script="${script_dir}/validation_container_stage.sbatch"
  [[ -r "$stage_script" ]] || { echo "ERROR: stage script missing: $stage_script" >&2; exit 2; }
  local export_set="ALL,NVIS_STAGE_SOURCE=${source_image},NVIS_STAGE_DEST=${run_image},NVIS_STAGE_RSYNC=${rsync_enabled},NVIS_STAGE_CONTAINER_PREFLIGHT=${needs_ib},NVIS_STAGE_EXPECTED_SIZE=${expected_size},NVIS_STAGE_EXPECTED_SHA256=${expected_sha},NVIS_SCRIPTS_DIR=${script_dir}"
  local output="${log_dir%/}/validation-container-stage-%j.out"
  local cmd=(sbatch --parsable --wait --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=1 --gpus-per-node=4
    --output="$output" --export="$export_set" "$stage_script")
  print_cmd "${cmd[@]}"
  if (( ! dry_run )); then
    local submitted stage_id
    submitted="$("${cmd[@]}")"; stage_id="${submitted%%;*}"
    [[ "$stage_id" =~ ^[0-9]+$ ]] || { echo "ERROR: unable to parse stage job id: $submitted" >&2; exit 2; }
    echo "STAGE_SUBMITTED jobid=$stage_id output=$output"
    echo "STAGE_COMPLETE jobid=$stage_id output=$output"
  fi
}

llmb_install="${LLMB_INSTALL:-/home/cmods/workspace/llmb}"
llmb_repo="${LLMB_REPO:-${llmb_install}/llmb_repo}"
llmb_run="${LLMB_RUN:-${NVIS_LLMB_RUN:-/home/cmods/workspace/llmb_venv/bin/llmb-run}}"

case "$preset" in
  nccl-2.31.2)
    [[ "$storage" == nfs && "$rsync_enabled" == 0 ]] || {
      echo "ERROR: nccl-2.31.2 is a direct host runtime and has no container to stage" >&2; exit 2;
    }
    export NCCL_DEBUG="${NCCL_DEBUG:-INFO}"
    export NVIS_NCCL_RUNTIME_PREFLIGHT=1 NVIS_NCCL_MIN_CUDA_VERSION=13.3 NVIS_NCCL_SBATCH_WAIT=1
    run_cmd bash "${script_dir}/run_nccl_mpi.sh" --nodelist "$nodelist" \
      --nccl-version 2.31.2 --rounds 5 --size-max 16G
    ;;

  llama31-405b-fp8)
    (( node_count == 256 )) || { echo "ERROR: Llama 3.1 405B preset requires exactly 256 nodes/1024 GPUs; got ${node_count}" >&2; exit 2; }
    source_image="/cluster/containers/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh"
    local_image="${local_container_dir}/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    node_sha="$(printf '%s\n' "${nodes[@]}" | sort | sha256sum | awk '{print $1}')"
    llama_batch="${script_dir}/llama405b_fp8_1024.sbatch"
    [[ -r "$llama_batch" ]] || { echo "ERROR: 1024-GPU Llama batch missing: $llama_batch" >&2; exit 2; }
    export_set="ALL,NVIS_LLAMA_SOURCE_IMAGE=${source_image},NVIS_LLAMA_RUN_IMAGE=${image},NVIS_LLAMA_STORAGE=${storage},NVIS_LLAMA_RSYNC=${rsync_enabled},NVIS_LLAMA_EXPECTED_NODE_SHA256=${node_sha},NVIS_LLAMA_SCRIPTS_DIR=${script_dir},NVIS_LLAMA_LOG_DIR=${log_dir}"
    run_cmd sbatch --wait --no-requeue --nodes=256 --nodelist="$nodelist" --ntasks-per-node=4 --gpus-per-node=4 \
      --output="${log_dir%/}/llama405b-fp8-1024-%j.out" --export="$export_set" "$llama_batch"
    ;;

  gpt-oss-cpu)
    (( node_count >= 1 && node_count <= 256 )) || { echo "ERROR: GPT-OSS CPU fan-out requires 1-256 exact nodes; got ${node_count}" >&2; exit 2; }
    source_image="/cluster/llmb/images/tensorrt-llm+release+1.1.0rc5.sqsh"
    local_image="${local_container_dir}/tensorrt-llm+release+1.1.0rc5.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    stage_image "$source_image" "$local_image" 30284881920 80de1820829418a3789a4ff5ce0c661f669ffb07c013552ac648822b9c1f9e72 0
    cpu_launch="${llmb_repo}/microbenchmarks/cpu_overhead/launch.sh"
    [[ -r "$cpu_launch" || "$dry_run" == 1 ]] || { echo "ERROR: GPT-OSS CPU launch script missing: $cpu_launch" >&2; exit 2; }
    model="${llmb_install}/workloads/microbenchmark_cpu_overhead/gpt-oss-120b/metal/model.bin"
    [[ -r "$model" || "$dry_run" == 1 ]] || { echo "ERROR: GPT-OSS model is missing/unreadable: $model" >&2; exit 2; }
    gpt_batch="${script_dir}/gpt-oss-cpu-strict.sbatch"
    export_set="ALL,LLMB_INSTALL=${llmb_install},LLMB_REPO=${llmb_repo},RUN_CONF_IMAGE=${image},GPU_TYPE=gb300,NUM_PROMPTS=100000,LLMB_EXPERIMENTS_ROOT=${log_dir}"
    # Keep the validated recipe single-node, but submit one independently
    # attributable job for every exact selected node. Submit the full fan-out
    # before waiting so a rack does not serialize eighteen CPU microbenchmarks.
    gpt_job_ids=()
    for node in "${nodes[@]}"; do
      gpt_cmd=(sbatch --parsable --no-requeue "${STAGE_DEPENDENCY[@]}" --nodelist="$node" --nodes=1 --gpus-per-node=4
        --job-name="gpt-oss-${node}" --output="${log_dir%/}/gpt-oss-cpu-${node}-%j.out"
        --export="$export_set" "$gpt_batch")
      print_cmd "${gpt_cmd[@]}"
      if (( ! dry_run )); then
        submitted="$("${gpt_cmd[@]}")"
        gpt_id="${submitted%%;*}"
        [[ "$gpt_id" =~ ^[0-9]+$ ]] || { echo "ERROR: unable to parse GPT-OSS job id: $submitted" >&2; exit 2; }
        gpt_job_ids+=("$gpt_id")
        echo "SUBMITTED: gpt-oss node=${node} jobid=${gpt_id}"
      fi
    done
    if (( ! dry_run )); then
      gpt_csv=$(IFS=,; printf '%s' "${gpt_job_ids[*]}")
      while squeue -h -j "$gpt_csv" 2>/dev/null | grep -q .; do sleep 5; done
      gpt_failed=0
      gpt_seen=0
      while IFS='|' read -r gpt_id gpt_state gpt_exit _rest; do
        [[ "$gpt_id" =~ ^[0-9]+$ ]] || continue
        gpt_seen=$((gpt_seen + 1))
        printf 'COMPLETE: gpt-oss jobid=%s state=%s exit=%s\n' "$gpt_id" "$gpt_state" "$gpt_exit"
        [[ "$gpt_state" == COMPLETED && "$gpt_exit" == 0:0 ]] || gpt_failed=1
      done < <(sacct -X -n -P -j "$gpt_csv" -o JobIDRaw,State,ExitCode)
      (( gpt_seen == ${#gpt_job_ids[@]} )) || gpt_failed=1
      (( gpt_failed == 0 )) || { echo "ERROR: one or more GPT-OSS node jobs failed" >&2; exit 2; }
    fi
    ;;

  exemplar-system-info)
    [[ -d "${llmb_repo}/microbenchmarks/system_info" || "$dry_run" == 1 ]] || {
      echo "ERROR: v26.06.01 system_info recipe missing under LLMB_REPO=${llmb_repo}" >&2; exit 2;
    }
    (( dry_run )) || git -C "$llmb_repo" rev-parse -q --verify 'v26.06.01^{commit}' >/dev/null || {
      echo "ERROR: LLMB_REPO has no v26.06.01 tag" >&2; exit 2;
    }
    (( dry_run )) || git -C "$llmb_repo" diff --quiet v26.06.01 -- microbenchmarks/system_info || {
      echo "ERROR: installed system_info recipe differs from NVIDIA v26.06.01" >&2; exit 2;
    }
    source_image="/cluster/containers/nvidia+nemo+26.04.01-arm.sqsh"
    local_image="${local_container_dir}/nvidia+nemo+26.04.01-arm.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    stage_image "$source_image" "$local_image" 42245001216 55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc 1
    sysinfo_runner="${script_dir}/run-exemplar-system-info.sh"
    run_cmd bash "$sysinfo_runner" --nodelist "$nodelist" --image "$image" --log-dir "$log_dir" \
      "${STAGE_DEPENDENCY[@]}"
    ;;

  nemo-26.04.01)
    source_image="/cluster/containers/nvidia+nemo+26.04.01-arm.sqsh"
    local_image="${local_container_dir}/nvidia+nemo+26.04.01-arm.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    stage_image "$source_image" "$local_image" 42245001216 55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc 1
    nemo_xrack="${NVIS_PRESET_SCOPE_MULTIRACK:-0}"
    export_set="ALL,NVIS_NEMO_CONT=${image},NVIS_SCRIPTS_DIR=${script_dir},NVIS_NEMO_XRACK=${nemo_xrack},NVIS_NEMO_MAX_STEPS=50"
    run_cmd sbatch --wait --no-requeue "${STAGE_DEPENDENCY[@]}" --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=4 --gpus-per-node=4 --cpus-per-task=36 \
      --chdir="$script_dir" --output="${log_dir%/}/nemo-26.04.01-%j.out" --export="$export_set" "${script_dir}/nemo-nemotron5h_56b-mbridge-fp8.sh"
    ;;

  mnubergemm-26.03-v1.17)
    source_image="/cluster/containers/mnubergemm-26.03-v1.17-arm.sqsh"
    local_image="${local_container_dir}/mnubergemm-26.03-v1.17-arm.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    stage_image "$source_image" "$local_image" 13949554688 9832dda6e80c8719e74f81ce617cac3abb77399e48d5385a38e86a040cad93e6 0
    export_set="ALL,NVIS_MNUBERGEMM_CONT=${image},NVIS_BIN_SCRIPTS=${script_dir},MNUBERGEMM_TIME_TO_RUN=300"
    run_cmd sbatch --wait --no-requeue "${STAGE_DEPENDENCY[@]}" --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=4 --gpus-per-node=4 --cpus-per-task=36 \
      --chdir="$script_dir" --output="${log_dir%/}/mnubergemm-26.03-v1.17-%j.out" --export="$export_set" "${script_dir}/mnubergemm.sbatch"
    ;;

  *) echo "ERROR: unsupported preset: $preset" >&2; exit 2 ;;
esac

if (( dry_run )); then
  echo "VALIDATION_PRESET_DRY_RUN preset=${preset} nodes=${node_count} storage=${storage} rsync=${rsync_enabled}"
else
  echo "VALIDATION_PRESET_LAUNCHED preset=${preset} nodes=${node_count} storage=${storage} rsync=${rsync_enabled}"
fi
