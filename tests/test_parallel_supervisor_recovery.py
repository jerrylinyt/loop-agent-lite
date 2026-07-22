"""Supervisor recovery regressions for retained gates and receipt authority."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from engine import cli
from engine import loop as loop_mod
from engine import parallel
from engine import parallel_contract
from engine import parallel_gate
from engine import parallel_spool
from engine import parallel_state
from engine import parallel_worker
from engine import platform_compat as compat
from engine import repo_executor
from engine import repo_owner


RUN_ID = "a1b2c3d4"
SESSION = "5" * 32
PENDING_LAUNCH_HASH = "4" * 64
DISPATCH_TOKEN = "supervisor-recovery-test-token"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True,
    )


def run_config(repo: Path) -> dict:
    return {
        "repo": str(repo.resolve()),
        "primary_repo": str(repo.resolve()),
        "goal": "goal.md",
        "plan_doc": "",
        "agent_cmd": "agent --test",
        "validate_cmd": compat.join_command([
            sys.executable, "-c", "raise SystemExit(0)",
        ]),
        "notify_cmd": "",
        "flag_threshold": 2,
        "done_threshold": 1,
        "red_limit": 3,
        "stall_limit": 4,
        "stuck_stop": False,
        "stuck_stop_count": 5,
        "round_timeout": 1,
        "agent_backoff_max": 2,
        "validate_timeout": 10,
        "max_parallel": 1,
        "worker_restart_limit": 3,
        "environment": {
            "path_additions": [],
            "non_secret": {},
            "required_secret_names": [],
        },
        "max_rounds": 0,
        "pause_after_plan": False,
        "allow_serial_stack": False,
    }


@unittest.skipUnless(shutil.which("git"), "requires git")
class TestParallelSupervisorRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "primary"
        self.workspace_root = self.root / "workspaces"
        self.repo.mkdir()
        self.workspace_root.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Supervisor Recovery Test")
        git(self.repo, "config", "user.email", "recovery@example.invalid")
        (self.repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
        git(self.repo, "add", "goal.md")
        git(self.repo, "commit", "-qm", "initial")
        self.start = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.primary_ref = git(
            self.repo, "symbolic-ref", "HEAD").stdout.strip()
        self.plan = [
            {"order": 1, "task": "recover one worker", "stack": 1},
        ]
        self.artifacts = parallel_state.materialize_run_artifacts(
            self.workspace_root,
            "base",
            RUN_ID,
            self.plan,
            run_config(self.repo),
            self.start,
            self.primary_ref,
            "python -m engine.parallel_gate --run-dir fixed",
            dispatch_tokens={1: DISPATCH_TOKEN},
        )
        self.workspace_root_patch = mock.patch.object(
            loop_mod, "WORKSPACE_ROOT", self.workspace_root)
        self.workspace_root_patch.start()
        self.addCleanup(self.workspace_root_patch.stop)
        # A provisioned worker always has its workspace directory before its
        # first checkpoint.  Keep that topology even in corruption fixtures so
        # a genuinely absent state.json is reported as absent, not malformed.
        loop_mod.Workspace(
            self.artifacts.assignments[1]["worker_workspace"])

    def aggregate_running(self) -> dict:
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        aggregate = parallel_state.transition_run_status(aggregate, "running")
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="provisioning")
        return parallel_state.transition_task(
            aggregate, 1, resource_state="running")

    def aggregate_exited(self, *, integrated: bool = False) -> dict:
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="provisioning")
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="running")
        if integrated:
            return parallel_state.transition_task(
                aggregate, 1, outcome="integrated", resource_state="exited")
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="gate_pending")
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="gate_claimed")
        return parallel_state.transition_task(
            aggregate, 1, resource_state="exited")

    def supervisor(self, aggregate: dict, executor) -> parallel.ParallelSupervisor:
        return parallel.ParallelSupervisor(
            workspace_root=self.workspace_root,
            workspace=mock.Mock(),
            artifacts=self.artifacts,
            aggregate=aggregate,
            executor=executor,
            pending_launch_hash=PENDING_LAUNCH_HASH,
            session=SESSION,
            generation=1,
            bootstrap_required=False,
        )

    def gate_request(self, request_id: str, validated_sha: str, *, round_: int = 2) -> dict:
        return parallel_gate.request_from_environment({
            "RUN_ID": RUN_ID,
            "TASK": "1",
            "REQUEST_ID": request_id,
            "VALIDATED_SHA": validated_sha,
            "VALIDATED_ROUND": str(round_),
            "RUN_CONFIG_HASH": self.artifacts.run_config_hash,
            "LAUNCH_SPEC_HASH": self.artifacts.assignment_hashes[1],
            "MANIFEST_HASH": self.artifacts.manifest_hash,
        }, deadline_at="2030-01-01T00:00:00+00:00")

    def publish_gate(
        self, request_id: str, validated_sha: str, *, state: str,
        success_status: str | None = None,
    ) -> tuple[parallel_spool.DurableSpool, dict]:
        spool = parallel_spool.DurableSpool(
            self.artifacts.run_dir / "requests",
            responses_root=self.artifacts.run_dir / "responses",
        )
        request = self.gate_request(request_id, validated_sha)
        spool.publish_request(request_id, request)
        if state == "claimed":
            self.assertTrue(spool.claim_request(request_id).transitioned)
        elif state == "cancelled":
            self.assertTrue(spool.cancel_request(request_id).transitioned)
        else:
            self.fail(f"unsupported gate state: {state}")
        if success_status is not None:
            returncode = 0 if success_status in {"merged", "already-merged"} else 10
            spool.publish_response(
                request_id,
                parallel_gate.durable_response_envelope(
                    request, returncode=returncode, status=success_status),
            )
        return spool, request

    def retained_worker_state(self, request: dict, *, status: str = "running") -> Path:
        assignment = self.artifacts.assignments[1]
        launch = parallel_worker.ManagedWorkerLaunch(
            resume=False,
            run_id=RUN_ID,
            assigned_order=1,
            stop_after_task=True,
            complete_gate_cmd=assignment["gate_command"],
            integration_ref=assignment["integration_ref"],
            parent_workspace="base",
            task_ref=assignment["task_ref"],
            run_config_hash=self.artifacts.run_config_hash,
            launch_spec_hash=self.artifacts.assignment_hashes[1],
            manifest_hash=self.artifacts.manifest_hash,
            dispatch_token=DISPATCH_TOKEN,
            dispatch_request_id="d" * 32,
            supervisor_session=SESSION,
            supervisor_generation=1,
            dispatch_attempt=0,
        )
        state = parallel_worker.initialize_state({
            "phase": "exec",
            "plan": [dict(item) for item in self.artifacts.plan],
            "completed": [],
            "done_count": 1,
            "notes": [],
        }, launch)
        state["assignment"].update({
            "status": status,
            "validated_sha": request["validated_sha"],
            "validated_round": request["validated_round"],
            "exit_reason": (
                "claimed response interrupted"
                if status == "recovery-required" else None),
            "gate_request": {
                "request_id": request["request_id"],
                "validated_sha": request["validated_sha"],
                "validated_round": request["validated_round"],
            },
        })
        worker = loop_mod.Workspace(assignment["worker_workspace"])
        worker.save_state(state)
        return worker.state_path

    def worker_state_without_gate(self, *, status: str) -> Path:
        request = self.gate_request("e" * 32, self.start)
        state_path = self.retained_worker_state(request, status="running")
        worker = loop_mod.Workspace(
            self.artifacts.assignments[1]["worker_workspace"])
        state = worker.load_state()
        state["assignment"].update({
            "status": status,
            "validated_sha": None,
            "validated_round": None,
            "gate_request": None,
            "exit_reason": (
                "worker deliberately blocked"
                if status == "blocked"
                else "parent supervisor requested Pause generation 1"
                if status == "paused" else None),
            "pause_generation": 1 if status == "paused" else 0,
        })
        worker.save_state(state)
        return state_path

    def audit_at_start(self) -> dict:
        return {
            "receipt_tip": self.start,
            "primary_sha": self.start,
            "sync_sha": self.start,
        }

    def true_executor(self) -> repo_executor.RepoExecutor:
        spec = parallel.build_repo_spec(
            self.artifacts,
            pending_launch_hash=PENDING_LAUNCH_HASH,
            supervisor_session=SESSION,
            generation=1,
        )
        executor = repo_executor.RepoExecutor(spec)
        self.addCleanup(executor.close)
        executor.execute({
            "operation": repo_executor.Operation.INITIALIZE_RUN_REFS.value,
            "operation_id": "1" * 32,
            "authority": {"manifest_hash": self.artifacts.manifest_hash},
            "expected": {
                "integration_start_sha": self.start,
                "sync_ref_absent": True,
            },
        })
        executor.execute({
            "operation": repo_executor.Operation.CREATE_WORKTREE.value,
            "operation_id": "2" * 32,
            "task": 1,
            "authority": {
                "manifest_hash": self.artifacts.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[1],
            },
            "expected": {
                "base_sha": self.start,
                "task_ref_absent": True,
                "worktree_absent": True,
            },
        })
        return executor

    def pristine_executor(self) -> repo_executor.RepoExecutor:
        spec = parallel.build_repo_spec(
            self.artifacts,
            pending_launch_hash=PENDING_LAUNCH_HASH,
            supervisor_session=SESSION,
            generation=1,
        )
        executor = repo_executor.RepoExecutor(spec)
        self.addCleanup(executor.close)
        return executor

    def reserve_operation(self, executor, request: dict) -> dict:
        executor._start()
        operation, operation_id, _task, _authority, expected = (
            executor._validate_request(request))
        request_hash = repo_executor.canonical_hash(request)
        lease = executor._new_lease(
            operation, operation_id, request_hash, expected, request)
        executor._atomic_json(executor.lease_path, lease)
        return lease

    def publish_claimed_abort_control(self, aggregate: dict) -> dict:
        record = {
            "schema": 1, "run_id": RUN_ID, "request_id": "f" * 32,
            "action": "abort", "state": "claimed",
            "expected_supervisor_generation": 1,
            "expected_aggregate_version": aggregate["version"],
            "expected_control_generation": aggregate["control_generation"],
            "created_at": "2026-07-22T00:00:00+00:00",
            "claimed_by": {
                "session": "6" * 32, "generation": 2,
                "claimed_at": "2026-07-22T00:00:01+00:00",
            },
            "assigned_control_generation": aggregate["control_generation"] + 1,
            "applied": None,
        }
        parallel._write_bootstrap_control(self.artifacts, record)
        return record

    def publish_initial_base_projection(self):
        workspace = loop_mod.Workspace("base")
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        parallel_state.atomic_write_json(
            self.artifacts.run_dir, "aggregate.json", aggregate)
        parallel._initialize_supervisor_generation(
            self.artifacts, session=SESSION)
        workspace.save_state(parallel.project_base_state(
            workspace,
            self.artifacts,
            aggregate,
            (),
            supervisor_pid=None,
            supervisor_session=None,
            supervisor_generation=1,
        ))
        return workspace, aggregate

    def commit_worker_change(self, executor: repo_executor.RepoExecutor) -> str:
        worktree = executor.worktree_path(1)
        (worktree / "recovered.txt").write_text(
            "recovered\n", encoding="utf-8")
        git(worktree, "add", "recovered.txt")
        git(worktree, "commit", "-qm", "worker result")
        return git(worktree, "rev-parse", "HEAD").stdout.strip()

    def execute_gate(
        self, executor: repo_executor.RepoExecutor,
        request: dict,
    ) -> tuple[dict, dict]:
        operation = {
            "operation": repo_executor.Operation.GATE_MERGE.value,
            "operation_id": repo_executor.gate_operation_id(
                RUN_ID, request["request_id"]),
            "task": 1,
            "authority": {
                "manifest_hash": self.artifacts.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[1],
                "request_hash": repo_executor.canonical_hash(request),
            },
            "expected": {
                "request_id": request["request_id"],
                "validated_sha": request["validated_sha"],
                "validated_round": request["validated_round"],
                "integration_before": self.start,
                "sync_before": self.start,
            },
        }
        return operation, executor.execute(operation)

    def run_blocks_without_cleanup(
        self,
        supervisor: parallel.ParallelSupervisor,
        worktree: Path,
        *, error_pattern: str,
        guard_dispatch: bool = False,
    ) -> None:
        dispatch = (mock.patch.object(
            supervisor, "_dispatch_available",
            side_effect=AssertionError(
                "corrupt gate evidence reached worker dispatch"))
                    if guard_dispatch else mock.patch.object(
                        supervisor, "_dispatch_available",
                        wraps=supervisor._dispatch_available))
        with (mock.patch.object(supervisor, "checkpoint"),
              mock.patch.object(supervisor, "quiesce_blocked") as quiesce,
              mock.patch.object(supervisor, "_cleanup_integrated") as cleanup,
              dispatch as dispatch_mock):
            result = supervisor.run()
        self.assertEqual(result, 2)
        self.assertEqual(supervisor.aggregate["status"], "blocked")
        self.assertRegex(supervisor.aggregate["error"], error_pattern)
        cleanup.assert_not_called()
        quiesce.assert_called_once()
        if guard_dispatch:
            dispatch_mock.assert_not_called()
        self.assertTrue(worktree.is_dir())

    def test_cancelled_retained_gate_publishes_safe_retry_and_pauses_resource(self):
        request_id = "6" * 32
        spool, request = self.publish_gate(
            request_id, self.start, state="cancelled")
        state_path = self.retained_worker_state(
            request, status="recovery-required")
        executor = mock.Mock()
        executor.audit_recovery_state.return_value = self.audit_at_start()
        supervisor = self.supervisor(self.aggregate_running(), executor)

        with (mock.patch.object(supervisor, "checkpoint"),
              mock.patch.object(
                  supervisor, "_task_child_records",
                  return_value=({"state": "reaped"},)),
              mock.patch.object(
                  supervisor, "_require_reaped_child_evidence")):
            supervisor.reconcile_existing()

        response = spool.get_response(request_id)
        self.assertIsNotNone(response)
        returncode, payload = parallel_gate._validate_durable_response(
            response, request)
        self.assertEqual(returncode, 11)
        self.assertEqual(payload["status"], "supervisor-lost-before-claim")
        self.assertEqual(spool.get_request(request_id).state, "cancelled")
        worker_state, _raw, _recovered = loop_mod.load_checkpointed_state(
            state_path, repair=False)
        self.assertEqual(worker_state["assignment"]["status"], "running")
        self.assertIsNone(worker_state["assignment"]["gate_request"])
        self.assertIsNone(worker_state["assignment"]["validated_sha"])
        task = supervisor.aggregate["tasks"][0]
        self.assertEqual(task["outcome"], "pending")
        self.assertEqual(task["resource_state"], "paused")
        executor.reconcile_claimed_gate.assert_not_called()

    def test_running_aggregate_with_retained_claim_uses_claimed_stale_recovery(self):
        request_id = "7" * 32
        spool, request = self.publish_gate(
            request_id, self.start, state="claimed",
            success_status="stale-integration")
        state_path = self.retained_worker_state(request, status="running")
        executor = mock.Mock()
        executor.reconcile_claimed_gate.return_value = {
            "status": "stale-integration",
        }
        executor.audit_recovery_state.return_value = self.audit_at_start()
        supervisor = self.supervisor(self.aggregate_running(), executor)

        with (mock.patch.object(supervisor, "checkpoint"),
              mock.patch.object(
                  supervisor, "_task_child_records",
                  return_value=({"state": "reaped"},)),
              mock.patch.object(
                  supervisor, "_require_reaped_child_evidence")):
            supervisor.reconcile_existing()

        executor.reconcile_claimed_gate.assert_called_once()
        self.assertEqual(spool.get_request(request_id).state, "claimed")
        self.assertIsNotNone(spool.get_response(request_id))
        worker_state, _raw, _recovered = loop_mod.load_checkpointed_state(
            state_path, repair=False)
        self.assertEqual(worker_state["assignment"]["status"], "running")
        self.assertIsNone(worker_state["assignment"]["gate_request"])
        task = supervisor.aggregate["tasks"][0]
        self.assertEqual(task["outcome"], "pending")
        self.assertEqual(task["resource_state"], "crashed")
        self.assertEqual(task["restart_count"], 1)
        self.assertIn("recovered stale gate", task["error"])
        self.assertNotIn("supervisor resume observed reaped worker", task["error"])

    def test_canonical_receipt_missing_common_evidence_blocks_before_cleanup(self):
        executor = self.true_executor()
        validated = self.commit_worker_change(executor)
        request_id = "8" * 32
        _spool, request = self.publish_gate(
            request_id, validated, state="claimed")
        operation, result = self.execute_gate(executor, request)
        self.assertEqual(result["status"], "merged")
        worktree = executor.worktree_path(1)
        evidence = {
            "journal": executor._intent_path("gate", request_id),
            "result": executor._operation_result_path(operation["operation_id"]),
        }

        for label, path in evidence.items():
            with self.subTest(missing=label):
                raw = path.read_bytes()
                path.unlink()
                try:
                    supervisor = self.supervisor(
                        self.aggregate_exited(), executor)
                    self.run_blocks_without_cleanup(
                        supervisor, worktree,
                        error_pattern="recovery audit blocked",
                    )
                finally:
                    path.write_bytes(raw)

    def test_success_response_without_receipt_blocks_before_dispatch_or_cleanup(self):
        executor = self.true_executor()
        validated = self.commit_worker_change(executor)
        request_id = "9" * 32
        self.publish_gate(
            request_id, validated, state="claimed", success_status="merged")
        self.assertFalse(
            (self.artifacts.run_dir / "receipts" / "task-1.json").exists())
        supervisor = self.supervisor(self.aggregate_exited(), executor)

        self.run_blocks_without_cleanup(
            supervisor,
            executor.worktree_path(1),
            error_pattern="success.*receipt|receipt.*success",
            guard_dispatch=True,
        )

    def test_integrated_aggregate_without_receipt_blocks_before_cleanup(self):
        executor = self.true_executor()
        worktree = executor.worktree_path(1)
        supervisor = self.supervisor(
            self.aggregate_exited(integrated=True), executor)

        self.run_blocks_without_cleanup(
            supervisor,
            worktree,
            error_pattern="integrated.*canonical receipt",
        )

    def test_historical_success_remains_auditable_after_resource_cleanup(self):
        aggregate = self.aggregate_exited(integrated=True)
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="cleaning")
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="cleaned")
        request_id = "a" * 32
        _spool, request = self.publish_gate(
            request_id, self.start, state="claimed", success_status="merged")
        supervisor = self.supervisor(aggregate, mock.Mock())

        supervisor._audit_success_responses({
            1: {
                "request_id": request_id,
                "validated_sha": request["validated_sha"],
                "validated_round": request["validated_round"],
            },
        })

    def test_cleaned_task_rejects_unbound_pending_gate_history(self):
        aggregate = self.aggregate_exited(integrated=True)
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="cleaning")
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="cleaned")
        request_id = "b" * 32
        request = self.gate_request(request_id, self.start)
        spool = parallel_spool.DurableSpool(
            self.artifacts.run_dir / "requests",
            responses_root=self.artifacts.run_dir / "responses",
        )
        spool.publish_request(request_id, request)
        supervisor = self.supervisor(aggregate, mock.Mock())

        with self.assertRaisesRegex(parallel.ParallelError, "recovery gate request"):
            supervisor._audit_success_responses({})

    def test_client_cancelled_without_response_is_valid_history(self):
        request_id = "3" * 32
        request = self.gate_request(request_id, self.start)
        spool = parallel_spool.DurableSpool(
            self.artifacts.run_dir / "requests",
            responses_root=self.artifacts.run_dir / "responses")
        spool.publish_request(request_id, request)
        self.assertTrue(spool.cancel_request(request_id).transitioned)
        self.assertIsNone(spool.get_response(request_id))
        supervisor = self.supervisor(self.aggregate_running(), mock.Mock())

        supervisor._audit_success_responses({})

    def test_reaped_blocked_worker_is_not_redispatched_or_charged_restart(self):
        self.worker_state_without_gate(status="blocked")
        executor = mock.Mock()
        executor.audit_recovery_state.return_value = self.audit_at_start()
        supervisor = self.supervisor(self.aggregate_running(), executor)

        with (mock.patch.object(supervisor, "checkpoint"),
              mock.patch.object(
                  supervisor, "_task_child_records",
                  return_value=({"state": "reaped", "payload_pid": 123},)),
              mock.patch.object(
                  supervisor, "_require_reaped_child_evidence")):
            supervisor.reconcile_existing()

        task = supervisor.aggregate["tasks"][0]
        self.assertEqual(task["outcome"], "blocked")
        self.assertEqual(task["resource_state"], "exited")
        self.assertEqual(task["restart_count"], 0)
        self.assertEqual(supervisor.aggregate["status"], "blocked")

    def test_explicit_abort_does_not_consume_exhausted_restart_budget(self):
        self.worker_state_without_gate(status="running")
        aggregate = self.aggregate_running()
        for _ in range(3):
            aggregate = parallel_state.increment_restart_count(
                aggregate, 1, limit=3)
        aggregate = parallel_state.set_terminal_intent(
            aggregate, "cancelled")
        aggregate = parallel_state.transition_run_status(
            aggregate, "cancel_requested")
        executor = mock.Mock()
        executor.audit_recovery_state.return_value = self.audit_at_start()
        supervisor = self.supervisor(aggregate, executor)

        with (mock.patch.object(supervisor, "checkpoint"),
              mock.patch.object(
                  supervisor, "_task_child_records",
                  return_value=({"state": "reaped", "payload_pid": 123},)),
              mock.patch.object(
                  supervisor, "_require_reaped_child_evidence")):
            supervisor.reconcile_existing(explicit_abort=True)

        task = supervisor.aggregate["tasks"][0]
        self.assertEqual(task["outcome"], "cancelled")
        self.assertEqual(task["resource_state"], "exited")
        self.assertEqual(task["restart_count"], 3)

    def test_reaped_gracefully_paused_worker_does_not_consume_restart(self):
        self.worker_state_without_gate(status="paused")
        executor = mock.Mock()
        executor.audit_recovery_state.return_value = self.audit_at_start()
        aggregate = parallel_state.advance_pause_generation(
            self.aggregate_running())
        supervisor = self.supervisor(aggregate, executor)

        with (mock.patch.object(supervisor, "checkpoint"),
              mock.patch.object(
                  supervisor, "_task_child_records",
                  return_value=({"state": "reaped", "payload_pid": 123},)),
              mock.patch.object(
                  supervisor, "_require_reaped_child_evidence")):
            supervisor.reconcile_existing()

        task = supervisor.aggregate["tasks"][0]
        self.assertEqual(task["outcome"], "pending")
        self.assertEqual(task["resource_state"], "paused")
        self.assertEqual(task["restart_count"], 0)

    def test_current_pause_repairs_stale_worker_pause_generation(self):
        self.worker_state_without_gate(status="paused")
        aggregate = self.aggregate_running()
        aggregate = parallel_state.advance_pause_generation(aggregate)
        aggregate = parallel_state.transition_run_status(
            aggregate, "pause_requested")
        aggregate = parallel_state.advance_pause_generation(aggregate)
        executor = mock.Mock()
        executor.audit_recovery_state.return_value = self.audit_at_start()
        supervisor = self.supervisor(aggregate, executor)

        with (mock.patch.object(supervisor, "checkpoint"),
              mock.patch.object(
                  supervisor, "_task_child_records",
                  return_value=({"state": "reaped", "payload_pid": 123},)),
              mock.patch.object(
                  supervisor, "_require_reaped_child_evidence")):
            supervisor.reconcile_existing()

        task = supervisor.aggregate["tasks"][0]
        self.assertEqual(task["resource_state"], "paused")
        self.assertEqual(task["restart_count"], 0)
        worker = loop_mod.Workspace(
            self.artifacts.assignments[1]["worker_workspace"])
        self.assertEqual(
            worker.load_state()["assignment"]["pause_generation"], 2)

    def test_resume_repairs_lagging_paused_generation_after_owner_loss(self):
        self.worker_state_without_gate(status="paused")
        aggregate = self.aggregate_running()
        aggregate = parallel_state.advance_pause_generation(aggregate)
        aggregate = parallel_state.advance_pause_generation(aggregate)
        aggregate = parallel_state.transition_run_status(
            aggregate, "blocked")
        aggregate = parallel_state.transition_run_status(
            aggregate, "initializing")
        executor = mock.Mock()
        executor.audit_recovery_state.return_value = self.audit_at_start()
        supervisor = self.supervisor(aggregate, executor)

        with (mock.patch.object(supervisor, "checkpoint"),
              mock.patch.object(
                  supervisor, "_task_child_records",
                  return_value=({"state": "reaped", "payload_pid": 123},)),
              mock.patch.object(
                  supervisor, "_require_reaped_child_evidence")):
            supervisor.reconcile_existing()

        self.assertEqual(
            supervisor.aggregate["tasks"][0]["resource_state"], "paused")
        worker = loop_mod.Workspace(
            self.artifacts.assignments[1]["worker_workspace"])
        self.assertEqual(
            worker.load_state()["assignment"]["pause_generation"], 2)

    def test_claimed_gate_without_response_or_current_binding_blocks(self):
        request_id = "f" * 32
        self.publish_gate(request_id, self.start, state="claimed")
        executor = mock.Mock()
        supervisor = self.supervisor(self.aggregate_running(), executor)

        with self.assertRaisesRegex(
                parallel.ParallelError,
                "claimed gate request has no response.*authority"):
            supervisor.reconcile_existing()
        executor.audit_recovery_state.assert_not_called()

    def test_claimed_stale_during_abort_never_reactivates_or_charges_restart(self):
        request_id = "0" * 32
        _spool, request = self.publish_gate(
            request_id, self.start, state="claimed",
            success_status="stale-integration")
        self.retained_worker_state(request, status="running")
        aggregate = self.aggregate_running()
        aggregate = parallel_state.set_terminal_intent(
            aggregate, "cancelled")
        aggregate = parallel_state.transition_run_status(
            aggregate, "cancel_requested")
        executor = mock.Mock()
        executor.reconcile_claimed_gate.return_value = {
            "status": "stale-integration",
        }
        executor.audit_recovery_state.return_value = self.audit_at_start()
        supervisor = self.supervisor(aggregate, executor)

        with (mock.patch.object(supervisor, "checkpoint"),
              mock.patch.object(
                  supervisor, "_task_child_records",
                  return_value=({"state": "reaped", "payload_pid": 123},)),
              mock.patch.object(
                  supervisor, "_require_reaped_child_evidence")):
            supervisor.reconcile_existing(explicit_abort=True)

        task = supervisor.aggregate["tasks"][0]
        self.assertEqual(task["outcome"], "cancelled")
        self.assertEqual(task["resource_state"], "exited")
        self.assertEqual(task["restart_count"], 0)
        worker = loop_mod.Workspace(
            self.artifacts.assignments[1]["worker_workspace"])
        state = worker.load_state()
        self.assertEqual(state["assignment"]["status"], "cancelled")
        self.assertIsNone(state["assignment"]["gate_request"])

    def test_dirty_cleanup_can_retry_in_new_generation_before_remove_intent(self):
        executor = self.true_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="provisioning")
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="running")
        aggregate = parallel_state.set_terminal_intent(
            aggregate, "cancelled")
        aggregate = parallel_state.transition_run_status(
            aggregate, "cancel_requested")
        aggregate = parallel_state.transition_task(
            aggregate, 1, outcome="cancelled", resource_state="exited",
            explicit_abort=True)
        supervisor = self.supervisor(aggregate, executor)
        dirty = executor.worktree_path(1) / "dirty.txt"
        dirty.write_text("dirty\n", encoding="utf-8")

        with (mock.patch.object(supervisor, "checkpoint"),
              self.assertRaisesRegex(parallel.ParallelError, "cleanup failed")):
            supervisor._cleanup_terminal_task(1)
        self.assertEqual(
            supervisor.aggregate["tasks"][0]["resource_state"],
            "cleanup_failed")
        dirty.unlink()
        executor.audit_recovery_state(
            allowed_blocked_removes={1: "cancelled"})

        recovered = parallel.ParallelSupervisor(
            workspace_root=self.workspace_root,
            workspace=mock.Mock(),
            artifacts=self.artifacts,
            aggregate=supervisor.aggregate,
            executor=executor,
            pending_launch_hash=PENDING_LAUNCH_HASH,
            session="6" * 32,
            generation=2,
            bootstrap_required=False,
        )
        with mock.patch.object(recovered, "checkpoint"):
            recovered._reconcile_cleanup(1)
        self.assertEqual(
            recovered.aggregate["tasks"][0]["resource_state"], "cleaned")
        self.assertFalse(executor.worktree_path(1).exists())

    def test_cleanup_retry_journals_operator_removed_resource(self):
        executor = self.true_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="provisioning")
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="running")
        aggregate = parallel_state.set_terminal_intent(
            aggregate, "cancelled")
        aggregate = parallel_state.transition_run_status(
            aggregate, "cancel_requested")
        aggregate = parallel_state.transition_task(
            aggregate, 1, outcome="cancelled", resource_state="exited",
            explicit_abort=True)
        supervisor = self.supervisor(aggregate, executor)
        dirty = executor.worktree_path(1) / "dirty.txt"
        dirty.write_text("dirty\n", encoding="utf-8")
        with (mock.patch.object(supervisor, "checkpoint"),
              self.assertRaisesRegex(parallel.ParallelError, "cleanup failed")):
            supervisor._cleanup_terminal_task(1)
        blocked = repo_executor.RepoExecutor._read_json(
            executor.lease_path, "test blocked remove before manual cleanup")

        git(self.repo, "worktree", "remove", "--force",
            str(executor.worktree_path(1)))
        git(self.repo, "update-ref", "-d", executor.task_ref(1))
        recovered = parallel.ParallelSupervisor(
            workspace_root=self.workspace_root,
            workspace=mock.Mock(),
            artifacts=self.artifacts,
            aggregate=supervisor.aggregate,
            executor=executor,
            pending_launch_hash=PENDING_LAUNCH_HASH,
            session="6" * 32,
            generation=2,
            bootstrap_required=False,
        )
        with mock.patch.object(recovered, "checkpoint"):
            recovered._reconcile_cleanup(1)

        self.assertEqual(
            recovered.aggregate["tasks"][0]["resource_state"], "cleaned")
        latest = repo_executor.RepoExecutor._read_json(
            executor.lease_path, "test absent cleanup successor")
        self.assertEqual(latest["terminal_status"], "already-removed")
        self.assertEqual(latest["reason"], f"recovered-from:{blocked['nonce']}")

    def test_public_resume_defers_and_supersedes_repaired_cleanup_lease(self):
        workspace, _initial = self.publish_initial_base_projection()
        pending_hash = parallel._pending_launch_hash(self.artifacts)
        spec = parallel.build_repo_spec(
            self.artifacts,
            pending_launch_hash=pending_hash,
            supervisor_session=SESSION,
            generation=1,
        )
        executor = repo_executor.RepoExecutor(spec)
        self.addCleanup(executor.close)
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = parallel.ParallelSupervisor(
            workspace_root=self.workspace_root,
            workspace=workspace,
            artifacts=self.artifacts,
            aggregate=aggregate,
            executor=executor,
            pending_launch_hash=pending_hash,
            session=SESSION,
            generation=1,
            bootstrap_required=False,
        )
        supervisor.preflight_and_initialize()
        supervisor._create_worktree(1)
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="provisioning")
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="running")
        aggregate = parallel_state.set_terminal_intent(
            aggregate, "cancelled")
        aggregate = parallel_state.transition_run_status(
            aggregate, "cancel_requested")
        aggregate = parallel_state.transition_task(
            aggregate, 1, outcome="cancelled", resource_state="exited",
            explicit_abort=True)
        supervisor.aggregate = aggregate
        dirty = executor.worktree_path(1) / "dirty.txt"
        dirty.write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(parallel.ParallelError, "cleanup failed"):
            supervisor._cleanup_terminal_task(1)
        supervisor.checkpoint(active=False)
        blocked = repo_executor.RepoExecutor._read_json(
            executor.lease_path, "test public blocked cleanup lease")
        self.assertEqual(blocked["terminal_status"], "blocked")
        self.assertFalse(executor._intent_path(
            "remove", blocked["operation_id"]).exists())
        executor.close()
        dirty.unlink()

        observed = {}

        def finish_after_reconcile(recovered):
            observed["supervisor"] = recovered
            return 0

        with mock.patch.object(
                parallel.ParallelSupervisor, "abort",
                autospec=True, side_effect=finish_after_reconcile):
            result = parallel.control_existing_parallel(
                self.workspace_root, workspace.name, "resume")

        self.assertEqual(result, 0)
        recovered = observed["supervisor"]
        self.assertEqual(
            recovered.aggregate["tasks"][0]["resource_state"], "cleaned")
        self.assertFalse(recovered.executor.worktree_path(1).exists())
        latest = repo_executor.RepoExecutor._read_json(
            recovered.executor.lease_path, "test superseded cleanup lease")
        self.assertEqual(
            latest["operation"], repo_executor.Operation.REMOVE_WORKTREE.value)
        self.assertIn(latest["terminal_status"], {"removed", "already-removed"})
        self.assertEqual(latest["reason"], f"recovered-from:{blocked['nonce']}")

    def test_claimed_abort_dominates_later_resume_and_is_acknowledged(self):
        spool = parallel_spool.DurableSpool(
            self.artifacts.run_dir / "controls")
        request_id = "c" * 32
        request = {
            "schema": 1,
            "request_id": request_id,
            "run_id": RUN_ID,
            "action": "abort",
            "supervisor_session": SESSION,
            "supervisor_generation": 1,
            "control_generation": 1,
            "aggregate_version": 0,
        }
        spool.publish_request(request_id, request)
        self.assertTrue(spool.claim_request(request_id).transitioned)

        initial = self.aggregate_running()
        (recovered_spool, records, action,
         control_generation) = parallel._claimed_control_recovery(
            self.artifacts, initial, next_generation=2)
        aggregate = parallel._apply_claimed_control(
            initial, action, control_generation)
        effective = parallel._effective_recovery_action("resume", action)
        aggregate = parallel._prepare_recovery_status(aggregate, effective)
        self.assertEqual(effective, "abort")
        self.assertEqual(aggregate["control_generation"], 1)
        self.assertEqual(aggregate["terminal_intent"], "cancelled")
        self.assertEqual(aggregate["status"], "cancel_requested")

        parallel._publish_recovered_control_responses(
            recovered_spool, records, run_id=RUN_ID)
        response = spool.get_response(request_id)
        self.assertEqual(response.payload["status"], "accepted")

    def test_post_quiescence_pause_cancels_late_exact_pending_gate(self):
        request_id = "1" * 32
        spool = parallel_spool.DurableSpool(
            self.artifacts.run_dir / "requests",
            responses_root=self.artifacts.run_dir / "responses")
        request = self.gate_request(request_id, self.start)
        self.retained_worker_state(request, status="running")
        spool.publish_request(request_id, request)
        aggregate = self.aggregate_running()
        aggregate = parallel_state.transition_run_status(
            aggregate, "pause_requested")
        aggregate = parallel_state.advance_pause_generation(aggregate)
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="pausing")
        aggregate = parallel_state.transition_task(
            aggregate, 1, resource_state="paused")
        aggregate = parallel_state.transition_run_status(
            aggregate, "paused")
        supervisor = self.supervisor(aggregate, mock.Mock())

        with mock.patch.object(supervisor, "checkpoint"):
            supervisor._cancel_pending_gates(
                abort=False, recovery=True)

        self.assertEqual(spool.get_request(request_id).state, "cancelled")
        rc, payload = parallel_gate._validate_durable_response(
            spool.get_response(request_id), request)
        self.assertEqual((rc, payload["status"]), (20, "paused"))

    def test_post_quiescence_abort_cancels_late_exact_pending_gate(self):
        request_id = "2" * 32
        spool = parallel_spool.DurableSpool(
            self.artifacts.run_dir / "requests",
            responses_root=self.artifacts.run_dir / "responses")
        request = self.gate_request(request_id, self.start)
        self.retained_worker_state(request, status="running")
        spool.publish_request(request_id, request)
        aggregate = self.aggregate_running()
        aggregate = parallel_state.set_terminal_intent(
            aggregate, "cancelled")
        aggregate = parallel_state.transition_run_status(
            aggregate, "cancel_requested")
        aggregate = parallel_state.transition_task(
            aggregate, 1, outcome="cancelled", resource_state="exited",
            explicit_abort=True)
        supervisor = self.supervisor(aggregate, mock.Mock())

        with mock.patch.object(supervisor, "checkpoint"):
            supervisor._cancel_pending_gates(
                abort=True, recovery=True)

        self.assertEqual(spool.get_request(request_id).state, "cancelled")
        rc, payload = parallel_gate._validate_durable_response(
            spool.get_response(request_id), request)
        self.assertEqual((rc, payload["status"]), (21, "cancelled"))

    def test_supervisor_generation_claim_survives_base_projection_lag(self):
        parallel._initialize_supervisor_generation(
            self.artifacts, session=SESSION)
        parallel._claim_supervisor_generation(
            self.artifacts,
            expected_generation=1,
            generation=2,
            session="6" * 32,
        )
        authority = parallel._read_supervisor_generation(self.artifacts)
        self.assertEqual(authority["generation"], 2)
        self.assertEqual(authority["session"], "6" * 32)
        parallel._claim_supervisor_generation(
            self.artifacts,
            expected_generation=2,
            generation=3,
            session="7" * 32,
        )
        self.assertEqual(
            parallel._read_supervisor_generation(
                self.artifacts)["generation"],
            3,
        )

    def test_claimed_bootstrap_abort_survives_generation_crash_and_dominates_resume(self):
        workspace, _aggregate = self.publish_initial_base_projection()
        shutil.rmtree(loop_mod.Workspace(
            self.artifacts.assignments[1]["worker_workspace"]).dir)
        existing = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        intent = parallel._install_bootstrap_control(existing, "abort")
        claimed = parallel._claim_bootstrap_control(
            existing,
            intent["request_id"],
            session="6" * 32,
            generation=2,
            assigned_control_generation=1,
        )
        self.assertEqual(claimed["state"], "claimed")
        parallel._claim_supervisor_generation(
            self.artifacts,
            expected_generation=1,
            generation=2,
            session="6" * 32,
        )
        # Simulate a hard crash after generation CAS but before any aggregate
        # or base checkpoint.  A later Resume must adopt the claimed Abort.
        result = parallel.control_existing_parallel(
            self.workspace_root, workspace.name, "resume")
        recovered = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        self.assertEqual(result, 0, recovered.aggregate)
        self.assertEqual(recovered.aggregate["status"], "cancelled")
        self.assertEqual(recovered.aggregate["terminal_intent"], "cancelled")
        self.assertEqual(recovered.aggregate["control_generation"], 1)
        self.assertEqual(recovered.generation, 3)
        durable = parallel._read_bootstrap_control(self.artifacts)
        self.assertEqual(durable["action"], "abort")
        self.assertEqual(durable["state"], "applied")
        launches = self.artifacts.run_dir / "launches"
        for state in ("pending", "claimed"):
            self.assertFalse(any((launches / state).glob("*.json")))

    def test_claimed_bootstrap_reconstructs_apply_after_checkpoint_crash(self):
        workspace, aggregate = self.publish_initial_base_projection()
        shutil.rmtree(loop_mod.Workspace(
            self.artifacts.assignments[1]["worker_workspace"]).dir)
        existing = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        intent = parallel._install_bootstrap_control(existing, "abort")
        claimed = parallel._claim_bootstrap_control(
            existing,
            intent["request_id"],
            session="6" * 32,
            generation=2,
            assigned_control_generation=1,
        )
        parallel._claim_supervisor_generation(
            self.artifacts,
            expected_generation=1,
            generation=2,
            session="6" * 32,
        )
        aggregate = parallel._apply_bootstrap_control(aggregate, claimed)
        aggregate = parallel._prepare_recovery_status(aggregate, "abort")
        parallel_state.atomic_write_json(
            self.artifacts.run_dir, "aggregate.json", aggregate)
        workspace.save_state(parallel.project_base_state(
            workspace,
            self.artifacts,
            aggregate,
            (),
            supervisor_pid=None,
            supervisor_session=None,
            supervisor_generation=2,
        ))
        # Crash before bootstrap.json is acknowledged.  Replay is idempotent
        # even though the aggregate already contains the control generation.
        result = parallel.control_existing_parallel(
            self.workspace_root, workspace.name, "resume")
        recovered = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        self.assertEqual(result, 0, recovered.aggregate)
        self.assertEqual(recovered.aggregate["status"], "cancelled")
        self.assertEqual(recovered.aggregate["control_generation"], 1)
        self.assertEqual(
            parallel._read_bootstrap_control(self.artifacts)["state"],
            "applied",
        )

    def test_first_pending_bootstrap_intent_rejects_conflicting_action(self):
        workspace, _aggregate = self.publish_initial_base_projection()
        existing = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        first = parallel._install_bootstrap_control(existing, "resume")
        duplicate = parallel._install_bootstrap_control(existing, "resume")
        self.assertEqual(duplicate["request_id"], first["request_id"])
        with self.assertRaisesRegex(parallel.ParallelError, "conflict"):
            parallel._install_bootstrap_control(existing, "abort")

        durable = parallel._read_bootstrap_control(self.artifacts)
        self.assertEqual(durable["request_id"], first["request_id"])
        self.assertEqual(durable["action"], "resume")
        self.assertEqual(durable["state"], "pending")
        self.assertEqual(
            parallel._read_supervisor_generation(
                self.artifacts)["generation"],
            1,
        )

    def test_unclaimed_stale_bootstrap_is_superseded_under_short_lock(self):
        workspace, aggregate = self.publish_initial_base_projection()
        existing = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        stale = parallel._install_bootstrap_control(existing, "resume")
        parallel._claim_supervisor_generation(
            self.artifacts,
            expected_generation=1,
            generation=2,
            session="6" * 32,
        )
        aggregate = parallel.save_aggregate(
            self.artifacts.run_dir, aggregate, self.artifacts.plan)
        workspace.save_state(parallel.project_base_state(
            workspace,
            self.artifacts,
            aggregate,
            (),
            supervisor_pid=None,
            supervisor_session=None,
            supervisor_generation=2,
        ))
        current = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)

        replacement = parallel._install_bootstrap_control(current, "abort")

        self.assertNotEqual(replacement["request_id"], stale["request_id"])
        self.assertEqual(replacement["action"], "abort")
        self.assertEqual(replacement["state"], "pending")
        self.assertEqual(replacement["expected_supervisor_generation"], 2)
        self.assertEqual(replacement["expected_aggregate_version"], 1)

    def test_missing_resume_secret_blocks_without_validator_or_ref_mutation_when_pristine(self):
        workspace, _aggregate = self.publish_initial_base_projection()
        shutil.rmtree(loop_mod.Workspace(
            self.artifacts.assignments[1]["worker_workspace"]).dir)
        with (mock.patch.object(
                  parallel, "missing_required_secret_names",
                  return_value=("API_TOKEN",)),
              mock.patch.object(
                  parallel.ParallelSupervisor, "preflight_and_initialize",
                  side_effect=AssertionError("validator/startup mutation ran"))):
            result = parallel.control_existing_parallel(
                self.workspace_root, workspace.name, "resume")

        recovered = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        self.assertEqual(result, 2, recovered.aggregate)
        self.assertEqual(recovered.aggregate["status"], "blocked")
        self.assertIn("API_TOKEN", recovered.aggregate["error"])
        self.assertIsNone(recovered.state["loop"]["pid"])
        self.assertEqual(recovered.generation, 2)
        self.assertEqual(
            parallel._read_bootstrap_control(self.artifacts)["state"],
            "applied",
        )
        self.assertNotEqual(
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet",
                 parallel_contract.integration_ref_for(RUN_ID)],
                cwd=self.repo, check=False).returncode,
            0,
        )

    def test_missing_secret_does_not_fake_idle_over_live_executor_lease(self):
        workspace, _aggregate = self.publish_initial_base_projection()
        executor = self.pristine_executor()
        supervisor = self.supervisor(
            parallel_state.build_initial_aggregate(
                RUN_ID, self.artifacts.plan), executor)
        self.reserve_operation(executor, supervisor._preflight_request())
        aggregate_before = (
            self.artifacts.run_dir / "aggregate.json").read_bytes()
        state_before = workspace.state_path.read_bytes()

        with (mock.patch.object(
                  parallel, "missing_required_secret_names",
                  return_value=("API_TOKEN",)),
              self.assertRaisesRegex(
                  parallel.ParallelError, "lock|occupied|占用")):
            parallel.control_existing_parallel(
                self.workspace_root, workspace.name, "resume")

        self.assertEqual(
            (self.artifacts.run_dir / "aggregate.json").read_bytes(),
            aggregate_before,
        )
        self.assertEqual(workspace.state_path.read_bytes(), state_before)
        self.assertEqual(
            parallel._read_bootstrap_control(self.artifacts)["state"],
            "claimed",
        )
        self.assertIsNotNone(executor._global_lock_file)

    def test_held_run_lock_with_partial_owner_metadata_blocks_bootstrap_publish(self):
        workspace, _aggregate = self.publish_initial_base_projection()
        existing = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        lock_path = workspace.dir / ".run.lock"
        fd = loop_mod._open_regular(lock_path, os.O_RDWR | os.O_CREAT)
        stream = os.fdopen(fd, "a+b", closefd=True)
        compat.lock_file(stream, blocking=False)
        try:
            stream.seek(0)
            stream.truncate()
            stream.flush()
            with self.assertRaisesRegex(
                    parallel.ParallelError, "metadata|owner"):
                parallel._install_bootstrap_control(existing, "resume")
        finally:
            compat.unlock_file(stream)
            stream.close()
        self.assertFalse(
            (self.artifacts.run_dir / "controls" / "bootstrap.json").exists())

    def test_illegal_finalizing_abort_does_not_publish_or_advance_authority(self):
        workspace, aggregate = self.publish_initial_base_projection()
        aggregate = parallel_state.transition_run_status(aggregate, "running")
        aggregate = parallel_state.set_terminal_intent(aggregate, "completed")
        aggregate = parallel_state.transition_run_status(aggregate, "finalizing")
        parallel_state.atomic_write_json(
            self.artifacts.run_dir, "aggregate.json", aggregate)
        workspace.save_state(parallel.project_base_state(
            workspace,
            self.artifacts,
            aggregate,
            (),
            supervisor_pid=None,
            supervisor_session=None,
            supervisor_generation=1,
        ))
        generation_before = (
            self.artifacts.run_dir / "supervisor-generation.json").read_bytes()
        aggregate_before = (
            self.artifacts.run_dir / "aggregate.json").read_bytes()
        state_before = workspace.state_path.read_bytes()
        checkpoint_before = workspace.checkpoint_path.read_bytes()

        with self.assertRaisesRegex(
                parallel.ParallelError, "completion finalization"):
            parallel.control_existing_parallel(
                self.workspace_root, workspace.name, "abort")

        self.assertEqual(
            (self.artifacts.run_dir / "supervisor-generation.json").read_bytes(),
            generation_before,
        )
        self.assertEqual(
            (self.artifacts.run_dir / "aggregate.json").read_bytes(),
            aggregate_before,
        )
        self.assertEqual(workspace.state_path.read_bytes(), state_before)
        self.assertEqual(workspace.checkpoint_path.read_bytes(), checkpoint_before)
        self.assertFalse(
            (self.artifacts.run_dir / "controls" / "bootstrap.json").exists())

    def test_claimed_abort_is_not_hidden_by_already_paused_fast_path(self):
        workspace, aggregate = self.publish_initial_base_projection()
        aggregate = parallel_state.transition_run_status(
            aggregate, "pause_requested")
        aggregate = parallel_state.transition_run_status(aggregate, "paused")
        parallel_state.atomic_write_json(
            self.artifacts.run_dir, "aggregate.json", aggregate)
        workspace.save_state(parallel.project_base_state(
            workspace,
            self.artifacts,
            aggregate,
            (),
            supervisor_pid=None,
            supervisor_session=None,
            supervisor_generation=1,
        ))
        shutil.rmtree(loop_mod.Workspace(
            self.artifacts.assignments[1]["worker_workspace"]).dir)
        existing = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        intent = parallel._install_bootstrap_control(existing, "abort")
        parallel._claim_bootstrap_control(
            existing,
            intent["request_id"],
            session="6" * 32,
            generation=2,
            assigned_control_generation=1,
        )
        parallel._claim_supervisor_generation(
            self.artifacts,
            expected_generation=1,
            generation=2,
            session="6" * 32,
        )

        result = parallel.control_existing_parallel(
            self.workspace_root, workspace.name, "pause")

        recovered = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        self.assertEqual(result, 0, recovered.aggregate)
        self.assertEqual(recovered.aggregate["status"], "cancelled")
        self.assertEqual(recovered.aggregate["terminal_intent"], "cancelled")
        self.assertEqual(
            parallel._read_bootstrap_control(self.artifacts)["state"],
            "applied",
        )

    def test_bootstrap_apply_requires_durable_aggregate_and_base_projection(self):
        workspace, aggregate = self.publish_initial_base_projection()
        existing = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        intent = parallel._install_bootstrap_control(existing, "resume")
        claimed = parallel._claim_bootstrap_control(
            existing,
            intent["request_id"],
            session="6" * 32,
            generation=2,
            assigned_control_generation=1,
        )
        fake = dict(aggregate)
        fake["control_generation"] = 1
        with self.assertRaisesRegex(
                parallel.ParallelError, "durable aggregate checkpoint"):
            parallel._mark_bootstrap_control_applied(
                self.artifacts, intent["request_id"], fake)
        self.assertEqual(
            parallel._read_bootstrap_control(self.artifacts)["state"],
            "claimed",
        )

        parallel._claim_supervisor_generation(
            self.artifacts,
            expected_generation=1,
            generation=2,
            session="6" * 32,
        )
        aggregate = parallel._apply_bootstrap_control(aggregate, claimed)
        aggregate = parallel._prepare_recovery_status(aggregate, "resume")
        aggregate = parallel.save_aggregate(
            self.artifacts.run_dir, aggregate, self.artifacts.plan)
        # Aggregate has landed but base projection still trails it.
        with self.assertRaisesRegex(
                parallel.ParallelError, "durable aggregate checkpoint"):
            parallel._mark_bootstrap_control_applied(
                self.artifacts, intent["request_id"], aggregate)
        workspace.save_state(parallel.project_base_state(
            workspace,
            self.artifacts,
            aggregate,
            (),
            supervisor_pid=os.getpid(),
            supervisor_session="6" * 32,
            supervisor_generation=2,
        ))
        applied = parallel._mark_bootstrap_control_applied(
            self.artifacts, intent["request_id"], aggregate)
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(applied["applied"]["aggregate_version"], 1)

    def test_startup_projection_exists_before_repo_executor_construction(self):
        plan_path = self.artifacts.run_dir / "plan.json"
        args = SimpleNamespace(
            repo=str(self.repo), name="startup-base",
            import_plan=str(plan_path), goal="goal.md", plan_doc="",
            agent_cmd=compat.join_command([sys.executable, "-c", "pass"]),
            validate_cmd=compat.join_command([sys.executable, "-c", "pass"]),
            flag_threshold=2, done_threshold=1, red_limit=3,
            stall_limit=4, stuck_stop=False, stuck_stop_count=5,
            round_timeout=1.0, agent_backoff_max=1.0,
            validate_timeout=5.0, notify_cmd="", max_parallel=1,
            worker_restart_limit=1,
        )
        launcher_fence = mock.Mock()
        run_id = "b2c3d4e5"

        with (mock.patch.object(parallel, "_new_run_id", return_value=run_id),
              mock.patch.object(
                  parallel, "_repository_start_identity",
                  return_value=(self.primary_ref, self.start)),
              mock.patch.object(
                  parallel.repo_owner.RepoOwnerFence, "claim",
                  return_value=launcher_fence),
              mock.patch.object(
                  parallel.repo_executor, "RepoExecutor",
                  side_effect=RuntimeError("injected constructor crash")),
              self.assertRaisesRegex(RuntimeError, "injected constructor crash")):
            parallel.start_parallel(args, self.workspace_root)

        workspace = loop_mod.Workspace("startup-base")
        state = workspace.load_state()
        self.assertEqual(state["runner"], parallel.SUPERVISOR_RUNNER)
        self.assertEqual(state["parallel"]["run_id"], run_id)
        self.assertEqual(state["parallel"]["status"], "initializing")
        self.assertEqual(state["parallel"]["aggregate_version"], 0)
        self.assertEqual(state["parallel"]["supervisor_generation"], 1)
        existing = parallel._load_existing_parallel_run(
            self.workspace_root, "startup-base")
        self.assertEqual(existing.artifacts.manifest["run_id"], run_id)
        self.assertEqual(existing.aggregate["status"], "initializing")
        self.assertIsNone(parallel._active_supervisor_owner(existing))
        launcher_fence.terminalize.assert_called_once_with(
            "parallel-launch-ready-for-executor")
        launcher_fence.close.assert_called_once()

        recovery_result = parallel.control_existing_parallel(
            self.workspace_root, "startup-base", "abort")
        recovered = parallel._load_existing_parallel_run(
            self.workspace_root, "startup-base")
        self.assertEqual(recovery_result, 0, recovered.aggregate)
        self.assertEqual(recovered.aggregate["status"], "cancelled")
        self.assertEqual(recovered.generation, 2)

    def test_stale_launcher_owner_requires_explicit_recover_owner_before_abort(self):
        workspace, _aggregate = self.publish_initial_base_projection()
        # This fixture normally pre-creates a provisioned worker directory for
        # retained-worker recovery tests.  The launcher crash window is earlier:
        # no worker container or workspace has been provisioned yet.
        shutil.rmtree(loop_mod.Workspace(
            self.artifacts.assignments[1]["worker_workspace"]).dir)
        launcher = repo_owner.RepoOwnerFence.claim(
            self.repo,
            owner_kind=repo_owner.OwnerKind.PARALLEL_LAUNCHER,
            workspace=workspace.dir,
            state_path=workspace.state_path,
            session=SESSION,
            owner_identity={
                "pid": 2_147_483_647,
                "creation_token": "definitely-gone-parallel-launcher",
            },
            boot_identity=repo_owner.host_boot_identity(),
        )
        launcher.close()  # hard-crash simulation: active marker remains
        state_before = workspace.state_path.read_bytes()
        checkpoint_before = workspace.checkpoint_path.read_bytes()

        with self.assertRaisesRegex(
                parallel.ParallelError, "explicit recovery|owner marker"):
            parallel.control_existing_parallel(
                self.workspace_root, workspace.name, "abort")

        self.assertEqual(workspace.state_path.read_bytes(), state_before)
        self.assertEqual(workspace.checkpoint_path.read_bytes(), checkpoint_before)
        self.assertEqual(
            repo_owner.RepoOwnerFence.inspect(self.repo)["state"], "active")
        self.assertNotEqual(
            subprocess.run(
                ["git", "show-ref", "--verify", "--quiet",
                 parallel_contract.integration_ref_for(RUN_ID)],
                cwd=self.repo, check=False).returncode,
            0,
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli.command_recover_owner(SimpleNamespace(
                workspace=workspace.name,
                acknowledge_child_gone=True,
                repo=None,
            )), 0)
        self.assertTrue(output.getvalue().strip())
        self.assertEqual(
            repo_owner.RepoOwnerFence.inspect(self.repo)["state"], "terminal")

        recovery_result = parallel.control_existing_parallel(
            self.workspace_root, workspace.name, "abort")
        recovered = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        self.assertEqual(recovery_result, 0, recovered.aggregate)
        self.assertEqual(recovered.aggregate["status"], "cancelled")
        # The failed automatic attempt claimed generation 2; the explicit
        # owner recovery is separate, so the successful Abort owns generation 3.
        self.assertEqual(recovered.generation, 3)

    def test_no_owner_abort_recovers_permanently_blocked_pristine_preflight(self):
        workspace, aggregate = self.publish_initial_base_projection()
        shutil.rmtree(loop_mod.Workspace(
            self.artifacts.assignments[1]["worker_workspace"]).dir)
        pending_hash = parallel._pending_launch_hash(self.artifacts)
        spec = parallel.build_repo_spec(
            self.artifacts,
            pending_launch_hash=pending_hash,
            supervisor_session=SESSION,
            generation=1,
        )
        executor = repo_executor.RepoExecutor(spec)
        supervisor = parallel.ParallelSupervisor(
            workspace_root=self.workspace_root,
            workspace=workspace,
            artifacts=self.artifacts,
            aggregate=aggregate,
            executor=executor,
            pending_launch_hash=pending_hash,
            session=SESSION,
            generation=1,
            bootstrap_required=False,
        )
        request = supervisor._preflight_request()
        lease = self.reserve_operation(executor, request)
        executor._mark_terminal(
            request["operation_id"], lease["request_hash"],
            status="blocked", reason="validator permanently unavailable")
        executor.close()

        with mock.patch.object(
                repo_executor.RepoExecutor, "_preflight",
                side_effect=AssertionError("Abort reran validator")) as validator:
            result = parallel.control_existing_parallel(
                self.workspace_root, workspace.name, "abort")

        validator.assert_not_called()
        recovered = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        self.assertEqual(result, 0, recovered.aggregate)
        self.assertEqual(recovered.aggregate["status"], "cancelled")
        self.assertEqual(recovered.generation, 2)
        self.assertFalse((
            executor.results_dir / f'{request["operation_id"]}.json').exists())
        latest = repo_executor.RepoExecutor._read_json(
            executor.lease_path, "test terminal Abort lease")
        self.assertEqual(
            latest["operation"], repo_executor.Operation.SHUTDOWN.value)
        self.assertEqual(latest["terminal_status"], "shutdown")
        self.assertEqual(
            git(self.repo, "rev-parse", executor.sync_ref).stdout.strip(), self.start)

    def test_pristine_abort_survives_crash_after_bootstrap_apply(self):
        workspace, aggregate = self.publish_initial_base_projection()
        shutil.rmtree(loop_mod.Workspace(
            self.artifacts.assignments[1]["worker_workspace"]).dir)
        pending_hash = parallel._pending_launch_hash(self.artifacts)
        spec = parallel.build_repo_spec(
            self.artifacts,
            pending_launch_hash=pending_hash,
            supervisor_session=SESSION,
            generation=1,
        )
        executor = repo_executor.RepoExecutor(spec)
        supervisor = parallel.ParallelSupervisor(
            workspace_root=self.workspace_root, workspace=workspace,
            artifacts=self.artifacts, aggregate=aggregate, executor=executor,
            pending_launch_hash=pending_hash, session=SESSION, generation=1,
            bootstrap_required=False,
        )
        request = supervisor._preflight_request()
        lease = self.reserve_operation(executor, request)
        executor._mark_terminal(
            request["operation_id"], lease["request_hash"],
            status="blocked", reason="validator permanently unavailable")
        executor.close()

        with mock.patch.object(
                parallel.ParallelSupervisor, "reconcile_existing",
                side_effect=KeyboardInterrupt):
            first = parallel.control_existing_parallel(
                self.workspace_root, workspace.name, "abort")
        interrupted = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        self.assertEqual(first, 2)
        self.assertEqual(interrupted.aggregate["status"], "blocked")
        self.assertEqual(
            interrupted.aggregate["terminal_intent"], "cancelled")
        old_bootstrap = parallel._read_bootstrap_control(self.artifacts)
        self.assertEqual(old_bootstrap["state"], "applied")

        second = parallel.control_existing_parallel(
            self.workspace_root, workspace.name, "resume")
        recovered = parallel._load_existing_parallel_run(
            self.workspace_root, workspace.name)
        self.assertEqual(second, 0, recovered.aggregate)
        self.assertEqual(recovered.aggregate["status"], "cancelled")
        new_bootstrap = parallel._read_bootstrap_control(self.artifacts)
        self.assertNotEqual(
            new_bootstrap["request_id"], old_bootstrap["request_id"])
        self.assertEqual(new_bootstrap["state"], "applied")

    def test_pristine_startup_recovery_replays_preflight_and_initialize_once(self):
        executor = self.pristine_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)

        self.assertTrue(supervisor.recover_startup_initialization())
        self.assertFalse(supervisor.recover_startup_initialization())

        audit = executor.audit_recovery_state()
        self.assertEqual(audit["primary_sha"], self.start)
        self.assertEqual(audit["sync_sha"], self.start)
        initialize_id = parallel._operation_id(RUN_ID, "initialize-refs")
        self.assertTrue((
            executor.intents_dir / f"init-{initialize_id}.json").is_file())
        self.assertTrue((
            executor.receipts_dir / f"init-{initialize_id}.json").is_file())
        self.assertTrue((
            executor.results_dir / f"{initialize_id}.json").is_file())

    def test_startup_recovery_after_preflight_reuses_exact_result(self):
        executor = self.pristine_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)
        preflight_id = parallel._operation_id(RUN_ID, "preflight")
        executor.execute({
            "operation": repo_executor.Operation.PREFLIGHT.value,
            "operation_id": preflight_id,
            "authority": {"pending_launch_hash": PENDING_LAUNCH_HASH},
            "expected": {
                "head_ref": self.primary_ref,
                "head_sha": self.start,
            },
        })
        preflight_result = executor.results_dir / f"{preflight_id}.json"
        original_result = preflight_result.read_bytes()

        self.assertTrue(supervisor.recover_startup_initialization())

        self.assertEqual(preflight_result.read_bytes(), original_result)
        self.assertEqual(executor.audit_recovery_state()["sync_sha"], self.start)

    def test_partial_startup_ref_without_receipt_fails_closed(self):
        executor = self.pristine_executor()
        git(self.repo, "update-ref", executor.sync_ref, self.start)
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)

        with (mock.patch.object(supervisor, "preflight_and_initialize") as replay,
              self.assertRaisesRegex(
                  parallel.ParallelError,
                  "partial/unknown startup initialization evidence")):
            supervisor.recover_startup_initialization()
        replay.assert_not_called()

    def test_corrupt_complete_startup_receipt_fails_closed(self):
        executor = self.pristine_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)
        self.assertTrue(supervisor.recover_startup_initialization())
        initialize_id = parallel._operation_id(RUN_ID, "initialize-refs")
        parallel_state.atomic_write_json(
            executor.receipts_dir, f"init-{initialize_id}.json", {})

        with self.assertRaisesRegex(
                parallel.ParallelError,
                "startup initialization success audit blocked|schema mismatch"):
            supervisor.recover_startup_initialization()

    def test_pristine_startup_rejects_same_run_shutdown_lease(self):
        first = self.pristine_executor()
        first.execute({
            "operation": repo_executor.Operation.SHUTDOWN.value,
            "operation_id": parallel._operation_id(RUN_ID, "shutdown", 1),
            "authority": {
                "supervisor_session": SESSION,
                "generation": 1,
            },
            "expected": {"idle": True},
        })
        replacement = repo_executor.RepoExecutor(first.spec)
        self.addCleanup(replacement.close)
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, replacement)

        with (mock.patch.object(supervisor, "preflight_and_initialize") as replay,
              self.assertRaisesRegex(
                  parallel.ParallelError, "non-PREFLIGHT same-run lease")):
            supervisor.recover_startup_initialization()
        replay.assert_not_called()
        self.assertIsNone(replacement._ref_tip(replacement.sync_ref))

    def test_pristine_startup_rejects_noncanonical_pending_create_before_replay(self):
        executor = self.pristine_executor()
        request = {
            "operation": repo_executor.Operation.CREATE_WORKTREE.value,
            "operation_id": parallel._operation_id(RUN_ID, "create", 1),
            "task": 1,
            "authority": {
                "manifest_hash": self.artifacts.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[1],
            },
            "expected": {
                "base_sha": self.start,
                "task_ref_absent": True,
                "worktree_absent": True,
            },
        }
        self.reserve_operation(executor, request)
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)

        with self.assertRaisesRegex(
                parallel.ParallelError, "non-canonical pending/blocked lease"):
            supervisor.recover_startup_initialization(
                reconcile_pending=True)

        self.assertIsNone(executor._ref_tip(executor.sync_ref))
        self.assertIsNone(executor._ref_tip(executor.task_ref(1)))
        observation = executor.observe_worktree(1)
        self.assertFalse(observation["exists"])
        self.assertFalse(observation["registered"])

    def test_recovery_flow_classifies_startup_before_generic_create_replay(self):
        workspace, _aggregate = self.publish_initial_base_projection()
        pending_hash = parallel._pending_launch_hash(self.artifacts)
        spec = parallel.build_repo_spec(
            self.artifacts,
            pending_launch_hash=pending_hash,
            supervisor_session=SESSION,
            generation=1,
        )
        executor = repo_executor.RepoExecutor(spec)
        request = {
            "operation": repo_executor.Operation.CREATE_WORKTREE.value,
            "operation_id": parallel._operation_id(RUN_ID, "create", 1),
            "task": 1,
            "authority": {
                "manifest_hash": self.artifacts.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[1],
            },
            "expected": {
                "base_sha": self.start,
                "task_ref_absent": True,
                "worktree_absent": True,
            },
        }
        self.reserve_operation(executor, request)
        executor.close()

        with self.assertRaisesRegex(
                parallel.ParallelError, "non-canonical pending/blocked lease"):
            parallel.control_existing_parallel(
                self.workspace_root, workspace.name, "abort")

        lease = repo_executor.RepoExecutor._read_json(
            executor.lease_path, "test retained CREATE lease")
        self.assertEqual(lease["state"], "reserved")
        self.assertEqual(
            lease["operation"], repo_executor.Operation.CREATE_WORKTREE.value)
        show_ref = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", executor.task_ref(1)],
            cwd=self.repo, check=False)
        self.assertNotEqual(show_ref.returncode, 0)
        self.assertFalse(executor.worktree_path(1).exists())

    def test_pending_canonical_preflight_is_fenced_then_initialized(self):
        executor = self.pristine_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)
        self.reserve_operation(executor, supervisor._preflight_request())

        self.assertTrue(supervisor.recover_startup_initialization(
            reconcile_pending=True))
        self.assertEqual(executor.audit_recovery_state()["sync_sha"], self.start)

    def test_blocked_canonical_preflight_exact_replay_can_initialize(self):
        executor = self.pristine_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)
        request = supervisor._preflight_request()
        lease = self.reserve_operation(executor, request)
        executor._mark_terminal(
            request["operation_id"], lease["request_hash"],
            status="blocked", reason="transient validator failure")

        self.assertTrue(supervisor.recover_startup_initialization(
            reconcile_pending=True))

        audit = executor.audit_recovery_state()
        self.assertEqual(audit["sync_sha"], self.start)
        preflight_result = executor._read_json(
            executor.results_dir / f'{request["operation_id"]}.json',
            "test recovered PREFLIGHT result")
        self.assertEqual(preflight_result["result"]["status"], "validated")

    def test_pristine_abort_bypasses_blocked_validator_but_initializes_sync_ref(self):
        executor = self.pristine_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)
        request = supervisor._preflight_request()
        lease = self.reserve_operation(executor, request)
        executor._mark_terminal(
            request["operation_id"], lease["request_hash"],
            status="blocked", reason="validator remains unavailable")
        control = self.publish_claimed_abort_control(aggregate)

        with mock.patch.object(executor, "_preflight") as validator:
            self.assertTrue(supervisor.recover_startup_initialization(
                reconcile_pending=True, pristine_abort_control=control))

        validator.assert_not_called()
        self.assertFalse((
            executor.results_dir / f'{request["operation_id"]}.json').exists())
        self.assertEqual(executor._ref_tip(executor.sync_ref), self.start)
        latest = executor._read_json(
            executor.lease_path, "test pristine Abort INIT lease")
        self.assertEqual(
            latest["operation"],
            repo_executor.Operation.INITIALIZE_RUN_REFS.value)
        self.assertEqual(latest["terminal_status"], "initialized")

    def test_unvalidated_pending_init_without_abort_authority_is_rejected(self):
        executor = self.pristine_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)
        control = self.publish_claimed_abort_control(aggregate)
        self.reserve_operation(executor, supervisor._initialize_refs_request())

        with self.assertRaisesRegex(
                parallel.ParallelError,
                "pristine Abort initialization authority unavailable"):
            supervisor.recover_startup_initialization(
                reconcile_pending=True, pristine_abort_control=control)

        self.assertIsNone(executor._ref_tip(executor.sync_ref))

    def test_pristine_abort_marker_cannot_switch_claim_before_cancel_checkpoint(self):
        executor = self.pristine_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)
        preflight = supervisor._preflight_request()
        initialize = supervisor._initialize_refs_request()
        lease = self.reserve_operation(executor, preflight)
        executor._mark_terminal(
            preflight["operation_id"], lease["request_hash"],
            status="blocked", reason="validator unavailable")
        blocked = executor._read_json(
            executor.lease_path, "test blocked PREFLIGHT lease")
        first = self.publish_claimed_abort_control(aggregate)
        authority = supervisor._publish_pristine_abort_init_authority(
            preflight, initialize, blocked, first)
        tampered = dict(authority)
        tampered["blocked_preflight_lease_hash"] = "0" * 64
        self.assertFalse(
            supervisor._authorize_pristine_abort_init_supersession(
                blocked, tampered, preflight, initialize))

        second = dict(first)
        second["request_id"] = "e" * 32
        second["claimed_by"] = {
            "session": "7" * 32, "generation": 3,
            "claimed_at": "2026-07-22T00:00:02+00:00",
        }
        parallel._write_bootstrap_control(self.artifacts, second)
        with self.assertRaisesRegex(
                parallel.ParallelError,
                "pristine Abort initialization authority mismatch"):
            supervisor._audit_pristine_abort_init_authority(
                preflight, initialize, second)

    def test_pristine_abort_init_crash_after_ref_replays_from_immutable_authority(self):
        executor = self.pristine_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)
        preflight = supervisor._preflight_request()
        lease = self.reserve_operation(executor, preflight)
        executor._mark_terminal(
            preflight["operation_id"], lease["request_hash"],
            status="blocked", reason="validator remains unavailable")
        control = self.publish_claimed_abort_control(aggregate)
        armed = {"initialize.after_ref"}

        def inject(point):
            if point in armed:
                armed.remove(point)
                raise RuntimeError("crash after private sync ref")

        executor._fault_injector = inject
        with self.assertRaisesRegex(
                parallel.ParallelError, "unexpected failure"):
            supervisor.recover_startup_initialization(
                reconcile_pending=True, pristine_abort_control=control)
        self.assertTrue((
            self.artifacts.run_dir
            / "startup/pristine-abort-init.json").is_file())
        interrupted = executor._read_json(
            executor.lease_path, "test interrupted Abort INIT lease")
        self.assertEqual(
            interrupted["operation"],
            repo_executor.Operation.INITIALIZE_RUN_REFS.value)
        self.assertEqual(interrupted["terminal_status"], "blocked")
        executor.close()

        recovered_executor = repo_executor.RepoExecutor(executor.spec)
        self.addCleanup(recovered_executor.close)
        recovered = self.supervisor(aggregate, recovered_executor)
        with mock.patch.object(recovered_executor, "_preflight") as validator:
            recovered.recover_startup_initialization(
                reconcile_pending=True, pristine_abort_control=control)

        validator.assert_not_called()
        self.assertEqual(
            recovered_executor._ref_tip(recovered_executor.sync_ref), self.start)
        latest = recovered_executor._read_json(
            recovered_executor.lease_path, "test recovered Abort INIT lease")
        self.assertEqual(latest["terminal_status"], "initialized")

    def test_pending_canonical_init_requires_and_uses_preflight_proof(self):
        executor = self.pristine_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)
        executor.execute(supervisor._preflight_request())
        self.reserve_operation(executor, supervisor._initialize_refs_request())

        supervisor.recover_startup_initialization(reconcile_pending=True)

        self.assertEqual(executor.audit_recovery_state()["sync_sha"], self.start)

    def test_pending_canonical_init_without_preflight_proof_fails_closed(self):
        executor = self.pristine_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)
        self.reserve_operation(executor, supervisor._initialize_refs_request())

        with self.assertRaisesRegex(
                parallel.ParallelError, "lacks exact PREFLIGHT proof"):
            supervisor.recover_startup_initialization(reconcile_pending=True)
        self.assertIsNone(executor._ref_tip(executor.sync_ref))

    def test_pristine_startup_binds_preflight_lease_to_result_hash(self):
        executor = self.pristine_executor()
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)
        executor.execute(supervisor._preflight_request())
        lease = executor._read_json(
            executor.lease_path, "test preflight operation lease")
        lease["result_hash"] = "0" * 64
        executor._atomic_json(executor.lease_path, lease)

        with self.assertRaisesRegex(
                parallel.ParallelError, "lease/result hash mismatch"):
            supervisor.recover_startup_initialization()
        self.assertIsNone(executor._ref_tip(executor.sync_ref))

    def test_pristine_startup_rejects_noncanonical_same_run_init_intent(self):
        executor = self.pristine_executor()
        executor._start()
        other_id = "e" * 32
        executor._atomic_json(
            executor.intents_dir / f"init-{other_id}.json",
            {
                "schema_version": 1,
                "kind": repo_executor.Operation.INITIALIZE_RUN_REFS.value,
                "operation_id": other_id,
                "manifest_hash": self.artifacts.manifest_hash,
                "sync_ref": executor.sync_ref,
                "start_sha": self.start,
                "state": "prepared",
                "prepared_at": "2030-01-01T00:00:00+00:00",
            },
        )
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)

        with self.assertRaisesRegex(
                parallel.ParallelError, "conflicting same-run intent"):
            supervisor.recover_startup_initialization()
        self.assertIsNone(executor._ref_tip(executor.sync_ref))

    def test_pristine_startup_recovery_requires_exact_primary_start(self):
        executor = self.pristine_executor()
        (self.repo / "after-start.txt").write_text("moved\n", encoding="utf-8")
        git(self.repo, "add", "after-start.txt")
        git(self.repo, "commit", "-qm", "move primary")
        aggregate = parallel_state.build_initial_aggregate(
            RUN_ID, self.artifacts.plan)
        supervisor = self.supervisor(aggregate, executor)

        with (mock.patch.object(supervisor, "preflight_and_initialize") as replay,
              self.assertRaisesRegex(
                  parallel.ParallelError,
                  "requires exact primary start")):
            supervisor.recover_startup_initialization()
        replay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
