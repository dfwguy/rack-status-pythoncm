#!/bin/bash
# NVL72 stress variant of mnubergemm.sbatch: heavier CE, higher duty, ASCII --MM_force_cublas_algo.
# Adjust #SBATCH -N for your allocation (18 = full GB200 NVL72 cell at 4 ranks/node).

#SBATCH -J mnubergemm-stress
#SBATCH -p defq
#SBATCH --exclusive
#SBATCH --ntasks-per-node=4
#SBATCH --cpus-per-task=36
#SBATCH --gpus-per-node=4
#SBATCH --time=00:100:00
#SBATCH -N 18

module load slurm
cd ${SLURM_SUBMIT_DIR}

topdir=/home/cmsupport/workspace

source ${topdir}/hpcx-v2.26-gcc-doca_ofed-ubuntu24.04-cuda13-aarch64/hpcx-mt-init-ompi.sh
hpcx_load
export CUDA_HOME=${topdir}/cuda13
export NCCL_PATH=${topdir}/nccl_2.28.9-1+cuda12.9_aarch64
export PATH=${CUDA_HOME}/bin:$PATH
export LD_LIBRARY_PATH=${CUDA_HOME}/lib64:${NCCL_PATH}/lib:.:${LD_LIBRARY_PATH}

echo "NODELIST=$SLURM_JOB_NODELIST"
hosts=($(scontrol show hostname $SLURM_JOB_NODELIST))

nic=bond0
GPUS_PER_NODE=4

## Ref: https://confluence.nvidia.com/pages/viewpage.action?spaceKey=DCGM&title=Mndiag+and+mnubergemm+testing+on+GB200+Colorado+%28Equinix%29+cluster+8x+NVL72
# mnubergemm's variables
# https://nvbugspro.nvidia.com/bug/5634196
# https://nvbugspro.nvidia.com/bug/5554973
duration_sec=$[SLURM_JOB_END_TIME-SLURM_JOB_START_TIME-600]
[ $duration_sec -le 0 ] && duration_sec=86000
STD="--dynamic_adj --MM_max_workload 65536 --max_workload 65536 --time_to_run ${duration_sec} --workload GNC"
MM="--MM_M 0 --MM_N 4096 --MM_sm_count 112 --MM_type SX_SX_SSS --MM_force_cublas_algo SQAAABQAAAAiAAAAAQAAAAAAAAAAAAAAAAAAAAAACQAAAAAARdgBAAAAAAAAAAAAAAAAAE0AAAAAAAAAAAAAAA=="
NET="--NET_sm_count 32 --NET_link_order scatter_sum --NET_size 20480000"
CE="--CE_type H --CE_size 4194304"
FREQQ="--freq 400 --duty 0.98 --dual_mode --dual_freq 0.0013888889 --dual_duty 0.50"

set -x
#1. Check nodes status
timeout 120 bash ${topdir}/aihpc_val/tools/hc/sysinfo.sh
#2. Run mnubergemm
MNUBERGEMM=/cm/local/apps/cuda-dcgm/4.5.2/libexec/datacenter-gpu-manager-4/plugins/cuda13/mnubergemm
if [[ ! -x ${MNUBERGEMM} ]]; then
  MNUBERGEMM=/usr/libexec/datacenter-gpu-manager-4/plugins/cuda13/mnubergemm
fi
ldd ${MNUBERGEMM}

# Same transport / NCCL policy as prior mpirun -x; srun inherits env via --export=ALL.
export SLURM_CPU_BIND=none
export NCCL_NVLS_ENABLE=1
export NCCL_DEBUG=INFO
export NCCL_P2P_LEVEL=NVL
export NCCL_CUMEM_ENABLE=1
export NCCL_CUMEM_HOST_ENABLE=0
export NCCL_MNNVL_ENABLE=1
export NCCL_MIN_CTAS=64
export NCCL_IB_DISABLE=1
export UCX_TLS=tcp
export NCCL_SOCKET_IFNAME=${nic}
export UCX_NET_DEVICES=${nic}
export NCCL_IB_HCA=${nic}
export OMPI_ALLOW_RUN_AS_ROOT=1
export OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1
export OMPI_MCA_pml=ucx
export OMPI_MCA_coll_hcoll_enable=0
export OMPI_MCA_btl=^openib,smcuda
export OMPI_MCA_btl_tcp_if_include=${nic}
export OMPI_MCA_oob_tcp_if_include=${nic}
export OMPI_MCA_btl_openib_warn_default_gid_prefix=0

# Task layout comes from #SBATCH (--ntasks-per-node × nodes); matches former mpirun -np.
srun --export=ALL --cpu-bind=none --mpi=pmix --label \
  "${MNUBERGEMM}" ${STD} ${FREQQ} ${MM} ${NET} ${CE}
#3. Check nodes' healthiness, drain if any issue found
pdsh -R ssh -f 32 -w $(echo ${hosts[*]} | tr ' ' ',') <<- EOF
${PWD}/../../tools/hc/checker.sh -e|grep -v PASS||true
EOF
set +x

# export MELLANOX_VISIBLE_DEVICES=all
# CONT=/raid/data/nvidian+nvis+mnubergemm+tensorrt-25.08-v1.12.1-arm.sqsh
# srun --cpu-bind=none --mpi=pmix --container-image="$CONT" ./mnubergemm ${STD} ${FREQQ} ${MM} ${NET} ${CE}
