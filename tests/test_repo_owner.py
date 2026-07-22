"""Focused and real-Git tests for the common-dir non-guardian owner fence."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from engine import platform_compat as compat
from engine import repo_owner


SESSION_A = "a" * 32
SESSION_B = "b" * 32
SESSION_C = "c" * 32
OWNER_A = {"pid": 101, "creation_token": "owner-a-created"}
OWNER_B = {"pid": 202, "creation_token": "owner-b-created"}
CHILD_A = {
    "pid": 303,
    "creation_token": "child-a-created",
    "containment_kind": "job" if compat.IS_WINDOWS else "process-group",
    "containment_id": "containment-303",
}


def _git(repo: Path, *args: str, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check)


@unittest.skipUnless(shutil.which("git"), "git is required")
class RepoOwnerFenceGitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "primary"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Repo Owner Test")
        _git(self.repo, "config", "user.email", "repo-owner@example.invalid")
        (self.repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-qm", "initial")
        self.workspace = self.root / "workspaces" / "base"
        self.workspace.mkdir(parents=True)
        self.state_path = self.workspace / "state.json"
        self.common_dir = self._common_dir(self.repo)
        self.marker_path = self.common_dir / repo_owner.MARKER_NAME

    @staticmethod
    def _common_dir(repo: Path) -> Path:
        raw = Path(_git(repo, "rev-parse", "--git-common-dir").stdout.strip())
        return (raw if raw.is_absolute() else repo / raw).resolve(strict=True)

    def claim(self, *, session=SESSION_A, owner_kind="loop", identity=OWNER_A):
        fence = repo_owner.RepoOwnerFence.claim(
            self.repo,
            owner_kind=owner_kind,
            workspace=self.workspace,
            state_path=self.state_path,
            session=session,
            owner_identity=identity,
            boot_identity="test-boot-a",
        )
        self.addCleanup(fence.close)
        return fence

    def test_atomic_json_retries_transient_windows_sharing_violation(self):
        target = self.common_dir / "atomic-retry.json"
        real_replace = os.replace
        attempts = []

        def flaky_replace(source, destination):
            attempts.append((source, destination))
            if len(attempts) < 3:
                raise PermissionError(13, "transient sharing violation")
            return real_replace(source, destination)

        with (mock.patch.object(repo_owner.compat, "IS_WINDOWS", True),
              mock.patch.object(
                  repo_owner.os, "replace", side_effect=flaky_replace)):
            repo_owner._atomic_json(target, {"ready": True})

        self.assertEqual(len(attempts), 3)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"ready": True})

    def test_atomic_json_accepts_exact_commit_reported_as_replace_error(self):
        target = self.common_dir / "atomic-committed-error.json"
        real_replace = os.replace

        def committed_error(source, destination):
            real_replace(source, destination)
            raise OSError(5, "reported after commit")

        with (mock.patch.object(repo_owner.compat, "IS_WINDOWS", True),
              mock.patch.object(
                  repo_owner.os, "replace", side_effect=committed_error)):
            repo_owner._atomic_json(target, {"committed": True})

        self.assertEqual(json.loads(target.read_text(encoding="utf-8")),
                         {"committed": True})

    def recover(self, authorizer, *, expected_session=SESSION_A,
                expected_generation=1, recovery_session=SESSION_B):
        fence = repo_owner.RepoOwnerFence.recover(
            self.repo,
            expected_owner_kind="loop",
            expected_workspace=self.workspace,
            expected_state_path=self.state_path,
            expected_session=expected_session,
            expected_generation=expected_generation,
            recovery_authorizer=authorizer,
            recovery_session=recovery_session,
            recovery_identity=OWNER_B,
            boot_identity="test-boot-b",
        )
        self.addCleanup(fence.close)
        return fence

    def executor_lease(self, *, state="running") -> dict:
        request = {
            "operation": "PREFLIGHT",
            "operation_id": "1" * 32,
            "authority": {"pending_launch_hash": "7" * 64},
            "expected": {},
        }
        request_hash = hashlib.sha256(json.dumps(
            request, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        terminal_result = {"status": "completed"}
        terminal_result_hash = hashlib.sha256(json.dumps(
            terminal_result, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        return {
            "schema_version": 2,
            "state": state,
            "operation": "PREFLIGHT",
            "operation_id": "1" * 32,
            "request_hash": request_hash,
            "immutable_spec_hash": "3" * 64,
            "nonce": "4" * 32,
            "generation": 1,
            "executor_session": "5" * 32,
            "pid": os.getpid(),
            "executor_creation_token": "executor-created",
            "expected": {},
            "request": request,
            "child_generation": 0,
            "child_state": "idle",
            "child_kind": None,
            "child_argv_hash": None,
            "child_identity": None,
            "child_result": None,
            "child_history": [],
            "updated_at": "2026-07-22T00:00:00Z",
            "terminal_status": "completed" if state == "terminal" else None,
            "result_hash": terminal_result_hash if state == "terminal" else None,
            "reason": None,
        }

    def write_executor_lease(self, lease: dict) -> Path:
        sidecar = self.common_dir / repo_owner.EXECUTOR_SIDECAR_NAME
        sidecar.mkdir(exist_ok=True)
        path = sidecar / repo_owner.EXECUTOR_LEASE_NAME
        path.write_text(
            json.dumps(lease, sort_keys=True, separators=(",", ":")),
            encoding="utf-8")
        if lease.get("state") == "terminal" and lease.get("result_hash"):
            result = {"status": lease["terminal_status"]}
            artifact = {
                "schema_version": 1,
                "operation_id": lease["operation_id"],
                "request_hash": lease["request_hash"],
                "result": result,
                "result_hash": hashlib.sha256(json.dumps(
                    result, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                ).encode("utf-8")).hexdigest(),
            }
            results = sidecar / "operation-results"
            results.mkdir(exist_ok=True)
            (results / f"{lease['operation_id']}.json").write_text(
                json.dumps(artifact, sort_keys=True, separators=(",", ":")),
                encoding="utf-8")
        return path

    def test_full_child_lifecycle_is_durable_and_argv_is_hash_only(self):
        fence = self.claim()
        marker = fence.marker
        self.assertEqual(marker["state"], "active")
        self.assertEqual(marker["child_state"], "idle")
        self.assertEqual(marker["generation"], 1)
        self.assertEqual(Path(marker["canonical_repo"]), self.repo)
        self.assertEqual(Path(marker["common_dir"]), self.common_dir)

        child_generation = fence.begin_child(
            "agent", ["agent", "--api-token", "secret-must-not-persist"])
        launching = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(launching["child_state"], "launching")
        self.assertEqual(launching["child_generation"], 1)
        self.assertEqual(len(launching["argv_hash"]), 64)
        self.assertNotIn("secret-must-not-persist",
                         self.marker_path.read_text(encoding="utf-8"))

        running = fence.publish_child_running(child_generation, CHILD_A)
        self.assertEqual(running["child_state"], "child_running")
        self.assertEqual(running["child_identity"], CHILD_A)
        with self.assertRaises(repo_owner.OwnerBusy):
            fence.terminalize("must-not-terminalize")

        reaped = fence.record_child_result(child_generation, 7)
        self.assertEqual(reaped["child_state"], "child_reaped")
        self.assertEqual(reaped["child_result"]["returncode"], 7)
        terminal = fence.terminalize("bounded-operation-finished")
        self.assertEqual(terminal["state"], "terminal")
        self.assertEqual(terminal["child_state"], "child_reaped")
        self.assertEqual(
            repo_owner.RepoOwnerFence.inspect(self.repo), terminal)

    def test_transition_adopts_exact_intended_commit_after_interrupt(self):
        fence = self.claim()
        generation = fence.begin_child("git", ["git", "status"])
        fence.publish_child_running(generation, CHILD_A)
        fence.record_child_result(generation, 0)
        real_atomic_json = repo_owner._atomic_json

        def commit_then_interrupt(path, payload):
            real_atomic_json(path, payload)
            raise KeyboardInterrupt

        with mock.patch.object(
                repo_owner, "_atomic_json", side_effect=commit_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                fence.checkpoint_child(generation)

        self.assertEqual(fence.marker["child_state"], "idle")
        self.assertEqual(
            repo_owner.RepoOwnerFence.inspect(self.repo)["child_state"],
            "idle")
        fence.terminalize("interrupt-commit-recovered")

    @unittest.skipUnless(compat.IS_WINDOWS, "CTRL+BREAK fd race is Windows-only")
    def test_transition_defers_break_during_marker_open_without_leaking_fd(self):
        fence = self.claim()
        generation = fence.begin_child("git", ["git", "status"])
        fence.publish_child_running(generation, CHILD_A)
        fence.record_child_result(generation, 0)
        real_open = repo_owner.os.open
        interrupted = []

        def raise_keyboard_interrupt(*_args):
            raise KeyboardInterrupt

        def open_then_break(path, flags, mode=0o600):
            should_interrupt = (
                not interrupted
                and Path(path) == self.marker_path
                and not flags & (os.O_WRONLY | os.O_RDWR)
            )
            fd = real_open(path, flags, mode)
            if should_interrupt:
                interrupted.append(fd)
                def close_if_still_open():
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                self.addCleanup(close_if_still_open)
                # This is the exact dangerous window: the CRT descriptor exists,
                # but _open_regular has not received it and therefore cannot put
                # it under its own finally block yet.
                compat._dispatch_windows_break(signal.SIGBREAK, None)
            return fd

        with mock.patch.object(
                compat, "_WINDOWS_BREAK_HANDLER", raise_keyboard_interrupt), \
                mock.patch.object(repo_owner.os, "open", side_effect=open_then_break):
            with self.assertRaises(KeyboardInterrupt):
                fence.checkpoint_child(generation)

        self.assertEqual(len(interrupted), 1)
        with self.assertRaises(OSError):
            os.fstat(interrupted[0])
        self.assertEqual(fence.marker["child_state"], "idle")
        self.assertEqual(
            repo_owner.RepoOwnerFence.inspect(self.repo)["child_state"], "idle")
        terminal = fence.terminalize("break-after-safe-marker-cas")
        self.assertEqual(terminal["state"], "terminal")

    @unittest.skipUnless(
        compat.IS_WINDOWS or sys.platform.startswith("linux"),
        "controlled owner guardian requires Windows or Linux",
    )
    def test_retained_child_handle_can_quiesce_interrupted_caller(self):
        fence = self.claim()
        child = fence.spawn_child(
            "git",
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        marker = fence.quiesce_active_child()

        self.assertEqual(marker["child_state"], "child_reaped")
        self.assertIsNotNone(child.poll())
        terminal = fence.terminalize("interrupted-caller-quiesced")
        self.assertEqual(terminal["state"], "terminal")

    def test_default_owner_and_boot_identity_are_live_process_bound(self):
        fence = repo_owner.RepoOwnerFence.claim(
            self.repo,
            owner_kind=repo_owner.OwnerKind.LOOP,
            workspace=self.workspace,
            state_path=self.state_path,
        )
        self.addCleanup(fence.close)
        marker = fence.marker
        self.assertEqual(marker["owner_identity"]["pid"], os.getpid())
        self.assertEqual(
            marker["owner_identity"]["creation_token"],
            repo_owner.process_creation_token(os.getpid()),
        )
        self.assertEqual(marker["host_boot_identity"],
                         repo_owner.host_boot_identity())
        fence.terminalize("completed")

    def test_controlled_spawn_preserves_pipe_api_and_records_after_job_empty(self):
        fence = self.claim()
        child = fence.spawn_child(
            "tool",
            [sys.executable, "-c",
             "import sys; data=sys.stdin.buffer.read(); "
             "sys.stdout.buffer.write(data.upper())"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = child.communicate(input=b"barrier-ok", timeout=10)
        self.assertEqual(stdout, b"BARRIER-OK")
        self.assertEqual(stderr, b"")
        self.assertEqual(child.returncode, 0)
        marker = child.record_result()
        self.assertEqual(marker["child_state"], "child_reaped")
        self.assertEqual(marker["child_result"]["returncode"], 0)
        fence.terminalize("completed")

    def test_controlled_spawn_blocks_payload_until_identity_and_fences_grandchild(self):
        fence = self.claim()
        pid_path = self.root / "grandchild.pid"
        grandchild_code = (
            "import os,sys,time\n"
            "p=sys.argv[1]\n"
            "with open(p,'w',encoding='utf-8') as f:\n"
            " f.write(str(os.getpid())); f.flush()\n"
            "while True:\n"
            " with open(p+'.tick','a',encoding='utf-8') as f:\n"
            "  f.write('x'); f.flush()\n"
            " time.sleep(0.05)\n"
        )
        leader_code = (
            "import subprocess,sys\n"
            "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]], "
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL)\n"
        )
        argv = [sys.executable, "-c", leader_code,
                grandchild_code, str(pid_path)]
        child_generation = fence.begin_child("agent", argv)

        publish_entered = threading.Event()
        allow_publish = threading.Event()
        original_publish = fence.publish_child_running

        def delayed_publish(generation, identity):
            publish_entered.set()
            if not allow_publish.wait(10):
                raise RuntimeError("test did not release durable publication")
            return original_publish(generation, identity)

        fence.publish_child_running = delayed_publish
        outcome = {}

        def spawn():
            try:
                outcome["child"] = fence.spawn_prepared_child(
                    child_generation, argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except BaseException as exc:  # test thread must report all failures
                outcome["error"] = exc

        thread = threading.Thread(target=spawn, daemon=True)
        thread.start()
        self.assertTrue(publish_entered.wait(10))
        time.sleep(0.3)
        self.assertFalse(pid_path.exists(),
                         "payload ran before child_running publication")
        self.assertFalse(Path(str(pid_path) + ".tick").exists())
        allow_publish.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        if "error" in outcome:
            raise outcome["error"]
        child = outcome["child"]
        self.assertEqual(child.wait(timeout=10), 0)

        deadline = time.monotonic() + 5
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(pid_path.exists(), "adversarial grandchild did not start")
        grandchild_pid = int(pid_path.read_text(encoding="utf-8"))
        if not compat.IS_WINDOWS:
            marker = child.record_result()
            self.assertEqual(marker["child_state"], "child_reaped")
            deadline = time.monotonic() + 5
            while (compat.process_is_alive(grandchild_pid)
                   and time.monotonic() < deadline):
                time.sleep(0.02)
            self.assertFalse(compat.process_is_alive(grandchild_pid))
            fence.terminalize("completed")
            return
        with self.assertRaisesRegex(repo_owner.OwnerBusy, "descendants"):
            child.record_result(containment_timeout=0.1)
        self.assertEqual(fence.marker["child_state"], "child_running")

        child.kill_containment(timeout=5)
        marker = child.record_result()
        self.assertEqual(marker["child_state"], "child_reaped")
        deadline = time.monotonic() + 5
        while compat.process_is_alive(grandchild_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(compat.process_is_alive(grandchild_pid))
        fence.terminalize("completed")

    def test_controlled_spawn_publication_failure_kills_without_payload_gap(self):
        compile(repo_owner._POSIX_BARRIER_CODE, "<repo-owner-barrier>", "exec")
        fence = self.claim()
        sentinel = self.root / "must-not-run.txt"
        argv = [
            sys.executable, "-c",
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_text('ran', encoding='utf-8')",
            str(sentinel),
        ]
        generation = fence.begin_child("tool", argv)

        def reject_publication(_generation, _identity):
            raise repo_owner.OwnerInvariantError("injected publication failure")

        fence.publish_child_running = reject_publication
        with self.assertRaisesRegex(repo_owner.OwnerInvariantError, "injected"):
            fence.spawn_prepared_child(
                generation, argv, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        self.assertFalse(sentinel.exists())
        self.assertEqual(fence.marker["child_state"], "launching")

    @unittest.skipUnless(
        os.name == "posix" and sys.platform.startswith("linux"),
        "Linux subreaper containment",
    )
    def test_posix_guardian_reaps_double_fork_setsid_descendant(self):
        fence = self.claim()
        pid_path = self.root / "detached.pid"
        sentinel = self.root / "detached-writer.txt"
        daemon_code = (
            "from pathlib import Path; import os,sys,time\n"
            "os.setsid()\n"
            "if os.fork(): os._exit(0)\n"
            "null=os.open('/dev/null',os.O_RDWR)\n"
            "for fd in (0,1,2): os.dup2(null,fd)\n"
            "Path(sys.argv[1]).write_text(str(os.getpid()),encoding='utf-8')\n"
            "time.sleep(1.0)\n"
            "Path(sys.argv[2]).write_text('escaped',encoding='utf-8')\n"
            "time.sleep(60)\n"
        )
        payload_code = (
            "from pathlib import Path; import subprocess,sys,time\n"
            "subprocess.Popen([sys.executable,'-c',sys.argv[3],"
            "sys.argv[1],sys.argv[2]],close_fds=True)\n"
            "deadline=time.monotonic()+5\n"
            "while not Path(sys.argv[1]).exists() and time.monotonic()<deadline: "
            "time.sleep(0.01)\n"
            "raise SystemExit(0 if Path(sys.argv[1]).exists() else 2)\n"
        )
        child = fence.spawn_child(
            "tool",
            [sys.executable, "-c", payload_code,
             str(pid_path), str(sentinel), daemon_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        running = fence.marker
        self.assertEqual(
            running["child_identity"]["containment_kind"],
            repo_owner._POSIX_GUARDIAN_KIND,
        )
        self.assertEqual(child.wait(timeout=12), 0)
        marker = child.record_result()
        self.assertEqual(marker["child_state"], "child_reaped")
        self.assertTrue(pid_path.exists())
        detached_pid = int(pid_path.read_text(encoding="utf-8"))
        time.sleep(1.2)
        self.assertFalse(sentinel.exists())
        self.assertFalse(compat.process_is_alive(detached_pid))
        fence.terminalize("completed")

    @unittest.skipUnless(
        os.name == "posix" and sys.platform.startswith("linux"),
        "Linux subreaper containment",
    )
    def test_posix_subreaper_readiness_failure_never_releases_payload(self):
        fence = self.claim()
        sentinel = self.root / "subreaper-failure-must-not-run.txt"
        argv = [
            sys.executable, "-c",
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_text('ran',encoding='utf-8')",
            str(sentinel),
        ]
        generation = fence.begin_child("tool", argv)
        original = repo_owner._POSIX_OWNER_GUARDIAN
        repo_owner._POSIX_OWNER_GUARDIAN = original.replace(
            "if not enable_subreaper():", "if True:", 1)
        try:
            with self.assertRaisesRegex(
                    repo_owner.OwnerInvariantError, "did not prove readiness"):
                fence.spawn_prepared_child(
                    generation, argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        finally:
            repo_owner._POSIX_OWNER_GUARDIAN = original
        time.sleep(0.2)
        self.assertFalse(sentinel.exists())
        self.assertEqual(fence.marker["child_state"], "launching")

    @unittest.skipUnless(
        os.name == "posix" and sys.platform.startswith("linux"),
        "Linux subreaper containment",
    )
    def test_owner_hard_kill_fences_detached_tree_and_leaves_guardian_marker(self):
        ready_path = self.root / "owner-child-ready.txt"
        pid_path = self.root / "owner-detached.pid"
        sentinel = self.root / "owner-detached-writer.txt"
        daemon_code = (
            "from pathlib import Path; import os,sys,time\n"
            "os.setsid()\n"
            "if os.fork(): os._exit(0)\n"
            "null=os.open('/dev/null',os.O_RDWR)\n"
            "for fd in (0,1,2): os.dup2(null,fd)\n"
            "Path(sys.argv[1]).write_text(str(os.getpid()),encoding='utf-8')\n"
            "time.sleep(1.5)\n"
            "Path(sys.argv[2]).write_text('escaped',encoding='utf-8')\n"
            "time.sleep(60)\n"
        )
        payload_code = (
            "import subprocess,sys,time\n"
            "subprocess.Popen([sys.executable,'-c',sys.argv[3],"
            "sys.argv[1],sys.argv[2]],close_fds=True)\n"
            "time.sleep(60)\n"
        )
        helper_code = (
            "from pathlib import Path; import subprocess,sys,time\n"
            "from engine import repo_owner\n"
            "f=repo_owner.RepoOwnerFence.claim("
            "Path(sys.argv[1]),owner_kind='loop',workspace=Path(sys.argv[2]),"
            "state_path=Path(sys.argv[3]))\n"
            "argv=[sys.executable,'-c',sys.argv[7],sys.argv[5],sys.argv[6],sys.argv[8]]\n"
            "c=f.spawn_child('tool',argv,stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "Path(sys.argv[4]).write_text(str(c.pid),encoding='utf-8')\n"
            "time.sleep(60)\n"
        )
        helper = subprocess.Popen(
            [sys.executable, "-c", helper_code,
             str(self.repo), str(self.workspace), str(self.state_path),
             str(ready_path), str(pid_path), str(sentinel),
             payload_code, daemon_code],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        def stop_helper():
            if helper.poll() is None:
                helper.kill()
                helper.wait(timeout=5)

        self.addCleanup(stop_helper)
        deadline = time.monotonic() + 10
        while ((not ready_path.exists() or not pid_path.exists())
               and time.monotonic() < deadline):
            time.sleep(0.02)
        self.assertTrue(ready_path.exists(), "owner helper did not publish guardian")
        self.assertTrue(pid_path.exists(), "detached payload did not start")
        guardian_pid = int(ready_path.read_text(encoding="utf-8"))
        detached_pid = int(pid_path.read_text(encoding="utf-8"))
        guardian_identity = compat.capture_process_identity(guardian_pid)

        def stop_guardian():
            if compat.process_matches_identity(
                    guardian_pid, guardian_identity["start_token"],
                    guardian_identity["group_id"]):
                compat.fence_process_tree(
                    guardian_pid,
                    start_token=guardian_identity["start_token"],
                    group_id=guardian_identity["group_id"],
                    graceful_timeout=0.1,
                    force_timeout=3.0,
                )

        self.addCleanup(stop_guardian)
        helper.kill()
        helper.wait(timeout=5)
        deadline = time.monotonic() + 8
        while (compat.process_is_alive(detached_pid)
               and time.monotonic() < deadline):
            time.sleep(0.02)
        self.assertFalse(compat.process_is_alive(detached_pid))
        time.sleep(1.7)
        self.assertFalse(sentinel.exists())
        self.assertTrue(compat.process_matches_identity(
            guardian_pid, guardian_identity["start_token"],
            guardian_identity["group_id"]))
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["child_state"], "child_running")
        self.assertEqual(
            marker["child_identity"]["containment_kind"],
            repo_owner._POSIX_GUARDIAN_KIND,
        )
        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            repo_owner.RepoOwnerFence.claim(
                self.repo,
                owner_kind="loop",
                workspace=self.workspace,
                state_path=self.state_path,
                session=SESSION_B,
                owner_identity=OWNER_B,
                boot_identity="test-boot-a",
            )

    @unittest.skipUnless(compat.IS_WINDOWS, "Windows suspended resume fault")
    def test_windows_resume_failure_kills_job_and_records_reaped_marker(self):
        fence = self.claim()
        sentinel = self.root / "must-not-resume.txt"
        argv = [
            sys.executable, "-c",
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_text('ran', encoding='utf-8')",
            str(sentinel),
        ]
        generation = fence.begin_child("git", argv)
        original_resume = repo_owner._windows_resume_suspended_primary_thread

        def reject_resume(_pid):
            raise repo_owner.OwnerInvariantError("injected resume failure")

        repo_owner._windows_resume_suspended_primary_thread = reject_resume
        try:
            with self.assertRaisesRegex(repo_owner.OwnerInvariantError, "resume"):
                fence.spawn_prepared_child(
                    generation, argv, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            repo_owner._windows_resume_suspended_primary_thread = original_resume
        time.sleep(0.3)
        self.assertFalse(sentinel.exists())
        self.assertEqual(fence.marker["child_state"], "child_reaped")
        fence.terminalize("resume-failure-quiesced")

    @unittest.skipUnless(compat.IS_WINDOWS, "Windows strict Job breakaway policy")
    def test_windows_controlled_payload_cannot_escape_with_explicit_breakaway(self):
        fence = self.claim()
        sentinel = self.root / "escaped-writer.txt"
        outcome = self.root / "breakaway-outcome.txt"
        descendant_code = (
            "from pathlib import Path; import sys,time; time.sleep(0.8); "
            "Path(sys.argv[1]).write_text('escaped', encoding='utf-8')"
        )
        payload_code = (
            "from pathlib import Path; import subprocess,sys; "
            "out=Path(sys.argv[1]); "
            "cmd=[sys.executable,'-c',sys.argv[3],sys.argv[2]]; "
            "\ntry:\n"
            " subprocess.Popen(cmd,creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB,"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "out.write_text('spawned',encoding='utf-8')\n"
            "except OSError:\n out.write_text('blocked',encoding='utf-8')\n"
        )
        child = fence.spawn_child(
            "tool",
            [sys.executable, "-c", payload_code,
             str(outcome), str(sentinel), descendant_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        running = fence.marker
        self.assertEqual(
            running["child_identity"]["containment_kind"],
            repo_owner._WINDOWS_STRICT_JOB_KIND,
        )
        self.assertTrue(compat.verify_process_group_containment(
            child.process, allow_breakaway=False))
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertIn(
            outcome.read_text(encoding="utf-8"), {"blocked", "spawned"})
        try:
            marker = child.record_result(containment_timeout=0.2)
        except repo_owner.OwnerBusy:
            # A platform may keep a requested child in the strict Job rather
            # than reject CreateProcess.  Either outcome must remain fenced.
            child.kill_containment(timeout=5)
            marker = child.record_result()
        self.assertEqual(marker["child_state"], "child_reaped")
        time.sleep(1.1)
        self.assertFalse(sentinel.exists())
        fence.terminalize("completed")

    def test_reaped_checkpoint_allows_next_child_with_monotonic_generation(self):
        fence = self.claim()
        first = fence.begin_child("agent", ["agent"])
        fence.publish_child_running(first, CHILD_A)
        fence.record_child_result(first, 0)
        idle = fence.checkpoint_child(first)
        self.assertEqual(idle["child_state"], "idle")
        self.assertEqual(idle["child_generation"], 1)

        second = fence.begin_child("validator", ["validate"])
        self.assertEqual(second, 2)
        fence.publish_child_running(second, {**CHILD_A, "pid": 304})
        fence.record_child_result(second, 0)
        fence.terminalize("completed")

    def test_terminal_to_active_is_exact_generation_increment(self):
        first = self.claim()
        first.terminalize("completed")
        second = self.claim(session=SESSION_B, owner_kind="ralph", identity=OWNER_B)
        self.assertEqual(second.marker["generation"], 2)
        self.assertEqual(second.marker["owner_kind"], "ralph")
        self.assertEqual(second.marker["state"], "active")
        second.terminalize("completed")

    def test_foreign_active_and_recovering_markers_fail_closed(self):
        first = self.claim()
        first.close()  # simulate abrupt parent loss; durable marker stays active
        before = repo_owner.RepoOwnerFence.inspect(self.repo)

        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            self.claim(session=SESSION_B, identity=OWNER_B)
        self.assertEqual(repo_owner.RepoOwnerFence.inspect(self.repo), before)

        recovering = self.recover(lambda _marker: True)
        self.assertEqual(recovering.marker["state"], "recovering")
        recovering.close()  # a second recovery owner may also crash
        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            self.claim(session=SESSION_C, identity=OWNER_B)

    def test_recovery_requires_exact_recorded_authority_and_literal_true(self):
        first = self.claim()
        first.begin_child("git", ["git", "status"])
        first.close()
        original = repo_owner.RepoOwnerFence.inspect(self.repo)
        callback_called = False

        def should_not_run(_marker):
            nonlocal callback_called
            callback_called = True
            return True

        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            self.recover(
                should_not_run, expected_session=SESSION_B,
                expected_generation=1)
        self.assertFalse(callback_called)

        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            self.recover(lambda _marker: 1)
        self.assertEqual(repo_owner.RepoOwnerFence.inspect(self.repo), original)

        saw_global_lock = False

        def prove_fenced(snapshot):
            nonlocal saw_global_lock
            self.assertEqual(snapshot, original)
            stream = open(
                self.common_dir / repo_owner.GLOBAL_LOCK_NAME, "r+b")
            try:
                try:
                    compat.lock_file(stream, blocking=False)
                except (BlockingIOError, PermissionError):
                    saw_global_lock = True
                else:
                    compat.unlock_file(stream)
            finally:
                stream.close()
            return saw_global_lock

        recovered = self.recover(prove_fenced)
        marker = recovered.marker
        self.assertTrue(saw_global_lock)
        self.assertEqual(marker["state"], "recovering")
        self.assertEqual(marker["generation"], 2)
        self.assertEqual(marker["session"], SESSION_B)
        self.assertEqual(marker["child_state"], "child_reaped")
        self.assertEqual(marker["child_result"]["status"], "recovered")
        self.assertEqual(marker["recovery_history"][-1]["from_session"], SESSION_A)
        recovered.terminalize("manual-recovery-complete")

    def test_recovery_callback_cannot_hide_a_marker_cas_change(self):
        first = self.claim()
        first.close()

        def mutate_marker(_snapshot):
            current = json.loads(self.marker_path.read_text(encoding="utf-8"))
            current["updated_at"] = "2099-01-01T00:00:00Z"
            replacement = self.marker_path.with_suffix(".replacement")
            replacement.write_text(
                json.dumps(current, sort_keys=True, separators=(",", ":")),
                encoding="utf-8")
            os.replace(replacement, self.marker_path)
            return True

        with self.assertRaisesRegex(repo_owner.OwnerBusy, "changed during"):
            self.recover(mutate_marker)
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["state"], "active")
        self.assertEqual(marker["generation"], 1)

    def test_nonterminal_repo_executor_lease_blocks_without_marker_mutation(self):
        lease_path = self.write_executor_lease(self.executor_lease(state="running"))
        with self.assertRaisesRegex(repo_owner.OwnerBusy, "nonterminal"):
            self.claim()
        self.assertFalse(os.path.lexists(self.marker_path))

        self.write_executor_lease(self.executor_lease(state="terminal"))
        fence = self.claim()
        self.assertEqual(fence.marker["state"], "active")
        fence.terminalize("completed")

    def test_blocked_terminal_executor_lease_requires_recovery(self):
        lease = self.executor_lease(state="terminal")
        lease.update({
            "terminal_status": "blocked",
            "result_hash": None,
            "reason": "fault after repository mutation",
        })
        self.write_executor_lease(lease)
        with self.assertRaisesRegex(repo_owner.OwnerBusy, "requires exact recovery"):
            self.claim()
        self.assertFalse(os.path.lexists(self.marker_path))

    def test_terminal_executor_result_must_match_lease(self):
        lease = self.executor_lease(state="terminal")
        self.write_executor_lease(lease)
        result_path = (self.common_dir / repo_owner.EXECUTOR_SIDECAR_NAME
                       / "operation-results" / f"{lease['operation_id']}.json")
        artifact = json.loads(result_path.read_text(encoding="utf-8"))
        artifact["result"]["status"] = "different"
        result_path.write_text(json.dumps(artifact), encoding="utf-8")
        with self.assertRaisesRegex(
                repo_owner.OwnerInvariantError, "does not match"):
            self.claim()
        self.assertFalse(os.path.lexists(self.marker_path))

    def test_terminal_executor_accepts_strict_windows_job_v2_evidence(self):
        lease = self.executor_lease(state="terminal")
        lease.update({
            "child_generation": 1,
            "child_history": [{
                "generation": 1,
                "kind": "git",
                "argv_hash": "8" * 64,
                "identity": {
                    "pid": 123,
                    "start_token": "windows-created",
                    "group_id": 123,
                    "containment_kind": "windows-job-no-breakaway-v2",
                },
                "result": {
                    "status": "exited", "returncode": 0,
                    "recorded_at": "2026-07-22T00:00:00Z",
                },
            }],
        })
        self.write_executor_lease(lease)
        fence = self.claim()
        self.assertEqual(fence.marker["state"], "active")
        fence.terminalize("completed")

    def test_read_only_audit_allows_missing_and_terminal_without_artifacts(self):
        global_path = self.common_dir / repo_owner.GLOBAL_LOCK_NAME
        marker_lock = self.common_dir / repo_owner.MARKER_LOCK_NAME
        with open(global_path, "a+b") as global_lock:
            compat.lock_file(global_lock, blocking=False)
            try:
                self.assertIsNone(repo_owner.audit_owner_marker_under_global_lock(
                    self.repo, global_lock))
                self.assertFalse(self.marker_path.exists())
                self.assertFalse(marker_lock.exists(),
                                 "read-only audit created marker lock")
            finally:
                compat.unlock_file(global_lock)

        fence = self.claim()
        terminal = fence.terminalize("completed")
        before = self.marker_path.read_bytes()
        with open(global_path, "r+b") as global_lock:
            compat.lock_file(global_lock, blocking=False)
            try:
                audited = repo_owner.audit_owner_marker_under_global_lock(
                    self.repo, global_lock)
            finally:
                compat.unlock_file(global_lock)
        self.assertEqual(audited, terminal)
        self.assertEqual(self.marker_path.read_bytes(), before)

    def test_read_only_audit_blocks_active_and_recovering_without_mutation(self):
        active = self.claim()
        before = self.marker_path.read_bytes()
        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            repo_owner.audit_owner_marker_under_global_lock(
                self.repo, active._global_lock_file)
        self.assertEqual(self.marker_path.read_bytes(), before)
        active.close()

        recovering = self.recover(lambda _marker: True)
        before = self.marker_path.read_bytes()
        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            repo_owner.audit_owner_marker_under_global_lock(
                self.repo, recovering._global_lock_file)
        self.assertEqual(self.marker_path.read_bytes(), before)

    def test_read_only_audit_rejects_noncanonical_lock_descriptor(self):
        wrong_path = self.root / "wrong.lock"
        with open(wrong_path, "w+b") as wrong:
            with self.assertRaises(repo_owner.OwnerAuthorityError):
                repo_owner.audit_owner_marker_under_global_lock(
                    self.repo, wrong)

    def test_malformed_executor_lease_and_owner_marker_fail_closed(self):
        malformed = self.executor_lease(state="terminal")
        malformed["operation"] = "SHELL"
        self.write_executor_lease(malformed)
        with self.assertRaises(repo_owner.OwnerInvariantError):
            self.claim()
        self.assertFalse(os.path.lexists(self.marker_path))

        # Remove the malformed executor artifact, create a valid marker, then
        # prove an unknown owner enum is not silently replaced.
        shutil.rmtree(self.common_dir / repo_owner.EXECUTOR_SIDECAR_NAME)
        fence = self.claim()
        fence.close()
        marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        marker["owner_kind"] = "unknown-mutator"
        self.marker_path.write_text(json.dumps(marker), encoding="utf-8")
        with self.assertRaises(repo_owner.OwnerInvariantError):
            self.claim(session=SESSION_B, identity=OWNER_B)

    def test_unknown_enums_and_out_of_order_transitions_are_rejected(self):
        with self.assertRaises(repo_owner.OwnerAuthorityError):
            self.claim(owner_kind="arbitrary-shell")
        self.assertFalse(os.path.lexists(self.marker_path))

        fence = self.claim()
        with self.assertRaises(repo_owner.OwnerAuthorityError):
            fence.begin_child("arbitrary", ["cmd"])
        with self.assertRaises(repo_owner.OwnerBusy):
            fence.publish_child_running(1, CHILD_A)
        generation = fence.begin_child("tool", ["tool"])
        with self.assertRaises(repo_owner.OwnerBusy):
            fence.record_child_result(generation, 0)
        with self.assertRaises(repo_owner.OwnerAuthorityError):
            fence.publish_child_running(generation, {
                **CHILD_A, "containment_kind": "uncontained"})
        fence.publish_child_running(generation, CHILD_A)
        with self.assertRaises(repo_owner.OwnerBusy):
            fence.checkpoint_child(generation)
        fence.record_child_result(generation, 0)
        fence.terminalize("completed")

    def test_linked_worktree_resolves_to_same_common_marker_and_primary_repo(self):
        linked = self.root / "linked"
        _git(self.repo, "worktree", "add", "-qb", "linked-owner-test", str(linked))
        fence = repo_owner.RepoOwnerFence.claim(
            linked,
            owner_kind="cli-launcher",
            workspace=self.workspace,
            state_path=self.state_path,
            session=SESSION_A,
            owner_identity=OWNER_A,
            boot_identity="test-boot-a",
        )
        self.addCleanup(fence.close)
        self.assertEqual(fence.common_dir, self.common_dir)
        self.assertEqual(Path(fence.marker["canonical_repo"]), self.repo)
        fence.close()
        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            self.claim(session=SESSION_B, identity=OWNER_B)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
