# ClusterKit SU-Suite Integration

Rack Status includes two ClusterKit paths:

- `bin/scripts/run_clusterkit.sh`: existing single-rack ClusterKit sweep.
- `bin/scripts/run_clusterkit_suite.sh`: SU-scale IB validation suite adapted from the IB team's ClusterKit runner.

The SU suite keeps Slurm execution simple by submitting five explicit sbatch tiers:

- `clusterkit_suite_per_rack.sbatch`
- `clusterkit_suite_2_node.sbatch`
- `clusterkit_suite_2_node_cross_rack.sbatch`
- `clusterkit_suite_2_rack.sbatch`
- `clusterkit_suite_entire_su.sbatch`

`clusterkit_suite_common.inc.sh` contains the shared ClusterKit launch body. It reuses the existing Rack Status HPC-X, HCA profile, UCX, and mapper logic from the single-rack ClusterKit path.

## Usage

Preview an SU run:

```bash
DRY_RUN=1 ./bin/scripts/run_clusterkit_suite.sh run dh02 j SU10
```

Run the suite:

```bash
./bin/scripts/run_clusterkit_suite.sh run dh02 j SU10
```

Check existing results:

```bash
./bin/scripts/run_clusterkit_suite.sh check SU10
```

The backend API endpoint is:

```text
POST /api/run-clusterkit-suite-async
GET  /api/run-clusterkit-suite-status?job_id=<id>
```

POST body:

```json
{
  "domain": "dh02",
  "prefix": "j",
  "su_label": "SU10",
  "log_dir": "/path/to/rack-status/logs"
}
```

## Output

By default, results are stored under:

```text
<log_dir>/clusterkit-suite/<SU_LABEL>/
```

with one subdirectory per tier:

```text
per_rack/
2_node_test/
2_node_cross_rack_test/
2_rack_test/
entire_SU/
```

Each sbatch tier writes one ClusterKit result directory per HCA.

## Thresholds

The default checks match the IB runner policy:

- Bandwidth: `170000` to `200000` MB/s
- Latency: `1.0` to `7.0` usec
- Implausible readings are reported as glitches and excluded from pass/fail:
  - bandwidth above `1000000` MB/s
  - latency above `10000` usec

These can be overridden with:

```text
RACK_CLUSTERKIT_SUITE_BW_MIN
RACK_CLUSTERKIT_SUITE_BW_MAX
RACK_CLUSTERKIT_SUITE_LAT_MIN
RACK_CLUSTERKIT_SUITE_LAT_MAX
RACK_CLUSTERKIT_SUITE_EXPECTED_HCAS
RACK_CLUSTERKIT_SUITE_RESULTS_ROOT
```

