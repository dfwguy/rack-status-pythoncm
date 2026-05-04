#!/bin/bash
# Full system factory reset on one NV switch (NVOS). Args: RACK_ID NVSWITCH_HOSTNAME
# Example: nvswitch_system_reset.sh p01-r01 p01-r01-nvsw-03
set -euo pipefail
RACK="${1:?rack id required}"
HOST="${2:?nvswitch hostname required}"
case "$HOST" in
"${RACK}"-nvsw-*) ;;
*)
	echo "${0##*/}: second argument must be ${RACK}-nvsw-* (got: ${HOST})" >&2
	exit 1
	;;
esac
pdsh -w "$HOST" "nv action reset system factory-default force"
