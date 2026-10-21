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
# GPT-OSS-only CPU overhead validation with strict tokenization result gates.
#SBATCH --exclusive
#SBATCH --job-name=gpt-oss-cpu
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=4
#SBATCH --time=00:30:00

set -euo pipefail
: "${LLMB_INSTALL:?}"
: "${LLMB_REPO:?}"
: "${RUN_CONF_IMAGE:?}"
: "${LLMB_EXPERIMENTS_ROOT:?}"
: "${NVIS_GPT_CPU_ARCH:?}"
[[ "$(uname -m)" == "$NVIS_GPT_CPU_ARCH" ]] || { echo "GPT_OSS_FAIL CPU architecture mismatch" >&2; exit 2; }
readonly NUM_PROMPTS="${NUM_PROMPTS:-100000}"
readonly WORKLOAD="${LLMB_INSTALL}/workloads/microbenchmark_cpu_overhead"
readonly MODEL="${WORKLOAD}/gpt-oss-120b"
readonly TRT_DIR="${WORKLOAD}/TensorRT-LLM"
readonly KERNEL_SCRIPT="${LLMB_REPO}/microbenchmarks/cpu_overhead/pytorch_kernel_launch_latency.py"
readonly RESULT_DIR="${LLMB_EXPERIMENTS_ROOT%/}/microbenchmark_cpu_overhead/experiments/cpu_overhead_tests/${SLURM_JOB_ID}"
readonly DATASET="${RESULT_DIR}/dataset_1000_1000_${NUM_PROMPTS}_${SLURM_JOB_ID}.txt"
mkdir -p "$RESULT_DIR"
[[ -r "$KERNEL_SCRIPT" ]] || { echo "GPT_OSS_FAIL missing kernel benchmark" >&2; exit 3; }
[[ -r "${MODEL}/tokenizer.json" && -r "${MODEL}/tokenizer_config.json" ]] || { echo "GPT_OSS_FAIL missing GPT-OSS tokenizer" >&2; exit 4; }

mounts="${WORKLOAD}:${WORKLOAD}:ro,${LLMB_REPO}:${LLMB_REPO}:ro,${LLMB_EXPERIMENTS_ROOT}:${LLMB_EXPERIMENTS_ROOT}"
srun --kill-on-bad-exit=1 --output="${RESULT_DIR}/kernel_launch_%N_%j.out" --error="${RESULT_DIR}/kernel_launch_%N_%j.err" \
  --container-image="$RUN_CONF_IMAGE" --container-mounts="$mounts" --container-writable --no-container-mount-home \
  bash -euo pipefail -c 'cd "$2"; numactl --cpunodebind=0 --membind=0 python "$1" --start_size 4 --end_size 512 --iters 1000000; test -s gemm_benchmark_results.csv' _ "$KERNEL_SCRIPT" "$RESULT_DIR"

srun --kill-on-bad-exit=1 --output="${RESULT_DIR}/tokenization_%N_%j.out" --error="${RESULT_DIR}/tokenization_%N_%j.err" \
  --container-image="$RUN_CONF_IMAGE" --container-mounts="$mounts" --container-writable --no-container-mount-home \
  bash -euo pipefail -c '
    dataset_script="$1/benchmarks/cpp/prepare_dataset.py"
    [[ -r "$dataset_script" ]] || dataset_script=/app/tensorrt_llm/benchmarks/cpp/prepare_dataset.py
    start_time=$(date +%s)
    python "$dataset_script" --stdout --tokenizer "$2" token-norm-dist \
      --input-mean 1000 --output-mean 1000 --input-stdev 0 --output-stdev 0 \
      --num-requests "$3" > "$4"
    test -s "$4"
    lines=$(wc -l < "$4")
    [[ "$lines" == "$3" ]] || { echo "GPT_OSS_FAIL dataset_lines=$lines expected=$3" >&2; exit 5; }
    echo "GPT_OSS_TOKENIZATION_PASS dataset_lines=$lines dataset=$4"
    echo "Tokenization time: $(($(date +%s) - start_time))s"
  ' _ "$TRT_DIR" "$MODEL" "$NUM_PROMPTS" "$DATASET"
echo "GPT_OSS_CPU_PASS node=$(hostname) num_prompts=$NUM_PROMPTS"
