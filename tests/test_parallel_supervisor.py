"""Pure supervisor planning and canonical worker argv contracts."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import loop as loop_mod
from engine import parallel
from engine import parallel_gate
from engine import parallel_spool
from engine import parallel_state
from engine import repo_executor


RUN_ID = "a1b2c3d4"
DISPATCH_TOKEN = "supervisor-dispatch-token"


def run_config(repo: Path) -> dict:
    return {
        "repo": str(repo),
        "goal": "goal.md",
        "plan_doc": "PLAN.md",
        "agent_cmd": "agent --model test",
        "validate_cmd": "validator --all",
        "notify_cmd": "",
        "flag_threshold": 2,
        "done_threshold": 3,
        "red_limit": 4,
        "stall_limit": 5,
        "stuck_stop": True,
        "stuck_stop_count": 6,
        "round_timeout": 7.5,
        "agent_backoff_max": 8.5,
        "validate_timeout": 9.5,
        "max_parallel": 2,
        "worker_restart_limit": 3,
    }


def assignment(repo: Path) -> dict:
    return {
        "run_id": RUN_ID,
        "parent_workspace": "base",
        "assigned_order": 2,
        "worker_workspace": f"base--{RUN_ID}-task-2",
        "worker_repo": str(repo),
        "task_ref": f"refs/heads/loop/{RUN_ID}/task-2",
        "integration_ref": f"refs/heads/loop/{RUN_ID}/integration",
        "gate_command": "python -m engine.parallel_gate --run-dir fixed",
        "run_config_hash": "1" * 64,
        "launch_spec_hash": "2" * 64,
        "manifest_hash": "3" * 64,
        "dispatch_token_hash": parallel_state.dispatch_token_hash(DISPATCH_TOKEN),
    }


def reservation(*, resume: bool = False) -> dict:
    return {
        "schema": 1,
        "request_id": "4" * 32,
        "run_id": RUN_ID,
        "task": 2,
        "manifest_hash": "3" * 64,
        "run_config_hash": "1" * 64,
        "launch_spec_hash": "2" * 64,
        "supervisor_session": "5" * 32,
        "supervisor_generation": 1,
        "attempt": 0,
        "resume": resume,
    }


class TestParallelPlanning(unittest.TestCase):
    def test_nonterminal_base_guard_is_pid_independent(self):
        state = {
            "runner": "parallel-supervisor",
            "loop": {"pid": None},
            "parallel": {"status": "paused"},
        }
        with self.assertRaisesRegex(parallel.ParallelError, "parallel resume/pause/abort"):
            parallel.assert_base_mutation_allowed(state, "edit")
        terminal = {**state, "parallel": {"status": "completed"}}
        parallel.assert_base_mutation_allowed(terminal, "edit")

    def test_parallel_status_fails_closed_on_unknown_enum(self):
        with self.assertRaisesRegex(parallel.ParallelError, "status"):
            parallel.parallel_run_status({
                "runner": "parallel-supervisor", "parallel": {"status": "mystery"},
            })

    def test_partition_batches_keeps_serial_tasks_and_contiguous_stacks(self):
        batches = parallel.partition_batches([
            {"order": 1, "task": "serial"},
            {"order": 2, "task": "a", "stack": 4},
            {"order": 3, "task": "b", "stack": 4},
            {"order": 4, "task": "serial again"},
            {"order": 5, "task": "c", "stack": 9},
        ])
        self.assertEqual([batch.number for batch in batches], [1, 2, 3, 4])
        self.assertEqual([batch.orders for batch in batches], [
            (1,), (2, 3), (4,), (5,),
        ])

    def test_partition_batches_revalidates_stack_invariant(self):
        with self.assertRaisesRegex(parallel.ParallelError, "stack 1"):
            parallel.partition_batches([
                {"order": 1, "task": "a", "stack": 1},
                {"order": 2, "task": "serial"},
                {"order": 3, "task": "b", "stack": 1},
            ])

    def test_load_frozen_plan_normalizes_but_rejects_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / "plan.json"
            raw = json.dumps([
                {"order": 1, "task": "  first  ", "stack": 1},
            ]).encode("utf-8")
            plan.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            with mock.patch.object(
                    parallel.loop_mod, "_open_regular",
                    wraps=parallel.loop_mod._open_regular) as secure_open:
                loaded = parallel.load_frozen_plan(
                    plan, expected_raw_sha256=digest)
            self.assertEqual(loaded, [
                {"order": 1, "task": "first", "ref": None, "stack": 1},
            ])
            secure_open.assert_called_once_with(
                plan, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            with self.assertRaisesRegex(parallel.ParallelError, "SHA-256 mismatch"):
                parallel.load_frozen_plan(
                    plan, expected_raw_sha256="0" * 64)
            plan.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(parallel.ParallelError, "非空陣列"):
                parallel.load_frozen_plan(plan)

    def test_expected_plan_hash_requires_canonical_raw_sha256(self):
        for value in ("", "A" * 64, "g" * 64, "0" * 63, 42):
            with self.subTest(value=value), self.assertRaisesRegex(
                    parallel.ParallelError, "SHA-256"):
                parallel.normalize_expected_plan_sha256(value)


class TestCanonicalWorkerCommand(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "worktree"
        self.repo.mkdir()
        self.plan = self.root / "plan.json"
        self.plan.write_text("[]", encoding="utf-8")

    def test_initial_and_resume_use_one_exact_builder(self):
        initial = parallel.build_worker_argv(
            python_executable=sys.executable,
            assignment=assignment(self.repo), run_config=run_config(self.repo),
            plan_path=self.plan, dispatch_token=DISPATCH_TOKEN,
            launch_reservation=reservation(resume=False), resume=False,
        )
        resumed = parallel.build_worker_argv(
            python_executable=sys.executable,
            assignment=assignment(self.repo), run_config=run_config(self.repo),
            plan_path=self.plan, dispatch_token=DISPATCH_TOKEN,
            launch_reservation=reservation(resume=True), resume=True,
        )
        self.assertEqual(initial[:initial.index("--import-plan")],
                         resumed[:resumed.index("--managed-worker-resume")])
        self.assertEqual(initial[-4:], [
            "--import-plan", str(self.plan.resolve()), "--start-phase", "exec",
        ])
        self.assertEqual(resumed[-1], "--managed-worker-resume")
        self.assertIn("--stuck-stop", initial)
        self.assertNotIn("--notify-cmd", initial)
        self.assertNotIn("--allow-serial-stack", initial)

    def test_builder_rejects_noncanonical_identity(self):
        for field, bad in (
            ("task_ref", "refs/heads/main"),
            ("integration_ref", f"refs/heads/loop/{RUN_ID}/elsewhere"),
            ("worker_workspace", "other"),
        ):
            with self.subTest(field=field):
                spec = assignment(self.repo)
                spec[field] = bad
                with self.assertRaisesRegex(parallel.ParallelError, field):
                    parallel.build_worker_argv(
                        python_executable=sys.executable,
                        assignment=spec, run_config=run_config(self.repo),
                        plan_path=self.plan, dispatch_token=DISPATCH_TOKEN,
                        launch_reservation=reservation(),
                    )

    def test_builder_rejects_dispatch_token_not_bound_to_assignment(self):
        with self.assertRaisesRegex(parallel.ParallelError, "dispatch token"):
            parallel.build_worker_argv(
                python_executable=sys.executable,
                assignment=assignment(self.repo), run_config=run_config(self.repo),
                plan_path=self.plan, dispatch_token="forged",
                launch_reservation=reservation(),
            )

    def test_run_config_rejects_bool_integer_and_nonfinite_timeout(self):
        config = run_config(self.repo)
        config["max_parallel"] = True
        with self.assertRaisesRegex(parallel.ParallelError, "max_parallel"):
            parallel.canonical_run_config(config)
        config = run_config(self.repo)
        config["validate_timeout"] = float("nan")
        with self.assertRaisesRegex(parallel.ParallelError, "validate_timeout"):
            parallel.canonical_run_config(config)


class TestImmutableWorkerEnvironment(unittest.TestCase):
    def test_artifact_values_override_ambient_but_secret_value_is_name_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            frozen_bin = (root / "frozen-bin").resolve()
            ambient_bin = (root / "ambient-bin").resolve()
            contract = {
                "path_additions": [str(frozen_bin)],
                "non_secret": {"MODE": "frozen", "TRACE": False},
                "required_secret_names": ["API_TOKEN"],
            }
            secret = "must-never-be-serialized"
            environment = parallel.build_worker_environment(
                contract, workspace_root=root / "workspaces",
                ambient={
                    "PATH": str(ambient_bin), "MODE": "ambient",
                    "API_TOKEN": secret,
                },
            )
        self.assertEqual(
            environment["PATH"].split(os.pathsep)[:2],
            [str(frozen_bin), str(ambient_bin)],
        )
        self.assertEqual(environment["MODE"], "frozen")
        self.assertEqual(environment["TRACE"], "false")
        self.assertEqual(environment["API_TOKEN"], secret)
        self.assertNotIn(secret, json.dumps(contract, sort_keys=True))

    def test_missing_required_secret_is_reported_without_value_materialization(self):
        contract = {
            "path_additions": [], "non_secret": {},
            "required_secret_names": ["API_TOKEN", "SERVICE_TOKEN"],
        }
        self.assertEqual(
            parallel.missing_required_secret_names(
                contract, ambient={"API_TOKEN": "rotated", "SERVICE_TOKEN": ""}),
            ("SERVICE_TOKEN",),
        )
        with self.assertRaisesRegex(parallel.ParallelError, "SERVICE_TOKEN"):
            parallel.require_required_secrets(
                contract, ambient={"API_TOKEN": "rotated"})

    def test_start_rejects_missing_secret_before_workspace_or_repo_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            repo.mkdir()
            plan = root / "plan.json"
            plan.write_text(json.dumps([
                {"order": 1, "task": "one", "ref": None, "stack": 1},
            ]), encoding="utf-8")
            args = parallel.build_argument_parser().parse_args([
                "start", "--repo", str(repo), "--name", "base",
                "--agent-cmd", "agent", "--validate-cmd", "validate",
                "--import-plan", str(plan),
                "--required-secret-name", "API_TOKEN",
            ])
            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(parallel, "load_frozen_plan") as load_plan, \
                    mock.patch.object(parallel.loop_mod, "Workspace") as workspace, \
                    mock.patch.object(
                        parallel.repo_owner.RepoOwnerFence, "claim") as owner_claim:
                with self.assertRaisesRegex(parallel.ParallelError, "API_TOKEN"):
                    parallel.start_parallel(args, root / "workspaces")
        workspace.assert_not_called()
        load_plan.assert_not_called()
        owner_claim.assert_not_called()

    def test_direct_cli_reads_plan_once_only_after_base_run_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace_root = root / "workspaces"
            repo = root / "repo"
            repo.mkdir()
            plan = root / "plan.json"
            plan.write_text(json.dumps([
                {"order": 1, "task": "one", "ref": None, "stack": 1},
            ]), encoding="utf-8")
            args = parallel.build_argument_parser().parse_args([
                "start", "--repo", str(repo), "--name", "base",
                "--agent-cmd", "agent", "--validate-cmd", "validate",
                "--import-plan", str(plan),
            ])
            events = []

            class ObservedRunLock:
                def __init__(self, *_args, **_kwargs):
                    events.append("lock-init")

                def __enter__(self):
                    events.append("lock-enter")
                    return self

                def __exit__(self, *_args):
                    events.append("lock-exit")

            def observed_load(path, *, expected_raw_sha256=None):
                self.assertIn("lock-enter", events)
                self.assertNotIn("lock-exit", events)
                self.assertIsNone(expected_raw_sha256)
                events.append("plan-read")
                return [{
                    "order": 1, "task": "one", "ref": None, "stack": 1,
                }]

            with mock.patch.object(
                    parallel.loop_mod, "WORKSPACE_ROOT", workspace_root), \
                    mock.patch.object(
                        parallel, "SupervisorRunLock", ObservedRunLock), \
                    mock.patch.object(
                        parallel, "load_frozen_plan",
                        side_effect=observed_load) as load_plan, \
                    mock.patch.object(
                        parallel.repo_owner.RepoOwnerFence, "claim",
                        side_effect=parallel.repo_owner.RepoOwnerError("stop")):
                with self.assertRaisesRegex(
                        parallel.ParallelError, "owner audit blocked"):
                    parallel.start_parallel(args, workspace_root)

        load_plan.assert_called_once_with(
            plan, expected_raw_sha256=None)
        self.assertLess(events.index("lock-enter"), events.index("plan-read"))
        self.assertLess(events.index("plan-read"), events.index("lock-exit"))

    def test_expected_hash_mismatch_blocks_before_owner_or_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace_root = root / "workspaces"
            repo = root / "repo"
            repo.mkdir()
            plan = root / "plan.json"
            plan.write_text(json.dumps([
                {"order": 1, "task": "one", "ref": None, "stack": 1},
            ]), encoding="utf-8")
            args = parallel.build_argument_parser().parse_args([
                "start", "--repo", str(repo), "--name", "base",
                "--agent-cmd", "agent", "--validate-cmd", "validate",
                "--import-plan", str(plan),
                "--expected-plan-sha256", "0" * 64,
            ])
            with mock.patch.object(
                    parallel.loop_mod, "WORKSPACE_ROOT", workspace_root), \
                    mock.patch.object(
                        parallel, "load_frozen_plan",
                        wraps=parallel.load_frozen_plan) as load_plan, \
                    mock.patch.object(
                        parallel.repo_owner.RepoOwnerFence, "claim") as owner_claim, \
                    mock.patch.object(
                        parallel.parallel_state,
                        "materialize_run_artifacts") as materialize:
                with self.assertRaisesRegex(
                        parallel.ParallelError, "SHA-256 mismatch"):
                    parallel.start_parallel(args, workspace_root)

            load_plan.assert_called_once_with(
                plan, expected_raw_sha256="0" * 64)
            owner_claim.assert_not_called()
            materialize.assert_not_called()
            workspace = workspace_root / "base"
            self.assertTrue((workspace / ".run.lock").is_file())
            self.assertFalse((workspace / "state.json").exists())
            self.assertFalse((workspace / "parallel").exists())


class TestGateFailureProjection(unittest.TestCase):
    def test_repo_executor_failure_blocks_with_recovery_required_without_type_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_root = root / "workspaces"
            repo = root / "repo"
            workspace_root.mkdir()
            repo.mkdir()
            artifacts = parallel_state.materialize_run_artifacts(
                workspace_root,
                "base",
                RUN_ID,
                [{"order": 1, "task": "one", "stack": 1}],
                run_config(repo),
                "a" * 40,
                "refs/heads/main",
                "python -m engine.parallel_gate --run-dir fixed",
                dispatch_tokens={1: DISPATCH_TOKEN},
            )
            aggregate = parallel_state.build_initial_aggregate(
                RUN_ID, artifacts.plan)
            aggregate = parallel_state.transition_run_status(
                aggregate, "running")
            aggregate = parallel_state.transition_task(
                aggregate, 1, resource_state="provisioning")
            aggregate = parallel_state.transition_task(
                aggregate, 1, resource_state="running")

            executor = mock.Mock()
            executor.execute.side_effect = repo_executor.InvariantError(
                "injected RepoExecutor failure")
            supervisor = parallel.ParallelSupervisor(
                workspace_root=workspace_root,
                workspace=mock.Mock(),
                artifacts=artifacts,
                aggregate=aggregate,
                executor=executor,
                pending_launch_hash="4" * 64,
                session="5" * 32,
                generation=1,
            )
            request_id = "6" * 32
            request = parallel_gate.request_from_environment({
                "RUN_ID": RUN_ID,
                "TASK": "1",
                "REQUEST_ID": request_id,
                "VALIDATED_SHA": "b" * 40,
                "VALIDATED_ROUND": "2",
                "RUN_CONFIG_HASH": artifacts.run_config_hash,
                "LAUNCH_SPEC_HASH": artifacts.assignment_hashes[1],
                "MANIFEST_HASH": artifacts.manifest_hash,
            }, deadline_at="2026-01-01T00:00:00+00:00")
            supervisor.gate_spool.publish_request(request_id, request)

            with mock.patch.object(supervisor, "checkpoint") as checkpoint:
                # Regression: transition_task accepts only keyword state
                # changes.  Passing the failure as a positional outcome used
                # to mask the RepoExecutor error with TypeError here.
                self.assertTrue(supervisor.process_gate_requests())

            task = supervisor.aggregate["tasks"][0]
            self.assertEqual(task["outcome"], "pending")
            self.assertEqual(task["resource_state"], "recovery_required")
            self.assertIn("injected RepoExecutor failure", task["error"])
            self.assertEqual(supervisor.aggregate["status"], "blocked")
            self.assertEqual(
                supervisor.gate_spool.get_request(request_id).state, "claimed")
            self.assertIsNone(supervisor.gate_spool.get_response(request_id))
            self.assertGreaterEqual(checkpoint.call_count, 3)


class TestWorkerReapOwnership(unittest.TestCase):
    def test_failed_terminal_reap_proof_keeps_worker_handle_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_root = root / "workspaces"
            repo = root / "repo"
            workspace_root.mkdir()
            repo.mkdir()
            artifacts = parallel_state.materialize_run_artifacts(
                workspace_root,
                "base",
                RUN_ID,
                [{"order": 1, "task": "one", "stack": 1}],
                run_config(repo),
                "a" * 40,
                "refs/heads/main",
                "python -m engine.parallel_gate --run-dir fixed",
                dispatch_tokens={1: DISPATCH_TOKEN},
            )
            aggregate = parallel_state.build_initial_aggregate(
                RUN_ID, artifacts.plan)
            aggregate = parallel_state.transition_run_status(
                aggregate, "running")
            aggregate = parallel_state.transition_task(
                aggregate, 1, resource_state="provisioning")
            aggregate = parallel_state.transition_task(
                aggregate, 1, resource_state="running")
            supervisor = parallel.ParallelSupervisor(
                workspace_root=workspace_root,
                workspace=mock.Mock(),
                artifacts=artifacts,
                aggregate=aggregate,
                executor=mock.Mock(),
                pending_launch_hash="4" * 64,
                session="5" * 32,
                generation=1,
            )
            process = mock.Mock()
            process.poll.return_value = 9
            process.wait.return_value = 9
            handle = parallel.WorkerHandle(
                1,
                process,
                mock.Mock(),
                {"request_id": "6" * 32},
                False,
            )
            supervisor.handles[1] = handle

            with (
                mock.patch.object(
                    supervisor,
                    "_terminalize_child_from_wait",
                    side_effect=parallel.ParallelError(
                        "guardian exited after ACK without durable reap proof"),
                ),
                mock.patch.object(parallel.compat, "close_process_group"),
                mock.patch.object(supervisor, "checkpoint") as checkpoint,
            ):
                self.assertTrue(supervisor.reap_workers())

            self.assertIs(supervisor.handles[1], handle)
            self.assertEqual(supervisor.aggregate["status"], "blocked")
            self.assertEqual(
                supervisor.aggregate["tasks"][0]["resource_state"],
                "recovery_required",
            )
            checkpoint.assert_called_once_with()


class TestControlDeadline(unittest.TestCase):
    def test_pause_is_idempotent_at_blocked_completion_boundary(self):
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID,
            [{"order": 1, "task": "one", "stack": 1}],
        )
        aggregate = parallel_state.transition_run_status(aggregate, "running")
        aggregate = parallel_state.set_terminal_intent(aggregate, "completed")
        aggregate = parallel_state.transition_run_status(
            aggregate, "finalizing")
        aggregate = parallel_state.transition_run_status(aggregate, "blocked")

        parallel._validate_recovery_action_legality(aggregate, "pause")
        replayed = parallel._apply_claimed_control(
            aggregate, "pause", aggregate["control_generation"] + 1)

        self.assertEqual(replayed["status"], "blocked")
        self.assertEqual(replayed["terminal_intent"], "completed")
        self.assertEqual(
            replayed["control_generation"],
            aggregate["control_generation"] + 1,
        )

    def test_illegal_control_is_not_published_from_stale_caller_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            aggregate = parallel_state.build_initial_aggregate(
                RUN_ID,
                [{"order": 1, "task": "one", "stack": 1}],
            )
            aggregate = parallel_state.transition_run_status(
                aggregate, "running")
            aggregate = parallel_state.set_terminal_intent(
                aggregate, "completed")
            aggregate = parallel_state.transition_run_status(
                aggregate, "finalizing")
            existing = parallel.ExistingParallelRun(
                workspace=mock.Mock(name="base"),
                state={},
                artifacts=mock.Mock(
                    run_dir=run_dir,
                    manifest={"run_id": RUN_ID},
                ),
                aggregate=aggregate,
                generation=1,
                pending_launch_hash="7" * 64,
            )

            with self.assertRaisesRegex(
                    parallel.ParallelError, "completion finalization"):
                parallel._control_request(
                    existing,
                    {"session_id": "8" * 32, "generation": 1},
                    "abort",
                )

            self.assertFalse((run_dir / "controls").exists())

    def test_owner_rejects_control_that_became_illegal_before_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            spool = parallel_spool.DurableSpool(run_dir / "controls")
            aggregate = parallel_state.build_initial_aggregate(
                RUN_ID,
                [{"order": 1, "task": "one", "stack": 1}],
            )
            aggregate = parallel_state.transition_run_status(
                aggregate, "running")
            aggregate = parallel_state.set_terminal_intent(
                aggregate, "completed")
            aggregate = parallel_state.transition_run_status(
                aggregate, "finalizing")
            request_id = "9" * 32
            spool.publish_request(request_id, {
                "schema": 1,
                "request_id": request_id,
                "run_id": RUN_ID,
                "action": "abort",
                "supervisor_session": "8" * 32,
                "supervisor_generation": 1,
                "control_generation": aggregate["control_generation"] + 1,
                "aggregate_version": aggregate["version"],
            })
            supervisor = parallel.ParallelSupervisor.__new__(
                parallel.ParallelSupervisor)
            supervisor.control_spool = spool
            supervisor.artifacts = mock.Mock(manifest={"run_id": RUN_ID})
            supervisor.session = "8" * 32
            supervisor.generation = 1
            supervisor.aggregate = aggregate

            self.assertTrue(supervisor.process_controls())

            self.assertEqual(supervisor.aggregate, aggregate)
            durable = spool.get_request(request_id)
            self.assertIsNotNone(durable)
            self.assertEqual(durable.state, "cancelled")
            response = spool.get_response(request_id)
            self.assertIsNotNone(response)
            self.assertEqual(response.payload, {
                "schema": 1,
                "request_id": request_id,
                "status": "rejected",
                "action": "abort",
                "run_id": RUN_ID,
            })

    def test_unclaimed_control_timeout_cancels_request_and_cannot_ghost_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            artifacts = mock.Mock(
                run_dir=run_dir,
                manifest={"run_id": RUN_ID},
            )
            aggregate = parallel_state.build_initial_aggregate(
                RUN_ID,
                [{"order": 1, "task": "one", "stack": 1}],
            )
            existing = parallel.ExistingParallelRun(
                workspace=mock.Mock(name="base"),
                state={},
                artifacts=artifacts,
                aggregate=aggregate,
                generation=1,
                pending_launch_hash="7" * 64,
            )
            owner = {"session_id": "8" * 32, "generation": 1}
            with (mock.patch.object(
                    parallel.time, "monotonic", side_effect=[0.0, 11.0]),
                  self.assertRaisesRegex(
                      parallel.ParallelError, "原子取消.*ghost")):
                parallel._control_request(existing, owner, "pause")

            spool = parallel_spool.DurableSpool(run_dir / "controls")
            cancelled = spool.list_requests("cancelled")
            self.assertEqual(len(cancelled), 1)
            self.assertIsNone(spool.get_response(cancelled[0].request_id))


class TestFinalizationInterrupt(unittest.TestCase):
    def test_keyboard_interrupt_preserves_completed_intent_for_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_root = root / "workspaces"
            repo = root / "repo"
            workspace_root.mkdir()
            repo.mkdir()
            artifacts = parallel_state.materialize_run_artifacts(
                workspace_root, "base", RUN_ID,
                [{"order": 1, "task": "one", "stack": 1}],
                run_config(repo), "a" * 40, "refs/heads/main",
                "python -m engine.parallel_gate --run-dir fixed",
                dispatch_tokens={1: DISPATCH_TOKEN},
            )
            aggregate = parallel_state.build_initial_aggregate(
                RUN_ID, artifacts.plan)
            aggregate = parallel_state.transition_run_status(
                aggregate, "running")
            aggregate = parallel_state.transition_task(
                aggregate, 1, resource_state="provisioning")
            aggregate = parallel_state.transition_task(
                aggregate, 1, resource_state="running")
            aggregate = parallel_state.transition_task(
                aggregate, 1, outcome="integrated", resource_state="exited")
            aggregate = parallel_state.transition_task(
                aggregate, 1, resource_state="cleaning")
            aggregate = parallel_state.transition_task(
                aggregate, 1, resource_state="cleaned")
            supervisor = parallel.ParallelSupervisor(
                workspace_root=workspace_root,
                workspace=mock.Mock(),
                artifacts=artifacts,
                aggregate=aggregate,
                executor=mock.Mock(),
                pending_launch_hash="9" * 64,
                session="a" * 32,
                generation=1,
                bootstrap_required=False,
            )
            with (mock.patch.object(supervisor, "checkpoint"),
                  mock.patch.object(
                      supervisor, "_audit_terminal_receipt_projection"),
                  mock.patch.object(
                      supervisor, "_archive_terminal_worker_workspaces"),
                  mock.patch.object(
                      supervisor, "_audit_terminal_worker_archives"),
                  mock.patch.object(
                      supervisor, "_durable_finalize",
                      side_effect=KeyboardInterrupt),
                  mock.patch.object(supervisor, "quiesce_blocked") as quiesce,
                  mock.patch.object(supervisor, "pause") as pause):
                self.assertEqual(supervisor.run(), 2)
            self.assertEqual(supervisor.aggregate["terminal_intent"], "completed")
            self.assertEqual(supervisor.aggregate["status"], "blocked")
            quiesce.assert_called_once_with()
            pause.assert_not_called()


class TestPrePayloadWorkspaceArchive(unittest.TestCase):
    def _supervisor(self, root: Path) -> parallel.ParallelSupervisor:
        workspace_root = root / "workspaces"
        repo = root / "repo"
        workspace_root.mkdir()
        repo.mkdir()
        artifacts = parallel_state.materialize_run_artifacts(
            workspace_root, "base", RUN_ID,
            [{"order": 1, "task": "one", "stack": 1}],
            run_config(repo), "a" * 40, "refs/heads/main",
            "python -m engine.parallel_gate --run-dir fixed",
            dispatch_tokens={1: DISPATCH_TOKEN},
        )
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, artifacts.plan)
        aggregate = parallel_state.set_terminal_intent(
            aggregate, "cancelled")
        aggregate = parallel_state.transition_task(
            aggregate, 1, outcome="cancelled", resource_state="cleaned",
            explicit_abort=True,
        )
        return parallel.ParallelSupervisor(
            workspace_root=workspace_root,
            workspace=mock.Mock(),
            artifacts=artifacts,
            aggregate=aggregate,
            executor=mock.Mock(),
            pending_launch_hash="9" * 64,
            session="a" * 32,
            generation=1,
            bootstrap_required=False,
        )

    def test_abort_archives_container_created_before_any_child_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = self._supervisor(root)
            with mock.patch.object(
                    loop_mod, "WORKSPACE_ROOT", supervisor.workspace_root):
                supervisor._ensure_worker_workspace_container(1)
                source = Path(supervisor.artifacts.assignments[1][
                    "worker_workspace_path"])
                self.assertTrue(source.is_dir())
                self.assertEqual(supervisor._task_child_records(1), ())

                supervisor._archive_terminal_worker_workspaces()
                supervisor._audit_terminal_worker_archives()

            archive = supervisor.artifacts.run_dir / "worker-archives" / "task-1"
            self.assertFalse(source.exists())
            self.assertTrue(archive.is_dir())

    def test_unexplained_pre_payload_workspace_content_blocks_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = self._supervisor(root)
            with mock.patch.object(
                    loop_mod, "WORKSPACE_ROOT", supervisor.workspace_root):
                supervisor._ensure_worker_workspace_container(1)
            source = Path(supervisor.artifacts.assignments[1][
                "worker_workspace_path"])
            (source / "unexpected.txt").write_text("unknown\n", encoding="utf-8")

            with self.assertRaisesRegex(parallel.ParallelError, "not pristine"):
                supervisor._archive_terminal_worker_workspaces()

    def test_reaped_pre_ack_record_cannot_outlive_missing_container(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = self._supervisor(root)
            with mock.patch.object(
                    loop_mod, "WORKSPACE_ROOT", supervisor.workspace_root):
                supervisor._ensure_worker_workspace_container(1)
            source = Path(supervisor.artifacts.assignments[1][
                "worker_workspace_path"])
            shutil.rmtree(source)

            with (mock.patch.object(
                    supervisor, "_task_child_records", return_value=({
                        "child_id": "1" * 32,
                        "state": "reaped",
                        "payload_pid": None,
                    },)),
                  self.assertRaisesRegex(
                      parallel.ParallelError, "workspace evidence")):
                supervisor._require_reaped_child_evidence(1)

    def test_cancelled_pre_spawn_reservation_still_requires_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = self._supervisor(root)
            with mock.patch.object(
                    loop_mod, "WORKSPACE_ROOT", supervisor.workspace_root):
                supervisor._ensure_worker_workspace_container(1)
            reservation = parallel.publish_launch_reservation(
                supervisor.artifacts, 1,
                supervisor_session=supervisor.session,
                supervisor_generation=supervisor.generation,
                attempt=0,
                resume=False,
            )
            parallel.cancel_launch_reservation(
                supervisor.artifacts.run_dir, reservation["request_id"])
            source = Path(supervisor.artifacts.assignments[1][
                "worker_workspace_path"])
            shutil.rmtree(source)

            with self.assertRaisesRegex(
                    parallel.ParallelError, "workspace audit evidence"):
                supervisor._archive_terminal_worker_workspaces()

    def test_cancel_winner_inert_pid_still_requires_pristine_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervisor = self._supervisor(root)
            with mock.patch.object(
                    loop_mod, "WORKSPACE_ROOT", supervisor.workspace_root):
                supervisor._ensure_worker_workspace_container(1)
            reservation = parallel.publish_launch_reservation(
                supervisor.artifacts, 1,
                supervisor_session=supervisor.session,
                supervisor_generation=supervisor.generation,
                attempt=0,
                resume=False,
            )
            parallel.cancel_launch_reservation(
                supervisor.artifacts.run_dir, reservation["request_id"])
            source = Path(supervisor.artifacts.assignments[1][
                "worker_workspace_path"])
            (source / "unexpected.txt").write_text(
                "not payload-authorized\n", encoding="utf-8")
            inert_child = {
                "child_id": reservation["request_id"],
                "state": "reaped",
                "payload_pid": 12345,
            }

            with (
                mock.patch.object(
                    supervisor, "_task_child_records",
                    return_value=(inert_child,)),
                self.assertRaisesRegex(parallel.ParallelError, "not pristine"),
            ):
                supervisor._archive_terminal_worker_workspaces()


if __name__ == "__main__":
    unittest.main()
