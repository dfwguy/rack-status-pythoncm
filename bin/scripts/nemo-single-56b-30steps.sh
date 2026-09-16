#!/bin/bash
# Single-node NeMo 26.04.01 validation using the same Nemotron-H 56B engine as
# the rack preset. The shared engine normally accepts even node counts; this
# wrapper opts into its reviewed one-node path and fixes the campaign at 30 steps.
#SBATCH -p defq
#SBATCH --exclusive
#SBATCH --ntasks-per-node=4
#SBATCH --cpus-per-task=36
#SBATCH --gpus-per-node=4
#SBATCH --time=00:30:00
#SBATCH -N 1

set -euo pipefail
umask 0002

CONT="${NVIS_LLAMA_CONT:-/opt/containers/nvidia+nemo+26.04.01.sqsh}"
EXPECTED_CONT_SIZE="${NVIS_LLAMA_EXPECTED_SIZE:-42245001216}"
RUNTIME_DIR="${NVIS_NEMO_RUNTIME_DIR:-/cluster/OVA/scripts/nscaletx-16k-validation/runtime/bin/scripts}"
ENGINE="${RUNTIME_DIR}/nemo-nemotron5h_56b-mbridge-fp8.sh"

if [[ ! -r "$CONT" ]]; then
  echo "NEMO_VALIDATION_FAIL stage=image reason=missing path=$CONT"
  exit 2
fi
actual_cont_size=$(stat -c %s "$CONT") || {
  echo "NEMO_VALIDATION_FAIL stage=image reason=stat path=$CONT"
  exit 2
}
if [[ "$actual_cont_size" != "$EXPECTED_CONT_SIZE" ]]; then
  echo "NEMO_VALIDATION_FAIL stage=image reason=size path=$CONT expected=$EXPECTED_CONT_SIZE actual=$actual_cont_size"
  exit 2
fi
if [[ ! -r "$ENGINE" ]]; then
  echo "NEMO_VALIDATION_FAIL stage=engine reason=missing path=$ENGINE"
  exit 2
fi

export NVIS_NEMO_CONT="$CONT"
export NVIS_SCRIPTS_DIR="$RUNTIME_DIR"
export NVIS_NEMO_ALLOW_SINGLE_NODE=1
export NVIS_NEMO_XRACK=1
export NVIS_NCCL_MNNVL=0
export NVIS_NEMO_UB_SKIPMC=1
export NVIS_NEMO_NVLS_ENABLE=0
export NVIS_NCCL_TRACE_ROOT="${NVIS_NCCL_TRACE_ROOT:-${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-/cluster/OVA/logs/nscaletx-16k-validation}}/nccl_traces}"
export NVIS_NEMO_MAX_STEPS=30
export NVIS_NCCL_PREFLIGHT=allreduce
export NVIS_NCCL_PREFLIGHT_STRICT=1
export NVIS_NCCL_DEBUG=INFO

echo "NEMO_SINGLE_56B_START node=${SLURM_JOB_NODELIST} image=$CONT size=$actual_cont_size max_steps=30"
exec bash "$ENGINE"
