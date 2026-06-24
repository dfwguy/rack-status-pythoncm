#!/bin/bash
# One sbatch per host under ./sn-llama/ in the current working directory (log_dir when invoked from nvis-rack-status).
# Usage: after ``module load slurm``, pass the same Slurm nodelist style as run_singlerack.sh / validation, e.g.
#   ./run-llama.sh 'p6-r06-ct[01-03,15]'
# Multiple arguments are joined with commas (same merged hostlist as the UI).
#
# Requires: scontrol, pdsh, Slurm, NEMO_SQSH_SRC, and nemo-llma3_8b.sh next to this script.

set -uo pipefail

# Force ssh transport: this cluster's pdsh defaults to rsh (refused on compute nodes).
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no"
export PDSH_RCMD_TYPE=ssh

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

if [[ $# -lt 1 ]]; then
  echo "run-llama: need a Slurm hostlist (e.g. p6-r06-ct[01-03,15]) — same style as single-rack validation" >&2
  exit 1
fi

hostspec=$(printf '%s,' "$@" | sed 's/,$//')

# Same host expansion as run_singlerack.sh (no nodeset).
hosts=()
# shellcheck disable=SC2207
hosts=($(scontrol show hostname "$hostspec"))
if ((${#hosts[@]} == 0)); then
  echo "run-llama: scontrol show hostname produced no hosts for: $hostspec" >&2
  exit 1
fi

mkdir -p sn-llama

: "${NEMO_SQSH_SRC:?set NEMO_SQSH_SRC to the head-node path of the nemo .sqsh image (same as validation)}"
_sqsh_b=$(basename "$NEMO_SQSH_SRC")
pdsh -R ssh -w "$(echo "${hosts[@]}" | tr ' ' ',')" "mkdir -p /raid/data; rsync -ah --progress $NEMO_SQSH_SRC /raid/data/$_sqsh_b"

for i in "${hosts[@]}"; do
  [[ -z "$i" ]] && continue
  # Use the real hostname in -o so Slurm StdOut= and dashboard tail paths match (avoid literal %N).
  sbatch -N 1 -w "$i" -o "./sn-llama/${USER}-NEMO-LLAMA3_8B-1N-${i}-%j.txt" "$SCRIPT_DIR/nemo-llma3_8b.sh"
done
