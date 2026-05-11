#!/bin/bash

# map DGX-GB300 HCA to core, so that processes have affinity to the HCA they are assigned.
# Assume we only run # of ranks that corresponds to the number of HCAs

#
# Use the following command to find core values to map:
#
# for mlx in `ls /sys/class/infiniband/mlx5_*/device/local_cpulist`; do echo -n "$mlx:  "; cat $mlx; done
#

if [ "${OMPI_COMM_WORLD_LOCAL_SIZE}" -eq 1 ]; then
  export UCX_NET_DEVICES=${HCA_TAG}:1
  taskset -c "$(cat "/sys/class/infiniband/${HCA_TAG}/device/local_cpulist")" "$@"
else
  case $OMPI_COMM_WORLD_LOCAL_RANK in
        0) core=0-71;      export UCX_NET_DEVICES=mlx5_0:1 ;;
        1) core=0-71;      export UCX_NET_DEVICES=mlx5_1:1 ;;
        2) core=72-143;    export UCX_NET_DEVICES=mlx5_2:1 ;;
        3) core=72-143;    export UCX_NET_DEVICES=mlx5_3:1 ;;
        # These are not necessarily correct:
        # ---------------------------------------------------
        4) core=72-143;    export UCX_NET_DEVICES=mlx5_4:1 ;;
        5) core=72-143;    export UCX_NET_DEVICES=mlx5_5:1 ;;
        # ---------------------------------------------------
  esac

  taskset -c "$core" "$@"
fi
