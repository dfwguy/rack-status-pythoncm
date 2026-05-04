#!/bin/bash
# Multinode NEMO launch. Pass rack targets as one comma-separated spec or multiple arguments, e.g.
#   p5-r[13-14]-ct[01-18],p5-r[15-16]-ct[01-18]
# Cwd should be the log dir (nvis-rack-status) so relative sbatch --output lands there.
#
# Slurm stdout basename (example):
#   root-NEMO-p5-r[13-14]-ct[01-18],p5-r[15-16]-ct[01-18]-72N-22403.txt

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Filename tag: same specs as passed in (comma-separated if multiple argv).
spec_tag=""
for _a in "$@"; do
  [[ -n "$spec_tag" ]] && spec_tag+=","
  spec_tag+="$_a"
done

a=( $(nodeset --expand "$@") )
if ((${#a[@]} == 0)); then
  echo "run-nemo-xrack: nodeset --expand produced no nodes for: $*" >&2
  exit 1
fi

echo "Hosts: ${spec_tag}"

hosts=""
for r in "${a[@]}"; do
  hosts="${hosts} $(scontrol show hostname "$r")"
done
hosts=( $hosts )

n=$(( ${#hosts[*]} / 2 * 2 ))

: "${NEMO_SQSH_SRC:?set NEMO_SQSH_SRC to the head-node path of the nemo .sqsh image to copy under /raid/data/ on compute nodes}"
_sqsh_b=$(basename "$NEMO_SQSH_SRC")
pdsh -w "$(echo "${hosts[*]}" | tr ' ' ',')" "mkdir -p /raid/data; rsync -ah --progress $NEMO_SQSH_SRC /raid/data/$_sqsh_b"

_out_stem="root-NEMO-${spec_tag}-${n}N"

sbatch -N "${n}" \
  -w "$(echo "${hosts[*]}" | tr ' ' ',')" \
  -t 03:00:00 \
  --job-name="NEMO-${n}N" \
  --output="${_out_stem}-%j.txt" \
  "$SCRIPT_DIR/nemo-nemotron5h_56b-mbridge-fp8.sh"
