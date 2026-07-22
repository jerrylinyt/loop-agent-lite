"""Real-Git tests for the closed Parallel RepoExecutor core."""

from __future__ import annotations

import copy
import json
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from unittest import mock
from pathlib import Path

from engine import parallel_gate
from engine import parallel_spool
from engine import parallel_state
from engine import repo_executor as executor_mod


RUN_ID = "a1b2c3d4"
SESSION = "9" * 32


def _git(repo: Path, *args: str, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check,
    )


def _run_config(repo: Path) -> dict:
    return {
        "repo": str(repo.resolve()),
        "primary_repo": str(repo.resolve()),
        "goal": "goal.md",
        "plan_doc": "PLAN.md",
        "agent_cmd": "agent --flag",
        "validate_cmd": "validate",
        "flag_threshold": 2,
        "done_threshold": 2,
        "red_limit": 2,
        "stall_limit": 4,
        "stuck_stop": True,
        "stuck_stop_count": 2,
        "round_timeout": 0,
        "validate_timeout": 30,
        "agent_backoff_max": 5,
        "notify_cmd": "",
        "max_parallel": 2,
        "worker_restart_limit": 3,
        "environment": {
            "path_additions": [],
            "non_secret": {"MODE": "test"},
            "required_secret_names": [],
        },
    }


class InjectedCrash(BaseException):
    pass


@unittest.skipUnless(shutil.which("git"), "需要 PATH 上有 git")
class RepoExecutorGitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "primary"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Repo Executor Test")
        _git(self.repo, "config", "user.email", "repo-executor@example.invalid")
        (self.repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
        (self.repo / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
        _git(self.repo, "add", "goal.md", "PLAN.md")
        _git(self.repo, "commit", "-qm", "initial")
        self.start = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.primary_ref = _git(self.repo, "symbolic-ref", "HEAD").stdout.strip()
        self.primary_branch = self.primary_ref.removeprefix("refs/heads/")
        self.workspace_root = self.root / "workspaces"
        self.workspace_root.mkdir()
        self.plan = [
            {"order": 1, "task": "first", "ref": None, "stack": 7},
            {"order": 2, "task": "second", "ref": None, "stack": 7},
        ]
        self.artifacts = parallel_state.materialize_run_artifacts(
            workspace_root=self.workspace_root,
            parent_workspace="base",
            run_id=RUN_ID,
            plan=self.plan,
            run_config=_run_config(self.repo),
            integration_start_sha=self.start,
            integration_branch=self.primary_branch,
            gate_client_cmd="python -m engine.parallel_gate",
            dispatch_tokens={1: "dispatch-token-1", 2: "dispatch-token-2"},
        )
        assignments = {
            order: executor_mod.AssignmentAuthority(
                order=order,
                assignment_hash=self.artifacts.assignment_hashes[order],
                run_config_hash=self.artifacts.run_config_hash,
                launch_spec_hash=self.artifacts.assignment_hashes[order],
            )
            for order in self.artifacts.assignments
        }
        self.spec = executor_mod.ImmutableRepoSpec(
            primary_repo=self.repo,
            workspace_root=self.workspace_root,
            parent_workspace="base",
            run_id=RUN_ID,
            pending_launch_hash="8" * 64,
            manifest_hash=self.artifacts.manifest_hash,
            primary_ref=self.primary_ref,
            integration_start_sha=self.start,
            validator_argv=(sys.executable, "-c", "raise SystemExit(0)"),
            validator_timeout=20,
            supervisor_session=SESSION,
            generation=1,
            assignments=assignments,
        )
        self.counter = 0

    def operation_id(self) -> str:
        self.counter += 1
        return f"{self.counter:032x}"

    def executor(self, *, fault_injector=None, recovery_authorizer=None):
        value = executor_mod.RepoExecutor(
            self.spec,
            fault_injector=fault_injector,
            recovery_authorizer=recovery_authorizer,
        )
        self.addCleanup(value.close)
        return value

    def test_gate_journal_scan_ignores_audited_history_from_prior_run(self):
        executor = self.executor()
        executor._start()
        prior_run = "b1c2d3e4"
        prior_request = "b" * 32
        current_request = "c" * 32
        for directory in (executor.intents_dir, executor.receipts_dir):
            historical = {
                "schema_version": 1,
                "kind": "GATE_MERGE",
                "run_id": prior_run,
                "request_id": prior_request,
                "operation_id": executor_mod.gate_operation_id(
                    prior_run, prior_request),
            }
            (directory / f"gate-{prior_request}.json").write_text(
                json.dumps(historical), encoding="utf-8")
            current = {
                "schema_version": 1,
                "kind": "GATE_MERGE",
                "run_id": RUN_ID,
                "request_id": current_request,
                "operation_id": executor_mod.gate_operation_id(
                    RUN_ID, current_request),
            }
            current_path = directory / f"gate-{current_request}.json"
            current_path.write_text(json.dumps(current), encoding="utf-8")
            self.assertEqual(
                executor._gate_journal_paths(directory, "gate journals"),
                {current_request: current_path},
            )

    def test_start_audits_ordinary_owner_under_held_lock_and_releases_on_failure(self):
        executor = self.executor()
        with mock.patch.object(
                executor_mod.repo_owner,
                "audit_owner_marker_under_global_lock",
                side_effect=executor_mod.repo_owner.OwnerRecoveryRequired("active owner")):
            with self.assertRaisesRegex(executor_mod.LeaseBusy, "active owner"):
                executor.execute(self.preflight_request())
        self.assertFalse(executor._started)
        self.assertIsNone(executor._global_lock_file)
        self.assertFalse(executor.sidecar_root.exists())

        contender = executor._open_regular_lock(executor._global_lock_path)
        try:
            executor_mod.compat.lock_file(contender, blocking=False)
            executor_mod.compat.unlock_file(contender)
        finally:
            contender.close()

        # A failed audit must not poison the instance; explicit owner recovery
        # can be followed by a clean retry.
        with mock.patch.object(
                executor_mod.repo_owner,
                "audit_owner_marker_under_global_lock", return_value=None):
            result = executor.execute(self.preflight_request())
        self.assertEqual(result["status"], "validated")

    def test_sidecar_initialization_failure_releases_global_lock(self):
        executor = self.executor()
        original_reject = executor._reject_link_components

        def reject(path, *, allow_missing=False):
            if Path(path) == executor.sidecar_root:
                raise executor_mod.AuthorityError("injected unsafe sidecar")
            return original_reject(path, allow_missing=allow_missing)

        with mock.patch.object(
                executor, "_reject_link_components", side_effect=reject):
            with self.assertRaisesRegex(
                    executor_mod.AuthorityError, "canonical executor sidecar"):
                executor.execute(self.preflight_request())
        self.assertFalse(executor._started)
        self.assertIsNone(executor._global_lock_file)
        contender = executor._open_regular_lock(executor._global_lock_path)
        try:
            executor_mod.compat.lock_file(contender, blocking=False)
            executor_mod.compat.unlock_file(contender)
        finally:
            contender.close()

    def test_operation_lease_records_exact_executor_and_reaped_child_evidence(self):
        executor = self.executor()
        real_attach = executor_mod.compat.attach_process_group
        with mock.patch.object(
                executor_mod.compat, "attach_process_group",
                wraps=real_attach) as attach:
            executor.execute(self.preflight_request())

        lease = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["schema_version"], 2)
        self.assertEqual(lease["executor_creation_token"],
                         executor._executor_creation_token)
        self.assertEqual(lease["child_state"], "idle")
        self.assertIsNone(lease["child_kind"])
        self.assertIn("validator", {
            entry["kind"] for entry in lease["child_history"]})
        last_child = lease["child_history"][-1]
        self.assertEqual(last_child["kind"], "git")
        self.assertEqual(len(last_child["argv_hash"]), 64)
        self.assertEqual(last_child["identity"]["pid"],
                         last_child["identity"]["group_id"])
        self.assertEqual(
            last_child["identity"]["containment_kind"],
            ("windows-job-no-breakaway-v2"
             if executor_mod.compat.IS_WINDOWS else "process-tree"),
        )
        if executor_mod.compat.IS_WINDOWS:
            self.assertGreater(len(attach.call_args_list), 0)
            for call in attach.call_args_list:
                self.assertEqual(call.kwargs, {"allow_breakaway": False})
        else:
            attach.assert_not_called()
        self.assertEqual(last_child["result"]["status"], "exited")
        self.assertEqual(last_child["result"]["returncode"], 0)

    @unittest.skipUnless(
        sys.platform.startswith("linux") and Path("/proc").is_dir(),
        "verified Linux subreaper containment only",
    )
    def test_natural_validator_exit_fences_detached_descendant_before_reaped(self):
        descendant_pid_file = self.root / "validator-descendant.pid"
        descendant_code = (
            "from pathlib import Path; import os,sys,time; "
            "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii'); "
            "time.sleep(60)"
        )
        validator_code = (
            "from pathlib import Path\n"
            "import subprocess,sys,time\n"
            "target=Path(sys.argv[1])\n"
            "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]], "
            "start_new_session=True, stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)\n"
            "deadline=time.monotonic()+5\n"
            "while not target.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "raise SystemExit(0 if target.exists() else 8)"
        )
        self.spec = replace(
            self.spec,
            validator_argv=(
                sys.executable, "-c", validator_code,
                str(descendant_pid_file), descendant_code,
            ),
        )

        def cleanup_descendant():
            if not descendant_pid_file.exists():
                return
            try:
                pid = int(descendant_pid_file.read_text(encoding="ascii"))
            except (OSError, ValueError):
                return
            if executor_mod.compat.process_is_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (OSError, ProcessLookupError, PermissionError):
                    pass

        self.addCleanup(cleanup_descendant)
        executor = self.executor()
        result = executor.execute(self.preflight_request())
        self.assertEqual(result["status"], "validated")
        descendant_pid = int(
            descendant_pid_file.read_text(encoding="ascii"))
        deadline = time.monotonic() + 5
        while (time.monotonic() < deadline
               and executor_mod.compat.process_is_alive(descendant_pid)):
            time.sleep(0.02)
        self.assertFalse(
            executor_mod.compat.process_is_alive(descendant_pid),
            f"detached validator descendant pid {descendant_pid} survived",
        )
        lease = json.loads(
            executor.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["child_state"], "idle")

    @unittest.skipIf(executor_mod.compat.IS_WINDOWS, "POSIX guardian protocol only")
    def test_subreaper_readiness_failure_never_releases_payload(self):
        sentinel = self.root / "payload-must-not-run"
        wrapper = self.root / "guardian-without-subreaper.py"
        # Force the embedded guardian down its unsupported-platform path in a
        # real child process.  The previous best-effort prctl implementation
        # ignored this condition and would run the sentinel payload after R.
        wrapper.write_text(
            "import sys\n"
            "sys.platform = 'darwin'\n"
            f"exec({executor_mod._POSIX_OPERATION_GUARDIAN!r}, "
            "{'__name__': '__main__'})\n",
            encoding="utf-8",
        )
        barrier_read, barrier_write = os.pipe()
        control_read, control_write = os.pipe()
        status_read, status_write = os.pipe()
        inherited = (barrier_read, control_read, status_write)
        payload_code = (
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_text('ran', encoding='ascii')"
        )
        process = None
        try:
            process = subprocess.Popen(
                [
                    sys.executable, str(wrapper),
                    str(barrier_read), str(control_read), str(status_write),
                    sys.executable, "-c", payload_code, str(sentinel),
                ],
                pass_fds=inherited,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.close(barrier_read)
            barrier_read = None
            os.close(control_read)
            control_read = None
            os.close(status_write)
            status_write = None
            readable, _, _ = select.select([status_read], [], [], 2.0)
            startup = os.read(status_read, 5) if readable else b""
            # Exercise the exact release byte an executor would send.  A
            # readiness failure must remain a pre-payload boundary.
            self.assertEqual(os.write(barrier_write, b"R"), 1)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and not sentinel.exists():
                time.sleep(0.02)
            self.assertEqual(startup, b"F" + struct.pack("!i", 126))
            self.assertFalse(sentinel.exists(), "payload ran without subreaper proof")
        finally:
            for fd in (
                    barrier_read, barrier_write, control_read, control_write,
                    status_read, status_write):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError, PermissionError):
                        process.kill()
                process.wait(timeout=5)

    def test_reconcile_pending_operation_is_idempotent_noop(self):
        executor = self.executor()
        self.assertIsNone(executor.reconcile_pending_operation())
        result = executor.execute(self.preflight_request())
        self.assertEqual(result["status"], "validated")
        self.assertIsNone(executor.reconcile_pending_operation())

    def test_launching_identity_gap_is_durably_blocked(self):
        armed = {"child.after_launching"}

        def inject(point):
            if point in armed:
                armed.remove(point)
                raise InjectedCrash(point)

        executor = self.executor(fault_injector=inject)
        with self.assertRaises(InjectedCrash):
            executor.execute(self.preflight_request())
        lease = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["state"], "running")
        self.assertEqual(lease["child_state"], "launching")
        self.assertIsNone(lease["child_identity"])
        executor.close()
        self.assertFalse(executor_mod.RepoExecutor.fence_recovery_lease(lease))

    def test_running_crash_window_retains_exact_child_identity(self):
        armed = {"child.after_running"}

        def inject(point):
            if point in armed:
                armed.remove(point)
                raise InjectedCrash(point)

        executor = self.executor(fault_injector=inject)
        with self.assertRaises(InjectedCrash):
            executor.execute(self.preflight_request())
        lease = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["state"], "running")
        self.assertEqual(lease["child_state"], "running")
        identity = lease["child_identity"]
        self.assertTrue(identity["start_token"])
        self.assertGreater(identity["group_id"], 1)
        deadline = time.monotonic() + 5
        while (time.monotonic() < deadline
               and executor_mod.compat.process_matches_identity(
                   identity["pid"], identity["start_token"],
                   identity["group_id"], include_zombie=True)):
            time.sleep(0.02)

    @unittest.skipIf(executor_mod.compat.IS_WINDOWS, "POSIX guardian regression")
    def test_disappeared_guardian_without_completion_does_not_forge_reap(self):
        armed = {"child.after_payload_release"}

        def inject(point):
            if point not in armed:
                return
            armed.remove(point)
            lease = json.loads(
                executor.lease_path.read_text(encoding="utf-8"))
            os.kill(lease["child_identity"]["pid"], signal.SIGKILL)
            deadline = time.monotonic() + 3
            while (time.monotonic() < deadline
                   and executor_mod.compat.process_matches_identity(
                       lease["child_identity"]["pid"],
                       lease["child_identity"]["start_token"],
                       lease["child_identity"]["group_id"],
                       include_zombie=True)):
                time.sleep(0.01)
            raise RuntimeError("guardian disappeared before D proof")

        executor = self.executor(fault_injector=inject)
        with self.assertRaises(executor_mod.RepoExecutorError):
            executor.execute(self.preflight_request())
        lease = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["state"], "running")
        self.assertEqual(lease["child_state"], "running")
        self.assertIsNone(lease["child_result"])

    def test_real_executor_hard_exit_is_exactly_fenced_and_recoverable(self):
        request = self.preflight_request()
        spec_path = self.root / "executor-spec.json"
        request_path = self.root / "executor-request.json"
        spec_path.write_text(
            json.dumps(self.spec.hash_material()), encoding="utf-8")
        request_path.write_text(json.dumps(request), encoding="utf-8")
        script = (
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "from engine.repo_executor import RepoExecutor\n"
            "spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
            "request=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))\n"
            "def crash(point):\n"
            "    if point == 'child.after_payload_release': os._exit(91)\n"
            "RepoExecutor(spec, fault_injector=crash).execute(request)\n"
        )
        crashed = subprocess.run(
            [sys.executable, "-c", script, str(spec_path), str(request_path)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, check=False, timeout=30,
        )
        self.assertEqual(crashed.returncode, 91, crashed.stderr)

        probe = self.executor()
        lease = json.loads(probe.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["state"], "running")
        self.assertEqual(lease["child_state"], "running")
        deadline = time.monotonic() + 10
        fenced = False
        while time.monotonic() < deadline:
            fenced = executor_mod.RepoExecutor.fence_recovery_lease(
                lease, graceful_timeout=0.1, force_timeout=1.0)
            if fenced:
                break
            time.sleep(0.05)
        self.assertTrue(fenced, "hard-exited executor child was not exactly fenced")

        recovered = self.executor(
            recovery_authorizer=executor_mod.RepoExecutor.fence_recovery_lease)
        result = recovered.reconcile_pending_operation()
        self.assertEqual(result["status"], "validated")
        terminal = json.loads(
            recovered.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(terminal["state"], "terminal")
        self.assertEqual(terminal["child_state"], "idle")
        self.assertEqual(
            terminal["reason"], f"recovered-from:{lease['nonce']}")

    def test_new_executor_reconciles_non_gate_create_after_explicit_fence(self):
        armed = {"create.after_ref"}

        def inject(point):
            if point in armed:
                armed.remove(point)
                raise InjectedCrash(point)

        first = self.executor(fault_injector=inject)
        self.initialize(first)
        request = {
            "operation": "CREATE_WORKTREE",
            "operation_id": self.operation_id(),
            "task": 1,
            "authority": {
                "manifest_hash": self.spec.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[1],
            },
            "expected": {
                "base_sha": self.start,
                "task_ref_absent": True,
                "worktree_absent": True,
            },
        }
        with self.assertRaises(InjectedCrash):
            first.execute(copy.deepcopy(request))
        old_lease = json.loads(first.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(old_lease["state"], "running")
        self.assertEqual(old_lease["child_state"], "idle")
        first.close()

        observed = []
        recovered = self.executor()
        result = recovered.reconcile_pending_operation(
            recovery_authorizer=lambda lease: observed.append(lease) is None)
        self.assertIn(result["status"], {"created", "already-created"})
        self.assertEqual(observed[0]["nonce"], old_lease["nonce"])
        self.assertTrue(recovered.observe_worktree(1)["exists"])
        terminal = json.loads(
            recovered.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(terminal["state"], "terminal")
        self.assertEqual(terminal["child_state"], "idle")
        self.assertEqual(
            terminal["reason"], f"recovered-from:{old_lease['nonce']}")

    def test_new_executor_reconciles_durable_preflight_without_request_input(self):
        armed = {"child.after_reaped"}

        def inject(point):
            if point in armed:
                armed.remove(point)
                raise InjectedCrash(point)

        first = self.executor(fault_injector=inject)
        request = self.preflight_request()
        with self.assertRaises(InjectedCrash):
            first.execute(copy.deepcopy(request))
        lease = json.loads(first.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["request"], request)
        self.assertEqual(lease["child_state"], "reaped")
        first.close()

        recovered = self.executor()
        result = recovered.reconcile_pending_operation(
            recovery_authorizer=lambda _lease: True)
        self.assertEqual(result["status"], "validated")
        terminal = json.loads(
            recovered.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(terminal["state"], "terminal")
        self.assertEqual(terminal["child_state"], "idle")

    def test_new_executor_reconciles_durable_remove_without_request_input(self):
        armed = set()

        def inject(point):
            if point in armed:
                armed.remove(point)
                raise InjectedCrash(point)

        first = self.executor(fault_injector=inject)
        self.initialize(first)
        created = self.create(first, 1)
        request = {
            "operation": "REMOVE_WORKTREE",
            "operation_id": self.operation_id(),
            "task": 1,
            "authority": {
                "manifest_hash": self.spec.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[1],
            },
            "expected": {
                "terminal_outcome": "cancelled",
                "observation_token": created["observation_token"],
            },
        }
        armed.add("remove.after_worktree")
        with self.assertRaises(InjectedCrash):
            first.execute(copy.deepcopy(request))
        lease = json.loads(first.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["request"], request)
        self.assertEqual(lease["state"], "running")
        first.close()

        recovered = self.executor()
        result = recovered.reconcile_pending_operation(
            recovery_authorizer=lambda _lease: True)
        self.assertIn(result["status"], {"removed", "already-removed"})
        self.assertFalse(recovered.worktree_path(1).exists())
        self.assertIsNone(recovered._ref_tip(recovered.task_ref(1)))

    def test_validator_timeout_fences_background_grandchild_before_terminal(self):
        pid_path = self.root / "validator-grandchild.pid"
        payload = (
            "import pathlib, subprocess, sys, time;"
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid),encoding='ascii');"
            "time.sleep(30)"
        )
        spec = replace(
            self.spec,
            validator_argv=(sys.executable, "-c", payload),
            validator_timeout=0.5,
        )
        executor = executor_mod.RepoExecutor(spec)
        self.addCleanup(executor.close)
        with self.assertRaisesRegex(
                executor_mod.InvariantError, "validator timeout"):
            executor.execute(self.preflight_request())

        self.assertTrue(pid_path.is_file())
        grandchild_pid = int(pid_path.read_text(encoding="ascii"))
        deadline = time.monotonic() + 5
        while (time.monotonic() < deadline
               and executor_mod.compat.process_is_alive(grandchild_pid)):
            time.sleep(0.02)
        self.assertFalse(executor_mod.compat.process_is_alive(grandchild_pid))
        lease = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["state"], "terminal")
        self.assertEqual(lease["terminal_status"], "blocked")
        self.assertEqual(lease["child_state"], "idle")
        self.assertGreaterEqual(len(lease["child_history"]), 4)

    def test_durably_blocked_preflight_exact_replay_can_publish_result(self):
        ready = self.root / "validator-ready"
        payload = (
            "from pathlib import Path; import sys;"
            "raise SystemExit(0 if Path(sys.argv[1]).exists() else 7)"
        )
        spec = replace(
            self.spec,
            validator_argv=(sys.executable, "-c", payload, str(ready)),
        )
        # Even an executor carrying a generic recovery callback may not turn a
        # different operation into an implicit successor of this blocked
        # lease.  Only the dedicated supersession API can do that.
        first = executor_mod.RepoExecutor(
            spec, recovery_authorizer=lambda _lease: True)
        with self.assertRaisesRegex(
                executor_mod.InvariantError, "validator failed rc=7"):
            first.execute(self.preflight_request())
        blocked = json.loads(first.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(blocked["terminal_status"], "blocked")

        initialize = {
            "operation": "INITIALIZE_RUN_REFS",
            "operation_id": self.operation_id(),
            "authority": {"manifest_hash": self.spec.manifest_hash},
            "expected": {
                "integration_start_sha": self.start,
                "sync_ref_absent": True,
            },
        }
        original = first.lease_path.read_bytes()
        with self.assertRaisesRegex(
                executor_mod.LeaseBusy, "explicit fenced supersession"):
            first.execute(copy.deepcopy(initialize))
        self.assertEqual(first.lease_path.read_bytes(), original)

        with self.assertRaisesRegex(
                executor_mod.LeaseBusy, "explicit fenced recovery"):
            first.supersede_blocked_operation(
                copy.deepcopy(initialize),
                recovery_authorizer=lambda _lease: False)
        self.assertEqual(first.lease_path.read_bytes(), original)
        first.close()

        ready.write_text("ready", encoding="ascii")
        recovered = executor_mod.RepoExecutor(spec)
        self.addCleanup(recovered.close)
        result = recovered.reconcile_pending_operation(
            recovery_authorizer=executor_mod.RepoExecutor.fence_recovery_lease)

        self.assertEqual(result["status"], "validated")
        terminal = json.loads(
            recovered.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(terminal["terminal_status"], "validated")
        self.assertIsNotNone(terminal["result_hash"])
        self.assertEqual(terminal["reason"], f"recovered-from:{blocked['nonce']}")

    def test_cached_result_requires_exact_lease_recovery_before_return(self):
        request = self.preflight_request()
        first = self.executor()
        expected_result = first.execute(copy.deepcopy(request))
        lease = json.loads(first.lease_path.read_text(encoding="utf-8"))
        lease.update({
            "terminal_status": "blocked",
            "result_hash": None,
            "reason": "terminal result publication failed",
        })
        executor_mod.RepoExecutor._validate_lease_shape(lease)
        first.lease_path.write_bytes(executor_mod.canonical_json_bytes(lease))
        first.close()

        recovered = self.executor()
        original = recovered.lease_path.read_bytes()
        with (mock.patch.object(
                  recovered, "_preflight",
                  side_effect=AssertionError("cached result was redispatched")),
              self.assertRaisesRegex(
                  executor_mod.LeaseBusy, "fenced exact replay")):
            recovered.execute(copy.deepcopy(request))
        self.assertEqual(recovered.lease_path.read_bytes(), original)

        with mock.patch.object(
                recovered, "_preflight",
                side_effect=AssertionError("cached result was redispatched")):
            result = recovered.reconcile_pending_operation(
                recovery_authorizer=(
                    executor_mod.RepoExecutor.fence_recovery_lease))

        self.assertEqual(result, expected_result)
        terminal = json.loads(
            recovered.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(terminal["terminal_status"], "validated")
        self.assertEqual(
            terminal["result_hash"], executor_mod.canonical_hash(result))
        self.assertEqual(terminal["reason"], f"recovered-from:{lease['nonce']}")

    def test_durably_blocked_reaped_child_requires_exact_fence(self):
        executor = self.executor()
        executor.execute(self.preflight_request())
        lease = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        last_child = lease["child_history"].pop()
        lease.update({
            "terminal_status": "blocked",
            "result_hash": None,
            "reason": "guardian ACK publication uncertain",
            "child_state": "reaped",
            "child_generation": last_child["generation"],
            "child_kind": last_child["kind"],
            "child_argv_hash": last_child["argv_hash"],
            "child_identity": last_child["identity"],
            "child_result": last_child["result"],
        })
        executor_mod.RepoExecutor._validate_lease_shape(lease)

        with mock.patch.object(
                executor_mod.compat, "process_matches_identity",
                return_value=True), mock.patch.object(
                    executor_mod.compat, "fence_process_tree",
                    return_value=False) as fence:
            self.assertFalse(
                executor_mod.RepoExecutor.fence_recovery_lease(lease))

        identity = last_child["identity"]
        fence.assert_called_once_with(
            identity["pid"], start_token=identity["start_token"],
            group_id=identity["group_id"], graceful_timeout=1.0,
            force_timeout=5.0)

    @unittest.skipUnless(executor_mod.compat.IS_WINDOWS, "Windows Job E2E")
    def test_operation_child_breaks_away_from_outer_job_then_owns_job(self):
        request = self.preflight_request()
        spec_path = self.root / "outer-job-spec.json"
        request_path = self.root / "outer-job-request.json"
        spec_path.write_text(
            json.dumps(self.spec.hash_material()), encoding="utf-8")
        request_path.write_text(json.dumps(request), encoding="utf-8")
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from engine.repo_executor import RepoExecutor\n"
            "spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
            "request=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))\n"
            "print('READY', flush=True)\n"
            "sys.stdin.readline()\n"
            "print(json.dumps(RepoExecutor(spec).execute(request)), flush=True)\n"
        )
        executor_mod.compat.request_process_group_breakaway()
        helper = subprocess.Popen(
            [sys.executable, "-c", script, str(spec_path), str(request_path)],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            **executor_mod.compat.popen_group_kwargs(),
        )
        self.addCleanup(executor_mod.compat.close_process_group, helper)
        self.assertTrue(executor_mod.compat.attach_process_group(helper))
        self.assertEqual(helper.stdout.readline().strip(), "READY")
        helper.stdin.write("go\n")
        helper.stdin.flush()
        helper.stdin.close()
        helper.stdin = None
        stdout, stderr = helper.communicate(timeout=30)
        self.assertEqual(helper.returncode, 0, stderr)
        result = json.loads(stdout.strip().splitlines()[-1])
        self.assertEqual(result["status"], "validated")

    def test_recovery_helper_rejects_pid_only_and_pid_reuse_inference(self):
        executor = self.executor()
        executor.execute(self.preflight_request())
        lease = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        lease["state"] = "running"
        lease["terminal_status"] = None
        lease["result_hash"] = None
        child = lease["child_history"].pop()
        lease["child_state"] = "running"
        lease["child_kind"] = child["kind"]
        lease["child_argv_hash"] = child["argv_hash"]
        lease["child_identity"] = child["identity"]
        lease["child_result"] = None

        with mock.patch.object(
                executor_mod.compat, "process_matches_identity",
                side_effect=[True]):
            self.assertFalse(
                executor_mod.RepoExecutor.fence_recovery_lease(lease))

        # A missing POSIX root does not prove that a setsid descendant was not
        # reparented, even when the PID has since been reused.
        lease["child_identity"]["containment_kind"] = "process-tree"
        with mock.patch.object(
                executor_mod.compat, "process_matches_identity",
                side_effect=[False, False]) as matches, mock.patch.object(
                    executor_mod.compat, "fence_process_tree") as fence:
            self.assertFalse(
                executor_mod.RepoExecutor.fence_recovery_lease(lease))
            self.assertEqual(matches.call_count, 2)
            fence.assert_not_called()

        # Legacy Windows Jobs allowed payload breakaway.  Keep their schema
        # readable, but never treat same-boot root absence as a tree proof.
        lease["child_identity"]["containment_kind"] = "windows-job"
        with mock.patch.object(
                executor_mod.compat, "process_matches_identity",
                side_effect=[False]) as matches, mock.patch.object(
                    executor_mod.compat, "fence_process_tree") as fence:
            self.assertFalse(
                executor_mod.RepoExecutor.fence_recovery_lease(lease))
            self.assertEqual(matches.call_count, 1)
            fence.assert_not_called()

        # The strict no-breakaway Job contract makes root absence conclusive
        # after the exact old executor identity is gone and its Job closes.
        lease["child_identity"]["containment_kind"] = (
            "windows-job-no-breakaway-v2")
        with mock.patch.object(
                executor_mod.compat, "process_matches_identity",
                side_effect=[False, False]) as matches, mock.patch.object(
                    executor_mod.compat, "fence_process_tree") as fence:
            self.assertTrue(
                executor_mod.RepoExecutor.fence_recovery_lease(lease))
            self.assertEqual(matches.call_count, 2)
            fence.assert_not_called()

    def test_recovery_rejects_legacy_windows_job_in_reaped_or_idle_history(self):
        executor = self.executor()
        executor.execute(self.preflight_request())
        durable = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        durable["state"] = "running"
        durable["terminal_status"] = None
        durable["result_hash"] = None
        durable["child_history"][-1]["identity"]["containment_kind"] = (
            "windows-job")

        with mock.patch.object(
                executor_mod.compat, "process_matches_identity",
                return_value=False) as matches:
            self.assertFalse(
                executor_mod.RepoExecutor.fence_recovery_lease(durable))
            matches.assert_called_once()

        reaped = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        reaped["state"] = "running"
        reaped["terminal_status"] = None
        reaped["result_hash"] = None
        current = reaped["child_history"].pop()
        current["identity"]["containment_kind"] = "windows-job"
        reaped.update({
            "child_state": "reaped",
            "child_kind": current["kind"],
            "child_argv_hash": current["argv_hash"],
            "child_identity": current["identity"],
            "child_result": current["result"],
        })
        with mock.patch.object(
                executor_mod.compat, "process_matches_identity",
                return_value=False) as matches:
            self.assertFalse(
                executor_mod.RepoExecutor.fence_recovery_lease(reaped))
            matches.assert_called_once()

    def test_corrupt_child_or_executor_identity_fails_closed(self):
        executor = self.executor()
        executor.execute(self.preflight_request())
        original = executor.lease_path.read_bytes()
        for field, mutate in (
            ("executor-token", lambda value: value.update(
                {"executor_creation_token": ""})),
            ("child-token", lambda value: value["child_history"][-1]["identity"].update(
                {"start_token": ""})),
            ("durable-request", lambda value: value["request"]["expected"].update(
                {"head_sha": "0" * 40})),
            ("child-state", lambda value: value.update(
                {"child_state": "running"})),
        ):
            with self.subTest(field=field):
                payload = json.loads(original.decode("utf-8"))
                mutate(payload)
                executor.lease_path.write_bytes(
                    executor_mod.canonical_json_bytes(payload))
                try:
                    with self.assertRaises(executor_mod.RepoExecutorError):
                        executor.audit_recovery_state()
                finally:
                    executor.lease_path.write_bytes(original)

    def preflight_request(self, **updates):
        request = {
            "operation": "PREFLIGHT",
            "operation_id": self.operation_id(),
            "authority": {"pending_launch_hash": self.spec.pending_launch_hash},
            "expected": {"head_ref": self.primary_ref, "head_sha": self.start},
        }
        request.update(updates)
        return request

    def initialize(self, executor):
        return executor.execute({
            "operation": "INITIALIZE_RUN_REFS",
            "operation_id": self.operation_id(),
            "authority": {"manifest_hash": self.spec.manifest_hash},
            "expected": {
                "integration_start_sha": self.start,
                "sync_ref_absent": True,
            },
        })

    def create(self, executor, task: int, *, base=None):
        return executor.execute({
            "operation": "CREATE_WORKTREE",
            "operation_id": self.operation_id(),
            "task": task,
            "authority": {
                "manifest_hash": self.spec.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[task],
            },
            "expected": {
                "base_sha": base or self.start,
                "task_ref_absent": True,
                "worktree_absent": True,
            },
        })

    def commit_task(self, executor, task: int, text: str) -> str:
        worktree = executor.worktree_path(task)
        (worktree / f"task-{task}.txt").write_text(text, encoding="utf-8")
        _git(worktree, "add", f"task-{task}.txt")
        _git(worktree, "commit", "-qm", f"task-{task}")
        return _git(worktree, "rev-parse", "HEAD").stdout.strip()

    def claim_gate(self, executor, task: int, validated_sha: str,
                   *, request_id=None, validated_round=7):
        request_id = request_id or self.operation_id()
        env = {
            "RUN_ID": RUN_ID,
            "TASK": str(task),
            "REQUEST_ID": request_id,
            "VALIDATED_SHA": validated_sha,
            "VALIDATED_ROUND": str(validated_round),
            "RUN_CONFIG_HASH": self.artifacts.run_config_hash,
            "LAUNCH_SPEC_HASH": self.artifacts.assignment_hashes[task],
            "MANIFEST_HASH": self.artifacts.manifest_hash,
        }
        payload = parallel_gate.request_from_environment(
            env, deadline_at="2030-01-01T00:00:00+00:00")
        spool = parallel_spool.DurableSpool(
            self.artifacts.run_dir / "requests",
            responses_root=self.artifacts.run_dir / "responses",
        )
        spool.publish_request(request_id, payload)
        claimed = spool.claim_request(request_id)
        self.assertTrue(claimed.transitioned)
        self.assertEqual(claimed.state, "claimed")
        return payload

    def gate_request(self, task: int, payload: dict, *, before: str):
        return {
            "operation": "GATE_MERGE",
            "operation_id": executor_mod.gate_operation_id(
                RUN_ID, payload["request_id"]),
            "task": task,
            "authority": {
                "manifest_hash": self.spec.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[task],
                "request_hash": parallel_state.canonical_json_hash(payload),
            },
            "expected": {
                "request_id": payload["request_id"],
                "validated_sha": payload["validated_sha"],
                "validated_round": payload["validated_round"],
                "integration_before": before,
                "sync_before": before,
            },
        }

    def prepare_gate(self, executor, *, task=1):
        self.initialize(executor)
        self.create(executor, task)
        validated = self.commit_task(executor, task, f"task {task}\n")
        payload = self.claim_gate(executor, task, validated)
        return validated, self.gate_request(task, payload, before=self.start)

    def test_closed_enum_exact_request_and_authority_fail_before_side_effect(self):
        self.assertEqual({item.value for item in executor_mod.Operation}, {
            "PREFLIGHT", "INITIALIZE_RUN_REFS", "CREATE_WORKTREE",
            "GATE_MERGE", "REMOVE_WORKTREE", "SHUTDOWN",
        })
        executor = self.executor()
        request = self.preflight_request(repo=str(self.repo))
        with self.assertRaises(executor_mod.AuthorityError):
            executor.execute(request)
        self.assertFalse(executor.sidecar_root.exists())

        wrong = self.preflight_request()
        wrong["authority"]["pending_launch_hash"] = "f" * 64
        with self.assertRaises(executor_mod.AuthorityError):
            executor.execute(wrong)
        self.assertFalse(executor.sidecar_root.exists())

        with self.assertRaisesRegex(executor_mod.AuthorityError, "only accepts"):
            executor.reconcile_claimed_gate(self.preflight_request())
        self.assertFalse(executor.sidecar_root.exists())

        spec_payload = self.spec.hash_material()
        assignment = spec_payload["assignments"].pop("1")
        spec_payload["assignments"] = {"not-an-order": assignment}
        with self.assertRaises(executor_mod.AuthorityError):
            executor_mod.ImmutableRepoSpec.from_dict(spec_payload)
        with self.assertRaises(TypeError):
            self.spec.assignments[3] = self.spec.assignments[1]

    def test_global_lock_path_rejects_symlink_without_touching_target(self):
        executor = self.executor()
        victim = self.root / "lock-victim.txt"
        victim.write_text("unchanged\n", encoding="utf-8")
        try:
            executor._global_lock_path.symlink_to(victim)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        with self.assertRaises(executor_mod.AuthorityError):
            executor.execute(self.preflight_request())

        self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged\n")

    def test_preflight_init_and_create_attached_canonical_worktree(self):
        executor = self.executor()
        preflight = executor.execute(self.preflight_request())
        self.assertEqual(preflight["status"], "validated")
        initialized = self.initialize(executor)
        self.assertEqual(initialized["status"], "initialized")
        self.assertEqual(
            _git(self.repo, "show-ref", "--verify", "--hash", executor.sync_ref).stdout.strip(),
            self.start,
        )
        audit = executor.audit_recovery_state()
        self.assertEqual(audit["receipt_count"], 0)
        self.assertEqual(audit["receipt_tip"], self.start)
        self.assertEqual(audit["primary_sha"], self.start)
        self.assertEqual(audit["sync_sha"], self.start)

        created = self.create(executor, 1)
        worktree = Path(created["worker_repo"])
        self.assertEqual(worktree, executor.worktree_path(1))
        self.assertFalse(executor._is_contained(worktree, self.repo))
        self.assertEqual(
            _git(worktree, "symbolic-ref", "HEAD").stdout.strip(),
            executor.task_ref(1),
        )
        self.assertEqual(created["head"], self.start)
        self.assertEqual(len(created["observation_token"]), 64)
        common = Path(_git(self.repo, "rev-parse", "--git-common-dir").stdout.strip())
        if not common.is_absolute():
            common = (self.repo / common).resolve()
        self.assertEqual(executor.sidecar_root.parent, common.resolve())
        lease = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["state"], "terminal")

    def test_gate_merge_writes_canonical_receipt_before_success(self):
        executor = self.executor()
        validated, request = self.prepare_gate(executor)

        result = executor.execute(request)

        self.assertEqual(result["status"], "merged")
        self.assertEqual(result["validated_sha"], validated)
        self.assertEqual(result["validated_round"], 7)
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD").stdout.strip(), validated)
        self.assertEqual(
            _git(self.repo, "show-ref", "--verify", "--hash", executor.sync_ref).stdout.strip(),
            validated,
        )
        _artifacts, chain = parallel_state.load_receipt_chain(
            self.artifacts.run_dir, workspace_root=self.workspace_root)
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0], {
            "schema_version": 1,
            "run_id": RUN_ID,
            "manifest_hash": self.artifacts.manifest_hash,
            "assignment_hash": self.artifacts.assignment_hashes[1],
            "task": 1,
            "request_id": request["expected"]["request_id"],
            "sequence": 1,
            "previous_receipt_hash": None,
            "integration_before": self.start,
            "validated_sha": validated,
            "validated_round": 7,
        })
        intent = json.loads(
            (executor.intents_dir
             / f"gate-{request['expected']['request_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(intent["state"], "committed")
        audit = executor.audit_recovery_state()
        self.assertEqual(audit["receipt_count"], 1)
        self.assertEqual(audit["receipt_tip"], validated)
        self.assertEqual(audit["primary_sha"], validated)
        self.assertEqual(audit["sync_sha"], validated)

        _git(self.repo, "update-ref", executor.sync_ref, self.start, validated)
        with self.assertRaisesRegex(executor_mod.InvariantError, "receipt tip"):
            executor.audit_recovery_state()

    def test_recovery_audit_requires_complete_gate_evidence_graph(self):
        executor = self.executor()
        validated, request = self.prepare_gate(executor)
        result = executor.execute(request)
        request_id = request["expected"]["request_id"]
        self.assertEqual(
            executor.audit_recovery_state()["receipt_tip"], validated)

        evidence_paths = {
            "intent": executor._intent_path("gate", request_id),
            "receipt": executor._receipt_path("gate", request_id),
            "operation-result": executor._operation_result_path(
                request["operation_id"]),
        }
        for label, path in evidence_paths.items():
            with self.subTest(missing=label):
                raw = path.read_bytes()
                path.unlink()
                try:
                    with self.assertRaises(executor_mod.RepoExecutorError):
                        executor.audit_recovery_state()
                finally:
                    path.write_bytes(raw)
                self.assertEqual(
                    executor.audit_recovery_state()["receipt_tip"], validated)

        claimed = executor.claimed_request_path(request_id)
        pending = (self.artifacts.run_dir / "requests" / "pending"
                   / f"{request_id}.json")
        claimed.replace(pending)
        try:
            with self.assertRaisesRegex(
                    executor_mod.InvariantError, "not claimed"):
                executor.audit_recovery_state()
        finally:
            pending.replace(claimed)

        raw_claimed = claimed.read_bytes()
        tampered = json.loads(raw_claimed.decode("utf-8"))
        tampered["deadline_at"] = "2031-01-01T00:00:00+00:00"
        claimed.write_bytes(executor_mod.canonical_json_bytes(tampered))
        try:
            with self.assertRaises(executor_mod.RepoExecutorError):
                executor.audit_recovery_state()
        finally:
            claimed.write_bytes(raw_claimed)

        orphan_id = "f" * 32 if request_id != "f" * 32 else "e" * 32
        for label, source, directory in (
            ("intent", evidence_paths["intent"], executor.intents_dir),
            ("receipt", evidence_paths["receipt"], executor.receipts_dir),
        ):
            with self.subTest(orphan=label):
                orphan = directory / f"gate-{orphan_id}.json"
                shutil.copyfile(source, orphan)
                try:
                    with self.assertRaisesRegex(
                            executor_mod.InvariantError, "orphan"):
                        executor.audit_recovery_state()
                finally:
                    orphan.unlink(missing_ok=True)

        audit = executor.audit_recovery_state()
        self.assertEqual(audit["receipt_tip"], validated)
        self.assertEqual(result["receipt_hash"], json.loads(
            evidence_paths["receipt"].read_text(encoding="utf-8"))["receipt_hash"])

    def test_recovery_audit_rejects_nondeterministic_gate_operation_id(self):
        executor = self.executor()
        validated, request = self.prepare_gate(executor)
        expected = executor_mod.gate_operation_id(
            RUN_ID, request["expected"]["request_id"])
        request["operation_id"] = self.operation_id()
        self.assertNotEqual(request["operation_id"], expected)
        executor.execute(request)

        with self.assertRaisesRegex(
                executor_mod.InvariantError, "operation_id"):
            executor.audit_recovery_state()

    def test_initialize_recovery_after_receipt_commits_intent(self):
        armed = {"initialize.after_receipt"}

        def inject(point):
            if point in armed:
                armed.remove(point)
                raise InjectedCrash(point)

        executor = self.executor(fault_injector=inject)
        request = {
            "operation": "INITIALIZE_RUN_REFS",
            "operation_id": self.operation_id(),
            "authority": {"manifest_hash": self.spec.manifest_hash},
            "expected": {
                "integration_start_sha": self.start,
                "sync_ref_absent": True,
            },
        }
        with self.assertRaises(InjectedCrash):
            executor.execute(request)

        result = executor.execute(copy.deepcopy(request))

        self.assertEqual(result["status"], "already-initialized")
        intent = json.loads(
            executor._intent_path("init", request["operation_id"])
            .read_text(encoding="utf-8"))
        self.assertEqual(intent["state"], "committed")

    def _assert_create_recovery(self, point: str):
        armed = set()

        def inject(point):
            if point in armed:
                armed.remove(point)
                raise InjectedCrash(point)

        executor = self.executor(fault_injector=inject)
        self.initialize(executor)
        request = {
            "operation": "CREATE_WORKTREE",
            "operation_id": self.operation_id(),
            "task": 1,
            "authority": {
                "manifest_hash": self.spec.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[1],
            },
            "expected": {
                "base_sha": self.start,
                "task_ref_absent": True,
                "worktree_absent": True,
            },
        }
        armed.add(point)
        with self.assertRaises(InjectedCrash):
            executor.execute(request)

        result = executor.execute(copy.deepcopy(request))

        self.assertIn(result["status"], {"created", "already-created"})
        observation = executor.observe_worktree(1)
        self.assertTrue(observation["exists"])
        self.assertTrue(observation["registered"])
        self.assertEqual(observation["head_ref"], executor.task_ref(1))
        self.assertEqual(observation["head"], self.start)
        intent = json.loads(
            executor._intent_path("create", request["operation_id"])
            .read_text(encoding="utf-8"))
        self.assertEqual(intent["state"], "committed")

    def test_create_recovery_after_prepared(self):
        self._assert_create_recovery("create.after_prepared")

    def test_create_recovery_after_ref_before_worktree(self):
        self._assert_create_recovery("create.after_ref")

    def test_create_recovery_after_worktree_before_receipt(self):
        self._assert_create_recovery("create.after_worktree")

    def test_create_recovery_after_receipt_commits_intent(self):
        self._assert_create_recovery("create.after_receipt")

    def test_stale_gate_does_not_write_receipt_or_move_primary(self):
        executor = self.executor()
        self.initialize(executor)
        self.create(executor, 1)
        self.create(executor, 2)
        first_sha = self.commit_task(executor, 1, "first\n")
        second_sha = self.commit_task(executor, 2, "second\n")
        first_payload = self.claim_gate(executor, 1, first_sha)
        first = executor.execute(self.gate_request(1, first_payload, before=self.start))
        self.assertEqual(first["status"], "merged")
        second_payload = self.claim_gate(executor, 2, second_sha)
        second_request = self.gate_request(2, second_payload, before=first_sha)

        stale = executor.execute(second_request)

        self.assertEqual(stale["status"], "stale-integration")
        self.assertIsNone(stale["receipt_hash"])
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD").stdout.strip(), first_sha)
        self.assertFalse((self.artifacts.run_dir / "receipts" / "task-2.json").exists())
        self.assertFalse(
            executor._receipt_path("gate", second_request["expected"]["request_id"]).exists())

    def _assert_gate_recovery(self, point: str):
        armed = {point}

        def inject(actual):
            if actual in armed:
                armed.remove(actual)
                raise InjectedCrash(actual)

        executor = self.executor(fault_injector=inject)
        validated, request = self.prepare_gate(executor)
        with self.assertRaises(InjectedCrash):
            executor.execute(request)
        lease = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["state"], "running")

        result = executor.execute(copy.deepcopy(request))

        self.assertIn(result["status"], {"merged", "already-merged"})
        self.assertEqual(result["validated_sha"], validated)
        self.assertTrue((self.artifacts.run_dir / "receipts" / "task-1.json").is_file())
        lease = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["state"], "terminal")

    def test_gate_recovery_before_merge(self):
        self._assert_gate_recovery("gate.after_prepared")

    def test_gate_recovery_after_merge_before_sync(self):
        self._assert_gate_recovery("gate.after_merge")

    def test_gate_recovery_after_sync_before_receipt(self):
        self._assert_gate_recovery("gate.after_sync")

    def test_gate_recovery_after_canonical_receipt_before_common_receipt(self):
        self._assert_gate_recovery("gate.after_run_receipt")

    def test_gate_recovery_after_common_receipt_commits_intent(self):
        self._assert_gate_recovery("gate.after_receipt")

    def test_nonterminal_lease_requires_explicit_fenced_recovery(self):
        armed = {"gate.after_sync"}

        def inject(point):
            if point in armed:
                armed.remove(point)
                raise InjectedCrash(point)

        first = self.executor(fault_injector=inject)
        validated, request = self.prepare_gate(first)
        with self.assertRaises(InjectedCrash):
            first.execute(request)
        old_lease = json.loads(first.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(old_lease["state"], "running")
        self.assertEqual(old_lease["immutable_spec_hash"], first.authority_hash)
        self.assertEqual(first.authority_hash, self.spec.authority_hash)
        first.close()

        recovered = self.executor()
        with self.assertRaises(executor_mod.InvariantError):
            recovered.audit_recovery_state()
        with self.assertRaisesRegex(executor_mod.LeaseBusy, "fenced recovery"):
            recovered.reconcile_claimed_gate(copy.deepcopy(request))

        authorized_leases = []

        def authorize(lease):
            authorized_leases.append(lease)
            return True

        result = recovered.reconcile_claimed_gate(
            copy.deepcopy(request), recovery_authorizer=authorize)

        self.assertEqual(result["status"], "merged")
        self.assertEqual(result["validated_sha"], validated)
        self.assertEqual(authorized_leases[0]["nonce"], old_lease["nonce"])
        terminal = json.loads(recovered.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(terminal["state"], "terminal")
        self.assertEqual(terminal["executor_session"], recovered._session)
        self.assertEqual(terminal["reason"], f"recovered-from:{old_lease['nonce']}")
        audit = recovered.audit_recovery_state()
        self.assertEqual(audit["receipt_tip"], validated)

    def test_gate_recovery_unknown_head_sync_combination_blocks(self):
        armed = {"gate.after_prepared"}

        def inject(point):
            if point in armed:
                armed.remove(point)
                raise InjectedCrash(point)

        executor = self.executor(fault_injector=inject)
        validated, request = self.prepare_gate(executor)
        with self.assertRaises(InjectedCrash):
            executor.execute(request)
        _git(self.repo, "update-ref", executor.sync_ref, validated, self.start)

        with self.assertRaisesRegex(executor_mod.InvariantError, "safe matrix"):
            executor.execute(request)
        with self.assertRaisesRegex(executor_mod.LeaseBusy, "durably blocked"):
            executor.execute(copy.deepcopy(request))
        self.assertFalse((self.artifacts.run_dir / "receipts" / "task-1.json").exists())

    def _assert_remove_recovery(self, point: str):
        armed = set()

        def inject(actual):
            if actual in armed:
                armed.remove(actual)
                raise InjectedCrash(actual)

        executor = self.executor(fault_injector=inject)
        self.initialize(executor)
        created = self.create(executor, 1)
        remove = {
            "operation": "REMOVE_WORKTREE",
            "operation_id": self.operation_id(),
            "task": 1,
            "authority": {
                "manifest_hash": self.spec.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[1],
            },
            "expected": {
                "terminal_outcome": "cancelled",
                "observation_token": created["observation_token"],
            },
        }
        armed.add(point)
        with self.assertRaises(InjectedCrash):
            executor.execute(remove)
        lease = json.loads(executor.lease_path.read_text(encoding="utf-8"))
        self.assertEqual(lease["state"], "running")

        result = executor.execute(copy.deepcopy(remove))

        self.assertIn(result["status"], {"removed", "already-removed"})
        self.assertFalse(executor.worktree_path(1).exists())
        self.assertIsNone(executor._ref_tip(executor.task_ref(1)))
        intent = json.loads(
            executor._intent_path("remove", remove["operation_id"])
            .read_text(encoding="utf-8"))
        self.assertEqual(intent["state"], "committed")
        self.assertTrue(
            executor._receipt_path("remove", remove["operation_id"]).is_file())

    def test_remove_recovery_after_prepared(self):
        self._assert_remove_recovery("remove.after_prepared")

    def test_remove_recovery_after_worktree_before_ref(self):
        self._assert_remove_recovery("remove.after_worktree")

    def test_remove_recovery_after_ref_before_receipt(self):
        self._assert_remove_recovery("remove.after_ref")

    def test_remove_recovery_after_receipt_commits_intent(self):
        self._assert_remove_recovery("remove.after_receipt")

    def test_remove_requires_fresh_clean_unlocked_observation_token(self):
        executor = self.executor()
        validated, gate_request = self.prepare_gate(executor)
        gate = executor.execute(gate_request)
        worktree = executor.worktree_path(1)
        stale_token = gate["observation_token"]
        (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        bad_remove = {
            "operation": "REMOVE_WORKTREE",
            "operation_id": self.operation_id(),
            "task": 1,
            "authority": {
                "manifest_hash": self.spec.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[1],
            },
            "expected": {
                "terminal_outcome": "integrated",
                "observation_token": stale_token,
            },
        }
        with self.assertRaisesRegex(executor_mod.InvariantError, "TOCTOU"):
            executor.execute(bad_remove)
        (worktree / "dirty.txt").unlink()

        _git(self.repo, "worktree", "lock", str(worktree), "--reason", "live worker")
        locked = executor.observe_worktree(1)
        locked_request = copy.deepcopy(bad_remove)
        locked_request["operation_id"] = self.operation_id()
        locked_request["expected"]["observation_token"] = locked["observation_token"]
        with self.assertRaisesRegex(executor_mod.InvariantError, "locked"):
            executor.supersede_blocked_operation(
                locked_request, recovery_authorizer=lambda _lease: True)
        _git(self.repo, "worktree", "unlock", str(worktree))

        clean = executor.observe_worktree(1)
        remove = copy.deepcopy(bad_remove)
        remove["operation_id"] = self.operation_id()
        remove["expected"]["observation_token"] = clean["observation_token"]
        result = executor.supersede_blocked_operation(
            remove, recovery_authorizer=lambda _lease: True)

        self.assertEqual(result["status"], "removed")
        self.assertEqual(result["worker_repo"], str(worktree))
        self.assertFalse(worktree.exists())
        self.assertIsNone(executor._ref_tip(executor.task_ref(1)))
        self.assertEqual(validated, gate["validated_sha"])
        audit = executor.audit_recovery_state()
        self.assertEqual(audit["receipt_tip"], validated)
        self.assertEqual(audit["primary_sha"], validated)
        self.assertEqual(audit["sync_sha"], validated)

    def test_remove_journals_fresh_fully_absent_observation(self):
        executor = self.executor()
        self.initialize(executor)
        self.create(executor, 1)
        worktree = executor.worktree_path(1)
        task_ref = executor.task_ref(1)
        _git(self.repo, "worktree", "remove", "--force", str(worktree))
        _git(self.repo, "update-ref", "-d", task_ref)
        observation = executor.observe_worktree(1)
        self.assertFalse(observation["exists"])
        self.assertFalse(observation["registered"])
        self.assertIsNone(observation["task_ref_tip"])

        operation_id = self.operation_id()
        result = executor.execute({
            "operation": "REMOVE_WORKTREE",
            "operation_id": operation_id,
            "task": 1,
            "authority": {
                "manifest_hash": self.spec.manifest_hash,
                "assignment_hash": self.artifacts.assignment_hashes[1],
            },
            "expected": {
                "terminal_outcome": "cancelled",
                "observation_token": observation["observation_token"],
            },
        })

        self.assertEqual(result["status"], "already-removed")
        intent = executor_mod.RepoExecutor._read_json(
            executor._intent_path("remove", operation_id),
            "test absent remove intent")
        self.assertIsNone(intent["observed_head"])
        self.assertEqual(intent["state"], "committed")

    def test_initialize_rejects_unknown_preexisting_safe_ref(self):
        executor = self.executor()
        tree = _git(self.repo, "write-tree").stdout.strip()
        other = _git(self.repo, "commit-tree", tree, "-p", self.start, "-m", "other").stdout.strip()
        _git(self.repo, "update-ref", executor.sync_ref, other)

        with self.assertRaisesRegex(executor_mod.InvariantError, "未知 actor"):
            self.initialize(executor)
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD").stdout.strip(), self.start)

    def test_shutdown_requires_authority_and_releases_global_lock(self):
        executor = self.executor()
        self.initialize(executor)
        result = executor.execute({
            "operation": "SHUTDOWN",
            "operation_id": self.operation_id(),
            "authority": {"supervisor_session": SESSION, "generation": 1},
            "expected": {"idle": True},
        })
        self.assertEqual(result["status"], "shutdown")
        self.assertIsNone(executor._global_lock_file)


if __name__ == "__main__":
    unittest.main()
