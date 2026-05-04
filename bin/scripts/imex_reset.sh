#!/bin/bash

rack="$1"
if [[ -z "$rack" ]]; then
  echo "usage: $0 <rack id>" >&2
  exit 2
fi

pdsh -w "${rack}-ct[01-18]" 'dcgm_service=cuda-dcgm; systemctl cat cuda-dcgm >/dev/null 2>&1 || dcgm_service=nvidia-dcgm; systemctl stop cmd; systemctl restart "${dcgm_service}"; sleep 2; systemctl start cmd; sleep 10; systemctl restart nvidia-imex'
