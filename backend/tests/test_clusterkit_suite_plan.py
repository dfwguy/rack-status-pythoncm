# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Exercise the offline planner with traps for scheduler/tool calls and writes.

Standalone: python3 -m unittest discover -s backend/tests -p test_clusterkit_suite_plan.py -v
No backend imports or pytest conftest are needed.
"""

from pathlib import Path
import importlib.util
import json
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"
NODES = "sys1-gso1-p-phy-gpu1-su19,sys1-gso1-p-phy-gpu2-su19"


class ExplicitNodePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nc-ck-plan-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / "repo" / "bin" / "scripts"
        self.scripts.mkdir(parents=True)
        self.runner = self.scripts / "run_clusterkit_suite.sh"
        self.runner.write_bytes((SCRIPTS / self.runner.name).read_bytes())
        batch = "clusterkit_suite_explicit_2_node.sbatch"
        (self.scripts / batch).write_bytes((SCRIPTS / batch).read_bytes())
        for name in ("clusterkit_suite_explicit_rank.sh", "clusterkit_suite_node_diag.py"):
            (self.scripts / name).write_bytes((SCRIPTS / name).read_bytes())
        self.calls = self.root / "unsafe-calls"
        trap = 'printf "%s\\n" "$0" >> "$PLAN_TEST_CALLS"; exit 99\n'
        for name in ("slurm-init.inc.sh", "workspace-dir-common.sh", "clusterkit_suite_common.inc.sh"):
            (self.scripts / name).write_text(trap)
        self.init = self.root / "workspace" / "hpcx" / "hpcx-init.sh"
        self.init.parent.mkdir(parents=True)
        self.init.write_text(trap)
        self.traps = self.root / "traps"
        self.traps.mkdir()
        for name in ("sbatch", "srun", "salloc", "sinfo", "squeue", "scontrol",
                     "module", "mpirun", "clusterkit", "clusterkit.sh", "hpcx_load",
                     "mkdir", "mv", "rm", "chmod", "touch"):
            stub = self.traps / name
            stub.write_text("#!/bin/bash\n" + trap)
            stub.chmod(0o700)
        timeout = self.traps / "timeout"
        timeout.write_text(
            "#!/bin/bash\n"
            "while (($#)); do\n"
            "  case \"$1\" in\n"
            "    --kill-after=*) shift ;;\n"
            "    --) shift; break ;;\n"
            "    *s) shift; break ;;\n"
            "    *) break ;;\n"
            "  esac\n"
            "done\n"
            "exec \"$@\"\n"
        )
        timeout.chmod(0o700)
        self.env = {
            "PATH": f"{self.traps}:/usr/bin:/bin",
            "HOME": str(self.root),
            "PLAN_TEST_CALLS": str(self.calls),
            # Legacy switches must not accidentally enable archives/full-SU jobs.
            "FORCE": "1", "PARALLEL": "1", "EXCLUDE_RACKS": "01,02",
        }

    def invoke(self, args, env=None):
        before = {p.relative_to(self.root): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}
        result = subprocess.run(
            ["/bin/bash", str(self.runner), *args], env=env or self.env,
            cwd=self.root, text=True, capture_output=True, timeout=5,
        )
        after = {p.relative_to(self.root): p.read_bytes()
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after, "planner changed files or called a trap")
        self.assertFalse(self.calls.exists(), result.stdout + result.stderr)
        self.assertFalse((self.root / "repo" / "results").exists())
        return result

    def options(self):
        return ["--nodelist", NODES, "--hpcx-init", str(self.init), "--label", "NC-su19"]

    def command(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return shlex.split(result.stdout.split("SBATCH PLAN (not executed):\n", 1)[1])

    def test_plan_pins_exact_nodes_paths_and_five_minute_limit_without_side_effects(self):
        cmd = self.command(self.invoke(["plan", *self.options()]))
        self.assertEqual(cmd[0], "sbatch")
        self.assertIn("--nodes=2", cmd)
        self.assertIn(f"--nodelist={NODES}", cmd)
        self.assertIn("--partition=defq", cmd)
        self.assertIn("--time=00:05:00", cmd)
        exports = next(arg for arg in cmd if arg.startswith("--export="))
        self.assertIn(f"NVIS_CLUSTERKIT_HPCX_INIT={self.init}", exports)
        self.assertIn(f"WORKSPACE_DIR={self.root / 'workspace'}", exports)
        self.assertIn("RACK_CLUSTERKIT_SUITE_GID_POLICY=global", exports)
        self.assertIn("RACK_CLUSTERKIT_SUITE_LOG_LEVEL=info", exports)
        self.assertIn("RACK_CLUSTERKIT_SUITE_PATH_CHECK=0", exports)
        self.assertIn(f"RACK_CLUSTERKIT_SUITE_OUTDIR={self.root / 'repo/results/NC-su19/2_node_test'}", exports)
        self.assertEqual(cmd[-1], str(self.scripts / "clusterkit_suite_explicit_2_node.sbatch"))
        self.assertIn("--gres=none", cmd)
        self.assertIn("--no-requeue", cmd)
        self.assertNotIn("--exclusive", cmd)

    def test_run_dry_run_matches_plan(self):
        plan = self.command(self.invoke(["plan", *self.options()]))
        dry = self.command(self.invoke(["run", "--dry-run", *self.options()]))
        self.assertEqual(plan, dry)

    def test_explicit_run_cannot_submit_without_dry_run(self):
        result = self.invoke(["run", *self.options()])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("execution is disabled", result.stderr)

    def test_live_run_requires_a_device_and_bounded_duration(self):
        for extra in (["--execute"], ["--execute", "--hca", "roce_vf_r1:1", "--time", "00:05:01"],
                      ["--execute", "--hca", "roce_vf_r1:1,mlx5_12:1"],
                      ["--execute", "--hca", "roce_vf_r1:0"]):
            with self.subTest(extra=extra):
                result = self.invoke(["run", *self.options(), *extra])
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Submitted job", result.stdout)

    def test_plan_and_dry_run_override_execute_flag(self):
        for mode in (["plan"], ["run", "--dry-run"]):
            cmd = self.command(self.invoke([*mode, *self.options(), "--execute", "--hca", "roce_vf_r1:1"]))
            exports = next(arg for arg in cmd if arg.startswith("--export="))
            self.assertIn("RACK_CLUSTERKIT_SUITE_HCA=roce_vf_r1:1", exports)
            self.assertIn(f"RACK_CLUSTERKIT_SUITE_EXPECTED_NODES={NODES.replace(',', ':')}", exports)

    def test_authorized_submission_is_one_job_and_never_reuses_results(self):
        # The only scheduler here is a shell stub. No real client is reachable.
        (self.scripts / "slurm-init.inc.sh").write_text("# test-only no-op\n")
        (self.traps / "mkdir").unlink()
        (self.traps / "sbatch").write_text(
            '#!/bin/bash\nprintf "%s\\n" "$@" >> "$PLAN_TEST_CALLS"\nprintf "123456\\n"\n'
        )
        args = ["/bin/bash", str(self.runner), "run", *self.options(), "--execute", "--hca", "roce_vf_r1:1"]
        result = subprocess.run(args, env=self.env, cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = self.root / "repo/results/NC-su19/2_node_test"
        self.assertEqual((out / "jobid.txt").read_text(), "123456\n")
        calls = self.calls.read_text()
        self.assertIn(f"--nodelist={NODES}\n", calls)
        self.assertIn("--nodes=2\n", calls)
        self.assertIn("--gres=none\n", calls)
        self.assertNotIn("--exclusive", calls)
        second = subprocess.run(args, env=self.env, cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already exists", second.stderr)
        self.assertEqual(self.calls.read_text(), calls, "second invocation submitted another job")
        self.assertEqual((out / "jobid.txt").read_text(), "123456\n")

    def test_batch_passes_one_output_path_and_only_cpu_tests_to_clusterkit(self):
        # Exercise the real batch body with fake Slurm and a fake native binary.
        # The preflight step is acknowledged by the stub; no host/device access.
        output = self.root / "batch-results"
        output.mkdir()
        argv = self.root / "clusterkit-argv"
        pkg = self.init.parent
        binary = pkg / "clusterkit/bin/clusterkit"
        binary.parent.mkdir(parents=True)
        binary.write_text(
            '#!/bin/bash\nprintf "CK_OUTPUT_SUBDIR=%s\\n" "${CK_OUTPUT_SUBDIR-unset}" > "$CK_TEST_ARGV"\n'
            'printf "%s\\n" "$@" >> "$CK_TEST_ARGV"\n'
        )
        binary.chmod(0o700)
        self.init.write_text('export HPCX_DIR="$CK_TEST_PACKAGE"\nhpcx_load() { :; }\n')
        (self.scripts / "clusterkit_suite_node_diag.py").write_text(
            'import os, sys\n'
            'if sys.argv[1] == "gid": print(5)\n'
            'elif sys.argv[1] == "exec-bound": os.execv(sys.argv[3], sys.argv[3:])\n'
            'elif sys.argv[1] == "summarize": pass\n'
            'else: raise SystemExit(98)\n'
        )
        scontrol = self.traps / "scontrol"
        scontrol.write_text('#!/bin/bash\nprintf "%s\\n" "sys1-gso1-p-phy-gpu1-su19" "sys1-gso1-p-phy-gpu2-su19"\n')
        srun = self.traps / "srun"
        srun.write_text(
            '#!/bin/bash\nif [[ "$1" == --mpi=none ]]; then exit 0; fi\n'
            'while (($#)) && [[ "$1" != bash ]]; do shift; done\n'
            '[[ "${1:-}" == bash ]] || exit 98\nshift\nexec /bin/bash "$@"\n'
        )
        env = dict(self.env, SLURM_JOB_NUM_NODES="2", SLURM_JOB_NODELIST=NODES,
                   SLURM_JOB_ID="123456", NVIS_CLUSTERKIT_HPCX_INIT=str(self.init),
                   NVIS_BIN_SCRIPTS=str(self.scripts), SLURM_PROCID="0",
                   RACK_CLUSTERKIT_SUITE_GID_POLICY="auto",
                   RACK_CLUSTERKIT_SUITE_OUTDIR=str(output), RACK_CLUSTERKIT_SUITE_HCA="roce_vf_r1:1",
                   RACK_CLUSTERKIT_SUITE_EXPECTED_NODES=NODES.replace(",", ":"),
                   CK_OUTPUT_SUBDIR="/wrong/inherited/path", CK_TEST_ARGV=str(argv), CK_TEST_PACKAGE=str(pkg))
        result = subprocess.run(["/bin/bash", str(self.scripts / "clusterkit_suite_explicit_2_node.sbatch")],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = argv.read_text().splitlines()
        self.assertEqual(args[0], "CK_OUTPUT_SUBDIR=unset")
        self.assertEqual([arg for arg in args if arg.startswith("--outputdir=")],
                         [f"--outputdir={output}/baseline"])
        self.assertNotIn("/wrong/inherited/path", "\n".join(args))
        self.assertEqual([args[i + 1] for i, arg in enumerate(args) if arg == "-d"], ["bw", "lat"])
        self.assertEqual((output / "status.txt").read_text(),
                         "baseline_EXIT=0\nPRIMARY_EXIT=0 GLOBAL_GID_EXIT=not-run TCP_CONTROL_EXIT=not-run\n")
        # The NC default must resolve a per-host GID instead of relying on UCX auto.
        other_output = self.root / "batch-results-global"
        other_output.mkdir()
        env["RACK_CLUSTERKIT_SUITE_GID_POLICY"] = "global"
        env["RACK_CLUSTERKIT_SUITE_OUTDIR"] = str(other_output)
        second = subprocess.run(["/bin/bash", str(self.scripts / "clusterkit_suite_explicit_2_node.sbatch")],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("GID_ENV=5", second.stdout)
        self.assertIn(f"--outputdir={other_output}/baseline", argv.read_text())

    def test_global_gid_selection_matches_each_nodes_configured_ip(self):
        spec = importlib.util.spec_from_file_location("node_diag", self.scripts / "clusterkit_suite_node_diag.py")
        diag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diag)
        for index, address in [(3, "2a03:83e4:410e:1200::1"), (5, "2a03:83e4:410e:1201::1")]:
            with self.subTest(index=index):
                rows = [{"index": 1, "gid": "fe80::1234", "type": "RoCE v2"},
                        {"index": 2, "gid": address, "type": "IB/RoCE v1"},
                        {"index": index, "gid": address, "type": "RoCE v2"}]
                interfaces = [{"addr_info": [{"local": address}, {"local": "fe80::1234"}]}]
                self.assertEqual(diag.select_global_gid(rows, interfaces)["index"], index)
        with self.assertRaises(RuntimeError):
            diag.select_global_gid([{"index": 1, "gid": "fe80::1234", "type": "RoCE v2"}],
                                   [{"addr_info": [{"local": "fe80::1234"}]}])

    def test_failure_diagnostics_stay_inside_one_batch_and_preserve_primary_failure(self):
        output = self.root / "batch-results"
        output.mkdir()
        self.calls = self.root / "stage-calls"
        (self.traps / "scontrol").write_text(
            '#!/bin/bash\nprintf "%s\\n" "sys1-gso1-p-phy-gpu1-su19" "sys1-gso1-p-phy-gpu2-su19"\n'
        )
        (self.traps / "srun").write_text(
            '#!/bin/bash\nif [[ "$1" == --mpi=none ]]; then exit 0; fi\n'
            'printf "%s\\n" "$RACK_CLUSTERKIT_SUITE_STAGE" >> "$PLAN_TEST_CALLS"\n'
            'if [[ "$RACK_CLUSTERKIT_SUITE_STAGE" == baseline ]]; then exit 7; fi\nexit 0\n'
        )
        env = dict(self.env, PLAN_TEST_CALLS=str(self.calls), SLURM_JOB_NUM_NODES="2",
                   SLURM_JOB_NODELIST=NODES, SLURM_JOB_ID="123456", NVIS_BIN_SCRIPTS=str(self.scripts),
                   NVIS_CLUSTERKIT_HPCX_INIT=str(self.init), RACK_CLUSTERKIT_SUITE_OUTDIR=str(output),
                   RACK_CLUSTERKIT_SUITE_HCA="roce_vf_r1:1",
                   RACK_CLUSTERKIT_SUITE_EXPECTED_NODES=NODES.replace(",", ":"))
        result = subprocess.run(["/bin/bash", str(self.scripts / "clusterkit_suite_explicit_2_node.sbatch")],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.calls.read_text().splitlines(), ["baseline", "global-gid", "tcp-control"])
        self.assertIn("PRIMARY_EXIT=7 GLOBAL_GID_EXIT=0 TCP_CONTROL_EXIT=0", (output / "status.txt").read_text())
        # A failed optional path comparison must not produce a successful job.
        (self.traps / "srun").write_text(
            '#!/bin/bash\nif [[ "$1" == --mpi=none ]]; then exit 0; fi\n'
            'printf "%s\\n" "$RACK_CLUSTERKIT_SUITE_STAGE" >> "$PLAN_TEST_CALLS"\n'
            'if [[ "$RACK_CLUSTERKIT_SUITE_STAGE" == single-path ]]; then exit 8; fi\nexit 0\n'
        )
        path_output = self.root / "path-results"
        path_output.mkdir()
        env.update(RACK_CLUSTERKIT_SUITE_OUTDIR=str(path_output), RACK_CLUSTERKIT_SUITE_PATH_CHECK="1")
        second = subprocess.run(["/bin/bash", str(self.scripts / "clusterkit_suite_explicit_2_node.sbatch")],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(second.returncode, 8)
        self.assertEqual(self.calls.read_text().splitlines()[-2:], ["baseline", "single-path"])
        self.assertIn("SINGLE_PATH_EXIT=8", (path_output / "status.txt").read_text())

    def test_invalid_selections_fail_without_scheduler_calls(self):
        bad = ["", "node1", "node1,node2,node3", "node1,node1", "node1,NODE1",
               "node1,node2,", ",node1,node2", "node1,,node2", "node1, node2",
               "node[1-2],node3", "node1;touch injected,node2", "$(touch injected),node2"]
        for nodelist in bad:
            with self.subTest(nodelist=nodelist):
                result = self.invoke(["plan", "--nodelist", nodelist, "--hpcx-init", str(self.init)])
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("SBATCH PLAN", result.stdout)

    def test_invalid_options_fail_closed(self):
        for extra in (["--label", "../other"], ["--partition", "defq,other"],
                      ["--time", "00:00:00"], ["--time", "00:60:00"],
                      ["--hpcx-init", "/missing/hpcx-init.sh"],
                      ["--results-root", "relative"], ["--results-root", "/tmp/a,b"],
                      ["--gid-policy", "3"],
                      ["--log-level", "trace"],
                      ["--submit"], ["--nodelist"]):
            with self.subTest(extra=extra):
                result = self.invoke(["plan", *self.options(), *extra])
                self.assertNotEqual(result.returncode, 0)

    def test_auto_gid_policy_is_explicit_in_the_plan(self):
        cmd = self.command(self.invoke(["plan", *self.options(), "--gid-policy", "auto"]))
        exports = next(arg for arg in cmd if arg.startswith("--export="))
        self.assertIn("RACK_CLUSTERKIT_SUITE_GID_POLICY=auto", exports)

    def test_path_comparison_and_diagnostic_logging_are_explicit(self):
        cmd = self.command(self.invoke(["plan", *self.options(), "--path-check", "--log-level", "debug"]))
        exports = next(arg for arg in cmd if arg.startswith("--export="))
        self.assertIn("RACK_CLUSTERKIT_SUITE_PATH_CHECK=1", exports)
        self.assertIn("RACK_CLUSTERKIT_SUITE_LOG_LEVEL=debug", exports)

    def test_cpu_mapping_rejects_smt_and_nonidentity_numbering(self):
        spec = importlib.util.spec_from_file_location("node_diag", self.scripts / "clusterkit_suite_node_diag.py")
        diag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diag)
        diag.verify_identity_cpu_map("# CPU,Core,Socket\n0,0,0\n1,1,0\n2,2,1\n3,3,1")
        for topology in ("0,0,0\n1,0,0", "0,0,0\n2,1,0\n1,2,0", ""):
            with self.subTest(topology=topology), self.assertRaises(RuntimeError):
                diag.verify_identity_cpu_map(topology)
        self.assertEqual(diag.cpu_numbers("0-2,7"), [0, 1, 2, 7])
        with self.assertRaises(RuntimeError):
            diag.cpu_numbers("0-99999")

    def test_cpu_binding_uses_only_the_current_jobs_assigned_cpu(self):
        spec = importlib.util.spec_from_file_location("node_diag", self.scripts / "clusterkit_suite_node_diag.py")
        diag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diag)
        env = {"SLURM_JOB_ID": "123456", "SLURM_CPUS_PER_TASK": "1"}
        responses = [
            {"exit": 0, "stdout": "SlurmdParameters = (null)"},
            {"exit": 0, "stdout": "0,0,0\n1,1,0"},
            {"exit": 0, "stdout": "Nodes=node1 CPU_IDs=1 Mem=1024\n Nodes=node2 CPU_IDs=0"},
            {"exit": 0, "stdout": "node1"}, {"exit": 0, "stdout": "node2"},
        ]
        with mock.patch.dict(diag.os.environ, env), mock.patch.object(diag.socket, "gethostname", return_value="node1"), \
                mock.patch.object(diag.os, "sched_getaffinity", return_value={0, 1}, create=True), \
                mock.patch.object(diag, "command", side_effect=responses):
            self.assertEqual(diag.allocated_cpu(), 1)
        with mock.patch.dict(diag.os.environ, env), mock.patch.object(diag.socket, "gethostname", return_value="node1"), \
                mock.patch.object(diag.os, "sched_getaffinity", return_value={0}, create=True), \
                mock.patch.object(diag, "command", side_effect=responses), self.assertRaises(RuntimeError):
            diag.allocated_cpu()
        output = self.root / "placement"
        output.mkdir()
        env.update(RACK_CLUSTERKIT_SUITE_STAGE="baseline", SLURM_PROCID="0",
                   RACK_CLUSTERKIT_SUITE_HCA="roce_vf_r1:1")
        with mock.patch.dict(diag.os.environ, env), mock.patch.object(diag, "allocated_cpu", return_value=1), \
                mock.patch.object(diag.os, "sched_getaffinity", side_effect=[{0, 1}, {1}], create=True), \
                mock.patch.object(diag.os, "sched_setaffinity", create=True) as bind, mock.patch.object(diag.os, "execv") as execute:
            diag.exec_bound(["/fake/clusterkit", "--disable-retest"], output)
            bind.assert_called_once_with(0, {1})
            execute.assert_called_once_with("/fake/clusterkit", ["/fake/clusterkit", "--disable-retest"])
        self.assertEqual(json.loads((output / "rank-placement-baseline-0.json").read_text())["affinity_after"], [1])

    def test_bound_command_parser_preserves_vendor_options_after_separator(self):
        spec = importlib.util.spec_from_file_location("node_diag", self.scripts / "clusterkit_suite_node_diag.py")
        diag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diag)
        command = ["/fake/clusterkit", "-d", "bw", "--outputdir=/fake/result", "--disable-retest"]
        with mock.patch.object(diag, "exec_bound") as execute:
            diag.main(["exec-bound", "--outdir", str(self.root), "--", *command])
            execute.assert_called_once_with(command, self.root)

    def test_offline_summary_keeps_raw_ppn_and_records_actual_mpi_tasks(self):
        spec = importlib.util.spec_from_file_location("node_diag", self.scripts / "clusterkit_suite_node_diag.py")
        diag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diag)
        output = self.root / "summary-results"
        folder = output / "baseline" / "timestamp"
        folder.mkdir(parents=True)
        raw = {"Testname": "bandwidth", "Units": "MB/s", "Links": [[0, 56.5]],
               "PPN": 144, "Technology": "Unknown", "HCA_Tag": "roce_vf_r1:1"}
        file = folder / "bandwidth.json"
        file.write_text(json.dumps(raw))
        (output / "status.txt").write_text("baseline_EXIT=0\nPRIMARY_EXIT=0 GLOBAL_GID_EXIT=not-run TCP_CONTROL_EXIT=not-run\n")
        for rank in (0, 1):
            (output / f"rank-placement-baseline-{rank}.json").write_text(json.dumps({"rank": rank, "mpi_tasks_per_node": 1}))
        with mock.patch.object(diag, "command", side_effect=AssertionError("offline summary queried cluster")):
            diag.main(["summarize", "--outdir", str(output)])
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["stages"]["baseline"]["actual_mpi_tasks_per_node"], 1)
        self.assertEqual(summary["stages"]["baseline"]["metrics"][0]["vendor_ppn"], 144)
        self.assertEqual(summary["performance_acceptance"], "UNVERIFIED")
        self.assertEqual(json.loads(file.read_text()), raw)
        with self.assertRaises(FileExistsError):
            diag.summarize(output)

    def test_cli_paths_with_spaces_round_trip_as_single_arguments(self):
        output = self.root / "output with spaces"
        cmd = self.command(self.invoke(["plan", *self.options(), "--results-root", str(output)]))
        self.assertIn(f"--output={output}/NC-su19/2_node_test/slurm-2-node-%j.out", cmd)
        self.assertFalse(output.exists())

    def test_environment_overrides_and_cli_precedence(self):
        env = dict(self.env, NVIS_CLUSTERKIT_HPCX_INIT=str(self.init), DRY_RUN="1",
                   RACK_CLUSTERKIT_SUITE_RESULTS_ROOT=str(self.root / "env-results"),
                   RACK_CLUSTERKIT_SUITE_PARTITION="other")
        cmd = self.command(self.invoke(["run", "--nodelist", NODES], env))
        self.assertIn("--partition=other", cmd)
        self.assertIn(f"--output={self.root}/env-results/explicit-nodes/2_node_test/slurm-2-node-%j.out", cmd)
        cmd = self.command(self.invoke(["plan", *self.options(), "--partition", "defq",
                                      "--results-root", str(self.root / "cli-results")], env))
        self.assertIn("--partition=defq", cmd)
        self.assertIn(f"--output={self.root}/cli-results/NC-su19/2_node_test/slurm-2-node-%j.out", cmd)

    def test_help_does_not_initialize_legacy_runner(self):
        result = self.invoke(["plan", "--help"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("literal hostnames", result.stdout)


if __name__ == "__main__":
    unittest.main()
