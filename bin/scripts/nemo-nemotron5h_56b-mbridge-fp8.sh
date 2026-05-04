#!/bin/bash
# Author: Junli Zhang<junliz@nvidia.com>
#
#SBATCH -p defq
#SBATCH --exclusive
#SBATCH --ntasks-per-node=4
#SBATCH --cpus-per-task=36
#SBATCH --gpus-per-node=4
#SBATCH --time=03:00:00
###SBATCH -N 2


#CONT=/home/cmsupport/workspace/nemo-25.11.01.sqsh
CONT=/raid/data/nemo-25.11.01.sqsh
SYSINFO_SH=${SLURM_SUBMIT_DIR}/sysinfo.sh

single_rack=1
[ $SLURM_NNODES -gt 18 ] && single_rack=0
if [ $(( ${SLURM_NNODES} % 2 )) != 0 ]
then
  echo "ERROR: SLURM_NNODES=$SLURM_NNODES is not multiple of 2!!!"
  exit 1
fi

module load slurm
cd ${SLURM_SUBMIT_DIR}

if [ -n "${SLURM_JOB_SCRIPT:-}" ]; then
  _job_dir="$(cd "$(dirname "$SLURM_JOB_SCRIPT")" && pwd)"
else
  _job_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

echo
echo "NODELIST: ${SLURM_JOB_NODELIST}"
echo

s_time=$(date +%s)
echo "INFO: cleaning work begin"
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no -o PreferredAuthentications=publickey"
pdsh -f 100 -R ssh -w ${SLURM_JOB_NODELIST} <<- 'EOF'|dshbak -c
pkill -9 python &>/dev/null || true
ps -ef|grep python|grep -E 'torch|nemo'|grep -v grep || true
enroot list -f|grep pyxis && enroot remove -f $(enroot list -f|grep pyxis) || true
rm -f /var/lib/systemd/coredump/* &>/dev/null || true
EOF
echo "INFO: cleaning work done"

echo "INFO: checking hardware status"
timeout 60 bash "${SYSINFO_SH}" 2>&1

# Prefer bond0 when it is up, else enP22s22f0np0 when up, else first existing interface, else bond0.
select_ns_nic() {
  local nic st
  for nic in bond0 enP22s22f0np0; do
    if [[ -r "/sys/class/net/${nic}/operstate" ]]; then
      st=$(tr -d '\n' <"/sys/class/net/${nic}/operstate")
      if [[ "$st" == "up" ]]; then
        printf '%s' "$nic"
        return 0
      fi
    fi
  done
  for nic in bond0 enP22s22f0np0; do
    if [[ -d "/sys/class/net/${nic}" ]]; then
      printf '%s' "$nic"
      return 0
    fi
  done
  printf '%s' "bond0"
}

ns_nic=$(select_ns_nic)
echo "INFO: ns_nic=${ns_nic} (auto: bond0 vs enP22s22f0np0)"
set -x
ew_nics=($(echo mlx5_{0..3}))
export MELLANOX_VISIBLE_DEVICES=all
export PMIX_MCA_gds=hash
#
if [ $single_rack -eq 1 ]; then
  export UCX_TLS=tcp
  export NCCL_IB_DISABLE=1
  export UCX_NET_DEVICES=${ns_nic}
else
  export UCX_TLS=rc
  export UCX_NET_DEVICES="$(echo ${ew_nics[@]}|tr ' ' '\n'|xargs -I {} echo {}:1|paste -s -d',')"
  export NCCL_IB_HCA="=$(echo ${ew_nics[@]}|tr ' ' '\n'|xargs -I {} echo {}|paste -s -d',')"
fi
export NCCL_SOCKET_IFNAME=${ns_nic}
export OMPI_MCA_btl_tcp_if_include=${ns_nic}
export OMPI_MCA_oob_tcp_if_include=${ns_nic}
export NVTE_UB_SOCKET_IFNAME=${ns_nic}
export GLOO_SOCKET_IFNAME=${ns_nic}
#
export OMPI_ALLOW_RUN_AS_ROOT=1
export OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1
export OMPI_MCA_btl_openib_warn_default_gid_prefix=0
export OMPI_MCA_coll_hcoll_enable=0
export SLURM_CPU_BIND=none
#
export NCCL_NVLS_ENABLE=1
#export NCCL_CUMEM_ENABLE=1
#export NCCL_CUMEM_HOST_ENABLE=0
export NCCL_MNNVL_ENABLE=1
export NCCL_MIN_CTAS=16
#export NCCL_P2P_LEVEL=NVL
#export NCCL_P2P_LEVEL=C2C
#export NCCL_NET_GDR_C2C=1
#export NCCL_NET_GDR_LEVEL=SYS
export NCCL_RUNTIME_CONNECT=0
export NCCL_IB_QPS_PER_CONNECTION=2
#export NCCL_IB_SPLIT_DATA_ON_QPS=0
#export NVSHMEM_DISABLE_GDRCOPY=1
export CUDA_MODULE_LOADING=EAGER
export NCCL_P2P_NET_CHUNKSIZE=262144
#export NCCL_GDRCOPY_ENABLE=1
#export NCCL_GDRCOPY_SYNC_ENABLE=1

srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null

## test nccl
export NCCL_DEBUG=WARN
export NCCL_NVLS_ENABLE=1
echo "*********************************************"
echo "* NCCL ALL-Reduce                           *"
echo "*********************************************"

timeout 300 \
srun --export=ALL --cpu-bind=none --mpi=pmix \
--container-name=nemo \
--container-writable \
--container-image=$CONT \
--container-mounts=/dev:/dev \
all_reduce_perf_mpi -dfloat -b 8 -f 2 -g 1 -e 16G --iters 20
exitcode=$?
if [ $exitcode -ne 0 ]; then
  echo "*********************************************"
  echo "* ERROR: All Reduce NCCL failed to run!!!              *"
  echo "*********************************************"
  srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
  exit 1
fi

sleep 10

echo "*********************************************"
echo "* NCCL ALL-to-ALL                            *"
echo "*********************************************"

timeout 300 \
srun --export=ALL --cpu-bind=none --mpi=pmix \
--container-name=nemo \
--container-writable \
--container-image=$CONT \
--container-mounts=/dev:/dev \
alltoall_perf_mpi -dfloat -b 8 -f 2 -g 1 -e 16G --iters 20
exitcode=$?
if [ $exitcode -ne 0 ]; then
  echo "*********************************************"
  echo "* ERROR: AllToAll NCCL failed to run!!!              *"
  echo "*********************************************"
  srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
  exit 1
fi


echo "*********************************************"
echo "* NCCL ALL-Gather                           *"
echo "*********************************************"

timeout 300 \
srun --export=ALL --cpu-bind=none --mpi=pmix \
--container-name=nemo \
--container-writable \
--container-image=$CONT \
--container-mounts=/dev:/dev \
all_gather_perf_mpi -dfloat -b 8 -f 2 -g 1 -e 16G --iters 20
exitcode=$?
if [ $exitcode -ne 0 ]; then
  echo "*********************************************"
  echo "* ERROR: All-Gather  NCCL failed to run!!!  *"
  echo "*********************************************"
  srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
  exit 1
fi


sleep 10
## generate config files
### Rule: num_gpus % (ep * et * tp * pp) == 0
#python3 /opt/Megatron-Bridge/scripts/performance/setup_experiment.py ... ... #the same args with run_script.py
#
########  run nemo test
#


echo "*********************************************"
echo "* NEMO5-H 56B LLM                           *"
echo "*********************************************"
sleep 10
## generate config files
### Rule: num_gpus % (ep * et * tp * pp) == 0
#python3 /opt/Megatron-Bridge/scripts/performance/setup_experiment.py ... ... #the same args with run_script.py
#
########  run nemo test
#
## Workaround for unstable or not robust enough NVLINK: 
# With unstable or not robust enough NVLINK, you may encounter error like:
# RuntimeError: /opt/TransformerEngine/transformer_engine/common/comm_gemm_overlap/userbuffers/userbuffers-host.cpp:331 in function create_communicator_grouped2: CUDA Error: unknown error
# To workaround this issue, we can set UB_SKIPMC=1 to disables the NVSwitch/CUDA multicast path and makes Userbuffers fall back to a slower IPC-based scheme. 
# It's usually better than turning off tp_comm_overlap entirely, but worse than full multicast on a healthy, supported fabric.
# UB_SKIPMC: UserBuffers Skip Multicast
# UB = UserBuffers (the TP-overlap communication backend in TransformerEngine)
# SKIPMC = skip multicast (don't use CUDA/NVLS multicast for UB collectives)
## export NCCL_NVLS_ENABLE=0
## export UB_SKIPMC=1
### For optimal performance, it's recommended to keep NVLS enabled and let UB use multicast if possible.
export NCCL_DEBUG=INFO
export NCCL_NVLS_ENABLE=1
export UB_SKIPMC=0
# export MAX_STEPS=2010 #For stress test, you can set a large number for MAX_STEPS. 
#export MAX_STEPS=110
export MAX_STEPS=2010
srun --export=ALL --cpu-bind=none --mpi=pmix \
--container-name=nemo \
--container-writable \
--container-image=$CONT \
--container-mounts=/dev:/dev \
bash <<- '__END__'
set -ex
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HOME=/root/.cache/huggingface

export NEMORUN_HOME=/mnt/nemo
export CPATH=/usr/local/cuda/include
export TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas
export TRITON_LIB_DIR=/usr/local/cuda/lib64
export TORCH_NCCL_AVOID_RECORD_STREAMS=1

export NVTE_FLASH_ATTN=1
export NVTE_FUSED_ATTN=1
export CUDA_DEVICE_MAX_CONNECTIONS=1
export NVTE_FWD_LAYERNORM_SM_MARGIN=16
export NVTE_BWD_LAYERNORM_SM_MARGIN=16
export NCCL_P2P_NET_CHUNKSIZE=2097152
export TORCH_NCCL_HIGH_PRIORITY=1
export PYTHONPATH=/opt/Megatron-Bridge/scripts/performance:$PYTHONPATH

## Rule: num_gpus % (ep * et * tp * pp) == 0
#With CUDA graphs, then expandable segments must be disabled by "unset PYTORCH_CUDA_ALLOC_CONF".
#For arguments explanation, please refer to 
# https://github.com/NVIDIA-NeMo/Megatron-Bridge/blob/main/scripts/performance/argument_parser.py
#For model name and model size, please refer to 
# https://github.com/NVIDIA-NeMo/Megatron-Bridge/blob/main/scripts/performance/configs/nemotronh/nemotronh_workload_base_configs.py
numactl --cpunodebind=$[SLURM_LOCALID/4] --membind=$[SLURM_LOCALID/4] \
python /opt/Megatron-Bridge/scripts/performance/run_script.py \
  --account root \
  --partition defq \
  --log_dir ${NEMORUN_HOME} \
  --container_image ${NEMORUN_HOME}/nemo-25.11.01.sqsh \
  --nemo_home ${NEMORUN_HOME}/experiments \
  --gpu gb300 \
  --num_gpus $[4*${SLURM_NNODES}] \
  --gpus_per_node 4 \
  --compute_dtype fp8_cs \
  --model_name nemotronh --model_size 56b \
  --use_megatron_fsdp 0 \
  -tp 2 \
  -pp 1 \
  -cp 1 \
  -ep 1 \
  -mb 1 \
  -gb $[192*${SLURM_NNODES}/16] \
  --cuda_graph_impl transformer_engine --cuda_graph_scope mamba,attn \
  --max_steps ${MAX_STEPS} \
  train.manual_gc=true train.manual_gc_interval=100
__END__

srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
set +x

e_time=$(date +%s)
echo "INFO: timelimit=$[(SLURM_JOB_END_TIME-SLURM_JOB_START_TIME)](s), real_took=$[e_time-s_time](s)"

if [ -n "${HC_CHECKER_SCRIPT:-}" ] && [ -x "$HC_CHECKER_SCRIPT" ]; then
pdsh -f 100 -R ssh -w ${SLURM_JOB_NODELIST} <<- EOF|dshbak -c
bash $HC_CHECKER_SCRIPT -e 1 -d
EOF
else
  echo "INFO: HC_CHECKER_SCRIPT not set or not executable; skipping post-run checker"
fi
