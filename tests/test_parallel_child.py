import contextlib
import io
import os
import json
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from engine import parallel_child
from engine import parallel_contract
from engine import parallel_spool
from engine import parallel_worker
from engine import platform_compat as compat


ROOT = Path(__file__).resolve().parent.parent
RUN_ID = "a1b2c3d4"
SESSION = "a" * 32


def _wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class TestChildRecord(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name)
        self.child_id = "1" * 32
        self.payload = [sys.executable, "-c", "raise SystemExit(7)"]

    def tearDown(self):
        self.temp.cleanup()

    def record(self, **overrides):
        values = {
            "run_id": RUN_ID,
            "task": 1,
            "child_id": self.child_id,
            "supervisor_session": SESSION,
            "supervisor_generation": 1,
            "attempt": 0,
            "resume": False,
            "guardian_pid": 2222,
            "guardian_start_token": "test:guardian-2222",
            "argv_hash": parallel_child.payload_argv_hash(self.payload),
            "state": "guardian_ready",
            "returncode": None,
        }
        values.update(overrides)
        return parallel_child.child_record(**values)

    def test_hash_and_guardian_argv_are_canonical(self):
        self.assertEqual(
            parallel_child.payload_argv_hash(self.payload),
            parallel_child.payload_argv_hash(tuple(self.payload)),
        )
        self.assertNotEqual(
            parallel_child.payload_argv_hash(self.payload),
            parallel_child.payload_argv_hash([*self.payload, "different"]),
        )
        argv = parallel_child.build_guardian_argv(
            sys.executable,
            self.run_dir,
            1,
            self.child_id,
            self.payload,
        )
        self.assertEqual(argv[:3], [sys.executable, "-m", "engine.parallel_child"])
        self.assertEqual(argv[-len(self.payload):], self.payload)
        self.assertEqual(argv[argv.index("--task") + 1], "1")
        self.assertEqual(argv[argv.index("--child-id") + 1], self.child_id)
        self.assertEqual(
            Path(argv[argv.index("--run-dir") + 1]), self.run_dir.absolute())

    def test_argv_rejects_ambiguous_or_unsafe_values(self):
        for value in ("python -c pass", b"python", [], ["bad\x00name"]):
            with self.subTest(value=value):
                with self.assertRaises(parallel_child.ParallelChildError):
                    parallel_child.payload_argv_hash(value)

    def test_exact_record_schema_and_forward_only_transitions(self):
        ready = self.record()
        expected_fields = {
            "schema", "run_id", "task", "child_id", "supervisor_session",
            "supervisor_generation", "attempt", "resume", "guardian_pid",
            "guardian_start_token", "argv_hash", "payload_pid",
            "payload_start_token", "payload_group_id",
            "payload_containment", "state", "returncode",
        }
        self.assertEqual(set(ready), expected_fields)
        self.assertNotIn("argv", ready)
        self.assertNotIn("dispatch_token", ready)

        path = parallel_child.write_child_record(self.run_dir, ready)
        self.assertEqual(
            path,
            self.run_dir / "children" / "task-1" / f"{self.child_id}.json",
        )
        self.assertEqual(
            parallel_child.write_child_record(self.run_dir, ready), path)
        self.assertEqual(
            parallel_child.read_child_record(self.run_dir, 1, self.child_id), ready)

        acked = dict(
            ready,
            state="acked",
            payload_pid=3333,
            payload_start_token="test:payload-3333",
            payload_group_id=3333,
            payload_containment=(
                "windows-job-kill-on-close-v1"
                if compat.IS_WINDOWS else "posix-exact-tree-v1"),
        )
        parallel_child.write_child_record(self.run_dir, acked)
        self.assertEqual(
            parallel_child.write_child_record(self.run_dir, acked), path)

        conflicting = dict(acked, guardian_pid=3333)
        with self.assertRaises(parallel_child.ParallelChildError):
            parallel_child.write_child_record(self.run_dir, conflicting)

        reaped = dict(acked, state="reaped", returncode=7)
        parallel_child.write_child_record(self.run_dir, reaped)
        self.assertEqual(
            parallel_child.read_child_record(self.run_dir, 1, self.child_id),
            reaped,
        )
        with self.assertRaises(parallel_child.ParallelChildError):
            parallel_child.write_child_record(self.run_dir, acked)

    def test_ready_can_terminalize_after_pre_ack_guardian_is_waited(self):
        ready = self.record(child_id="2" * 32)
        parallel_child.write_child_record(self.run_dir, ready)
        reaped = dict(
            ready,
            state="reaped",
            returncode=parallel_child.GUARDIAN_PROTOCOL_RC,
        )
        path = parallel_child.write_child_record(self.run_dir, reaped)
        self.assertEqual(
            path,
            self.run_dir / "children" / "task-1" / f"{'2' * 32}.json",
        )
        self.assertEqual(
            parallel_child.read_child_record(self.run_dir, 1, "2" * 32),
            reaped,
        )
        self.assertEqual(parallel_child.write_child_record(self.run_dir, reaped), path)

    def test_record_cannot_be_created_after_ready(self):
        acked = self.record(
            child_id="3" * 32,
            state="acked",
            payload_pid=3333,
            payload_start_token="test:payload-3333",
            payload_group_id=3333,
            payload_containment=(
                "windows-job-kill-on-close-v1"
                if compat.IS_WINDOWS else "posix-exact-tree-v1"),
        )
        with self.assertRaises(parallel_child.ParallelChildError):
            parallel_child.write_child_record(self.run_dir, acked)

    def test_record_validation_rejects_extra_fields_and_bad_returncode(self):
        ready = self.record()
        with self.assertRaises(parallel_child.ParallelChildError):
            parallel_child.validate_child_record({**ready, "argv": self.payload})
        with self.assertRaises(parallel_child.ParallelChildError):
            parallel_child.validate_child_record({**ready, "returncode": 0})
        with self.assertRaises(parallel_child.ParallelChildError):
            parallel_child.validate_child_record(
                {**ready, "state": "reaped", "returncode": None})


class _RecordingPipe(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.snapshot = b""

    def close(self):
        if not self.closed:
            self.snapshot = self.getvalue()
        super().close()


class TestGuardianLaunchBarrier(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name).resolve()
        self.child_id = "9" * 32
        self.payload = [sys.executable, "-c", "raise SystemExit(0)"]
        self.artifacts = SimpleNamespace(
            run_dir=self.run_dir,
            manifest={"run_id": RUN_ID, "parent_workspace": "base"},
            manifest_hash="1" * 64,
            run_config_hash="2" * 64,
            assignment_hashes={1: "3" * 64},
        )
        self.expected_reservation = {
            "schema": 1,
            "request_id": self.child_id,
            "run_id": RUN_ID,
            "task": 1,
            "manifest_hash": "1" * 64,
            "run_config_hash": "2" * 64,
            "launch_spec_hash": "3" * 64,
            "supervisor_session": SESSION,
            "supervisor_generation": 1,
            "attempt": 0,
            "resume": False,
        }
        self.acked = {
            "run_id": RUN_ID,
            "task": 1,
            "child_id": self.child_id,
            "supervisor_session": SESSION,
            "supervisor_generation": 1,
            "attempt": 0,
            "resume": False,
            "state": "acked",
            "payload_pid": 3333,
        }

    def tearDown(self):
        self.temp.cleanup()

    def _spool(self):
        return parallel_spool.DurableSpool(self.run_dir / "launches")

    def test_cancel_wins_claim_cas_and_no_authorized_response_is_forged(self):
        spool = self._spool()
        spool.publish_request(self.child_id, self.expected_reservation)
        cancelled = spool.cancel_request(self.child_id)
        self.assertTrue(cancelled.transitioned)
        with mock.patch.object(
                parallel_worker, "_validated_launch_artifacts",
                return_value=self.artifacts), \
                mock.patch.object(parallel_worker, "_require_live_parent"):
            with self.assertRaisesRegex(
                    parallel_contract.ParallelContractError, "不可 claim"):
                parallel_worker.claim_guardian_launch(
                    self.run_dir, self.acked, payload_pid=3333)
        self.assertEqual(spool.get_request(self.child_id).state, "cancelled")
        self.assertIsNone(spool.get_response(self.child_id))

    def test_claim_wins_and_authorized_response_binds_acked_payload_pid(self):
        spool = self._spool()
        spool.publish_request(self.child_id, self.expected_reservation)
        with mock.patch.object(
                parallel_worker, "_validated_launch_artifacts",
                return_value=self.artifacts), \
                mock.patch.object(parallel_worker, "_require_live_parent") as live:
            result = parallel_worker.claim_guardian_launch(
                self.run_dir, self.acked, payload_pid=3333)
        self.assertIs(result, self.artifacts)
        self.assertEqual(live.call_count, 2)
        self.assertEqual(spool.get_request(self.child_id).state, "claimed")
        self.assertEqual(spool.get_response(self.child_id).payload, {
            "schema": 1,
            "request_id": self.child_id,
            "status": "authorized",
            "pid": 3333,
            "supervisor_session": SESSION,
            "supervisor_generation": 1,
            "attempt": 0,
        })

    def test_guardian_never_writes_payload_go_when_authorizer_rejects(self):
        ready = parallel_child.child_record(
            run_id=RUN_ID,
            task=1,
            child_id=self.child_id,
            supervisor_session=SESSION,
            supervisor_generation=1,
            attempt=0,
            resume=False,
            guardian_pid=os.getpid(),
            guardian_start_token="test:guardian",
            argv_hash=parallel_child.payload_argv_hash(self.payload),
            state="guardian_ready",
        )
        parallel_child.write_child_record(self.run_dir, ready)
        pipe = _RecordingPipe()
        process = mock.Mock(pid=3333, stdin=pipe)
        process.poll.return_value = None
        thread = mock.Mock()
        sentinel = self.run_dir / "worker" / "console.log"

        def cancelled(_run_dir, record, *, payload_pid):
            self.assertEqual(record["state"], "acked")
            self.assertEqual(payload_pid, 3333)
            self.assertEqual(pipe.getvalue(), b"")
            raise parallel_worker.LaunchReservationUnavailable(
                "reservation cancelled")

        with mock.patch.object(compat, "IS_WINDOWS", True), \
                mock.patch.object(compat, "popen_group_kwargs", return_value={}), \
                mock.patch.object(parallel_child.subprocess, "Popen",
                                  return_value=process), \
                mock.patch.object(compat, "attach_process_group", return_value=True), \
                mock.patch.object(compat, "capture_process_identity", return_value={
                    "pid": 3333, "start_token": "test:payload", "group_id": 3333,
                }), \
                mock.patch.object(compat, "process_matches_identity", return_value=True), \
                mock.patch.object(parallel_child, "_terminate_payload") as terminate, \
                mock.patch.object(compat, "close_process_group"), \
                mock.patch.object(parallel_child, "_install_signal_handlers",
                                  return_value={}), \
                mock.patch.object(parallel_child, "_restore_signal_handlers"), \
                mock.patch.object(parallel_child.threading, "Thread",
                                  return_value=thread):
            terminate.side_effect = lambda _process: setattr(
                process.poll, "return_value", 0)
            rc = parallel_child.run_guardian(
                self.payload,
                run_dir=self.run_dir,
                task=1,
                child_id=self.child_id,
                control_stream=io.BytesIO(parallel_child.ACK_BYTE),
                launch_authorizer=cancelled,
            )
        self.assertEqual(rc, parallel_child.GUARDIAN_CANCELLED_RC)
        terminate.assert_called_once_with(process)
        self.assertNotIn(parallel_child.PAYLOAD_GO_BYTE, pipe.snapshot)
        self.assertFalse(sentinel.exists())


class TestGuardianProtocol(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name)
        self.processes = []
        self.counter = 0

    def tearDown(self):
        for process in reversed(self.processes):
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
                process.stdin = None
            if process.poll() is None:
                try:
                    compat.kill_process_group(process)
                except (OSError, ProcessLookupError, ValueError):
                    try:
                        process.kill()
                    except OSError:
                        pass
            try:
                compat.wait_process(process, timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                compat.wait_process(process)
            compat.close_process_group(process)
            for stream_name in ("stdout", "stderr"):
                stream = getattr(process, stream_name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        self.temp.cleanup()

    def _next_child_id(self):
        self.counter += 1
        return f"{self.counter:032x}"

    def _spawn(self, payload, *, recorded_hash=None, recorded_pid=None):
        child_id = self._next_child_id()
        argv = parallel_child.build_guardian_argv(
            sys.executable,
            self.run_dir,
            1,
            child_id,
            payload,
        )
        argv[2] = "tests.parallel_child_harness"
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        process = subprocess.Popen(
            argv,
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **compat.popen_group_kwargs(),
        )
        self.processes.append(process)
        self.assertIs(compat.attach_process_group(process), True)
        ready = parallel_child.child_record(
            run_id=RUN_ID,
            task=1,
            child_id=child_id,
            supervisor_session=SESSION,
            supervisor_generation=1,
            attempt=0,
            resume=False,
            guardian_pid=(process.pid if recorded_pid is None else recorded_pid),
            guardian_start_token=compat.process_start_token(process.pid),
            argv_hash=(parallel_child.payload_argv_hash(payload)
                       if recorded_hash is None else recorded_hash),
            state="guardian_ready",
        )
        parallel_child.write_child_record(self.run_dir, ready)
        return process, child_id, ready

    def _close_control(self, process):
        self.assertIsNotNone(process.stdin)
        process.stdin.close()
        process.stdin = None

    def _finish(self, process, timeout=10):
        returncode = compat.wait_process(process, timeout=timeout)
        stdout = process.stdout.read().decode("utf-8", "replace")
        stderr = process.stderr.read().decode("utf-8", "replace")
        process.stdout.close()
        process.stderr.close()
        return returncode, stdout, stderr

    def test_eof_before_ack_never_spawns_payload(self):
        marker = self.run_dir / "before-ack.txt"
        payload = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('ran')",
            str(marker),
        ]
        process, child_id, ready = self._spawn(payload)
        self._close_control(process)
        returncode, _stdout, _stderr = self._finish(process)
        self.assertEqual(returncode, parallel_child.GUARDIAN_CANCELLED_RC)
        self.assertFalse(marker.exists())
        self.assertEqual(
            parallel_child.read_child_record(self.run_dir, 1, child_id), ready)

        recovered = parallel_child.recover_orphan_child(
            self.run_dir, 1, child_id, expected_record=ready)
        self.assertEqual(recovered["state"], "reaped")
        self.assertEqual(
            recovered["returncode"], parallel_child.GUARDIAN_CANCELLED_RC)

    def test_live_guardian_ready_cannot_be_recovered(self):
        payload = [sys.executable, "-c", "raise SystemExit(0)"]
        process, child_id, ready = self._spawn(payload)
        with self.assertRaisesRegex(
                parallel_child.ParallelChildError, "guardian is still alive"):
            parallel_child.recover_orphan_child(
                self.run_dir, 1, child_id, expected_record=ready)
        self._close_control(process)
        self._finish(process)

    def test_payload_waits_for_ack_then_reaps_with_its_returncode(self):
        marker = self.run_dir / "acked.txt"
        payload = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import sys; "
                "Path(sys.argv[1]).write_text('ran'); raise SystemExit(7)"
            ),
            str(marker),
        ]
        process, child_id, _ready = self._spawn(payload)
        time.sleep(0.2)
        self.assertFalse(marker.exists())
        self.assertIsNone(process.poll())

        process.stdin.write(parallel_child.ACK_BYTE)
        process.stdin.flush()
        returncode, _stdout, stderr = self._finish(process)
        self.assertEqual(stderr, "")
        self.assertEqual(returncode, 7)
        self.assertTrue(marker.exists())
        record = parallel_child.read_child_record(self.run_dir, 1, child_id)
        self.assertEqual(record["state"], "reaped")
        self.assertEqual(record["returncode"], 7)
        self.assertEqual(
            record["payload_containment"],
            (parallel_child.WINDOWS_STRICT_PAYLOAD_CONTAINMENT
             if compat.IS_WINDOWS else "posix-exact-tree-v1"),
        )

    @unittest.skipIf(compat.IS_WINDOWS, "POSIX subreaper containment only")
    def test_natural_payload_exit_fences_detached_descendant_before_reaped(self):
        descendant_pid_file = self.run_dir / "natural-exit-descendant.pid"
        descendant_code = (
            "from pathlib import Path; import os,sys,time; "
            "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
        )
        payload_code = (
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
        payload = [
            sys.executable,
            "-c",
            payload_code,
            str(descendant_pid_file),
            descendant_code,
        ]
        guardian, child_id, _ready = self._spawn(payload)
        guardian.stdin.write(parallel_child.ACK_BYTE)
        guardian.stdin.flush()
        self.assertTrue(_wait_for(descendant_pid_file.exists))
        descendant_pid = int(descendant_pid_file.read_text())

        def cleanup_descendant():
            if compat.process_is_alive(descendant_pid):
                try:
                    os.kill(descendant_pid, signal.SIGKILL)
                except (OSError, ProcessLookupError, PermissionError):
                    pass

        self.addCleanup(cleanup_descendant)
        returncode, _stdout, stderr = self._finish(guardian)
        self.assertEqual(stderr, "")
        self.assertEqual(returncode, 0)
        record = parallel_child.read_child_record(
            self.run_dir, 1, child_id)
        self.assertEqual(record["state"], "reaped")
        self.assertEqual(record["returncode"], 0)
        self.assertTrue(
            _wait_for(lambda: not compat.process_is_alive(descendant_pid)),
            f"detached descendant pid {descendant_pid} survived natural exit",
        )

    def test_windows_payload_attach_requests_no_breakaway_before_strict_ack(self):
        child_id = "b" * 32
        payload = [sys.executable, "-c", "raise SystemExit(0)"]
        ready = parallel_child.child_record(
            run_id=RUN_ID,
            task=1,
            child_id=child_id,
            supervisor_session=SESSION,
            supervisor_generation=1,
            attempt=0,
            resume=False,
            guardian_pid=os.getpid(),
            guardian_start_token="test:guardian",
            argv_hash=parallel_child.payload_argv_hash(payload),
            state="guardian_ready",
        )
        parallel_child.write_child_record(self.run_dir, ready)
        process = mock.Mock()
        process.pid = 3333
        process.stdin = io.BytesIO()
        process.poll.return_value = 0
        thread = mock.Mock()
        identity = {
            "pid": 3333,
            "start_token": "test:payload",
            "group_id": 3333,
        }
        with mock.patch.object(compat, "IS_WINDOWS", True), \
                mock.patch.object(compat, "popen_group_kwargs", return_value={}), \
                mock.patch.object(parallel_child.subprocess, "Popen",
                                  return_value=process), \
                mock.patch.object(compat, "attach_process_group",
                                  return_value=True) as attach, \
                mock.patch.object(compat, "capture_process_identity",
                                  return_value=identity), \
                mock.patch.object(compat, "process_matches_identity",
                                  return_value=True), \
                mock.patch.object(compat, "wait_process", return_value=0), \
                mock.patch.object(compat, "close_process_group"), \
                mock.patch.object(parallel_child, "_install_signal_handlers",
                                  return_value={}), \
                mock.patch.object(parallel_child, "_restore_signal_handlers"), \
                mock.patch.object(parallel_child.threading, "Thread",
                                  return_value=thread):
            returncode = parallel_child.run_guardian(
                payload,
                run_dir=self.run_dir,
                task=1,
                child_id=child_id,
                control_stream=io.BytesIO(parallel_child.ACK_BYTE),
                launch_authorizer=(
                    lambda _run_dir, _record, *, payload_pid: None),
            )

        self.assertEqual(returncode, 0)
        attach.assert_called_once_with(process, allow_breakaway=False)
        record = parallel_child.read_child_record(self.run_dir, 1, child_id)
        self.assertEqual(record["state"], "reaped")
        self.assertEqual(
            record["payload_containment"],
            parallel_child.WINDOWS_STRICT_PAYLOAD_CONTAINMENT,
        )

    def test_posix_reap_publication_requires_descendant_proof(self):
        child_id = "c" * 32
        payload = [sys.executable, "-c", "raise SystemExit(0)"]
        ready = parallel_child.child_record(
            run_id=RUN_ID,
            task=1,
            child_id=child_id,
            supervisor_session=SESSION,
            supervisor_generation=1,
            attempt=0,
            resume=False,
            guardian_pid=os.getpid(),
            guardian_start_token="test:guardian",
            argv_hash=parallel_child.payload_argv_hash(payload),
            state="guardian_ready",
        )
        parallel_child.write_child_record(self.run_dir, ready)
        process = mock.Mock()
        process.pid = 3333
        process.stdin = io.BytesIO()
        process.poll.return_value = 0
        thread = mock.Mock()
        identity = {
            "pid": 3333,
            "start_token": "test:payload",
            "group_id": 3333,
        }
        stderr = io.StringIO()
        with (
            mock.patch.object(compat, "IS_WINDOWS", False),
            mock.patch.object(
                parallel_child, "_enable_posix_subreaper", return_value=True),
            mock.patch.object(compat, "popen_group_kwargs", return_value={}),
            mock.patch.object(
                parallel_child.subprocess, "Popen", return_value=process),
            mock.patch.object(
                compat, "attach_process_group", return_value=True),
            mock.patch.object(
                compat, "capture_process_identity", return_value=identity),
            mock.patch.object(
                compat, "process_matches_identity", return_value=True),
            mock.patch.object(compat, "wait_process", return_value=0),
            mock.patch.object(compat, "close_process_group"),
            mock.patch.object(
                parallel_child,
                "_fence_posix_adopted_descendants",
                return_value=False,
            ) as descendant_fence,
            mock.patch.object(
                parallel_child, "_install_signal_handlers", return_value={}),
            mock.patch.object(parallel_child, "_restore_signal_handlers"),
            mock.patch.object(
                parallel_child.threading, "Thread", return_value=thread),
            contextlib.redirect_stderr(stderr),
        ):
            returncode = parallel_child.run_guardian(
                payload,
                run_dir=self.run_dir,
                task=1,
                child_id=child_id,
                control_stream=io.BytesIO(parallel_child.ACK_BYTE),
                launch_authorizer=(
                    lambda _run_dir, _record, *, payload_pid: None),
            )

        self.assertEqual(returncode, parallel_child.GUARDIAN_PROTOCOL_RC)
        self.assertIn("descendant reap proof failed", stderr.getvalue())
        descendant_fence.assert_called_once_with()
        record = parallel_child.read_child_record(
            self.run_dir, 1, child_id)
        self.assertEqual(record["state"], "acked")
        self.assertIsNone(record["returncode"])

    def test_invalid_ack_never_spawns_payload(self):
        marker = self.run_dir / "wrong-ack.txt"
        payload = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path(__import__('sys').argv[1]).touch()",
            str(marker),
        ]
        process, child_id, ready = self._spawn(payload)
        process.stdin.write(b"x")
        process.stdin.flush()
        self._close_control(process)
        returncode, _stdout, stderr = self._finish(process)
        self.assertEqual(returncode, parallel_child.GUARDIAN_PROTOCOL_RC)
        self.assertIn("invalid ACK", stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(
            parallel_child.read_child_record(self.run_dir, 1, child_id), ready)

    def test_record_hash_mismatch_blocks_spawn_and_acked_publication(self):
        marker = self.run_dir / "hash-mismatch.txt"
        payload = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path(__import__('sys').argv[1]).touch()",
            str(marker),
        ]
        process, child_id, ready = self._spawn(payload, recorded_hash="f" * 64)
        process.stdin.write(parallel_child.ACK_BYTE)
        process.stdin.flush()
        returncode, _stdout, stderr = self._finish(process)
        self.assertEqual(returncode, parallel_child.GUARDIAN_PROTOCOL_RC)
        self.assertIn("durable ACK validation failed", stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(
            parallel_child.read_child_record(self.run_dir, 1, child_id), ready)

    def test_record_pid_mismatch_blocks_spawn_and_acked_publication(self):
        marker = self.run_dir / "pid-mismatch.txt"
        payload = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path(__import__('sys').argv[1]).touch()",
            str(marker),
        ]
        process, child_id, ready = self._spawn(payload, recorded_pid=2)
        process.stdin.write(parallel_child.ACK_BYTE)
        process.stdin.flush()
        returncode, _stdout, _stderr = self._finish(process)
        self.assertEqual(returncode, parallel_child.GUARDIAN_PROTOCOL_RC)
        self.assertFalse(marker.exists())
        self.assertEqual(
            parallel_child.read_child_record(self.run_dir, 1, child_id), ready)

    def test_eof_after_ack_kills_payload_group_and_publishes_reaped(self):
        payload_pid_file = self.run_dir / "payload.pid"
        descendant_pid_file = self.run_dir / "descendant.pid"
        descendant_code = (
            "from pathlib import Path; import os,sys,time; "
            "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
        )
        payload_code = (
            "from pathlib import Path; import os,subprocess,sys,time; "
            "Path(sys.argv[1]).write_text(str(os.getpid())); "
            "kwargs = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} "
            "if os.name == 'nt' else {'start_new_session': True}); "
            "subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]], "
            "**kwargs); "
            "time.sleep(60)"
        )
        payload = [
            sys.executable,
            "-c",
            payload_code,
            str(payload_pid_file),
            str(descendant_pid_file),
            descendant_code,
        ]
        process, child_id, _ready = self._spawn(payload)
        process.stdin.write(parallel_child.ACK_BYTE)
        process.stdin.flush()
        self.assertTrue(_wait_for(payload_pid_file.exists))
        self.assertTrue(_wait_for(descendant_pid_file.exists))
        self.assertEqual(
            parallel_child.read_child_record(self.run_dir, 1, child_id)["state"],
            "acked",
        )
        acked = parallel_child.read_child_record(self.run_dir, 1, child_id)
        if not compat.IS_WINDOWS:
            # POSIX exec preserves the bootstrap PID as the real loop payload.
            self.assertEqual(
                acked["payload_pid"], int(payload_pid_file.read_text()))
        self.assertIsInstance(acked["payload_start_token"], str)
        self.assertGreater(acked["payload_group_id"], 1)
        payload_pid = int(payload_pid_file.read_text())
        descendant_pid = int(descendant_pid_file.read_text())

        self._close_control(process)
        returncode, _stdout, _stderr = self._finish(process)
        self.assertEqual(returncode, parallel_child.GUARDIAN_CANCELLED_RC)
        record = parallel_child.read_child_record(self.run_dir, 1, child_id)
        self.assertEqual(record["state"], "reaped")
        self.assertEqual(
            record["returncode"], parallel_child.GUARDIAN_CANCELLED_RC)
        self.assertTrue(
            _wait_for(lambda: not compat.process_is_alive(payload_pid)),
            f"payload pid {payload_pid} survived guardian EOF",
        )
        self.assertTrue(
            _wait_for(lambda: not compat.process_is_alive(descendant_pid)),
            f"descendant pid {descendant_pid} survived guardian EOF",
        )

    def test_hard_killed_guardian_stays_acked_until_exact_recovery_fences_tree(self):
        payload_pid_file = self.run_dir / "hard-payload.pid"
        descendant_pid_file = self.run_dir / "hard-descendant.pid"
        descendant_code = (
            "from pathlib import Path; import os,sys,time; "
            "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
        )
        payload_code = (
            "from pathlib import Path; import os,subprocess,sys,time; "
            "Path(sys.argv[1]).write_text(str(os.getpid())); "
            "kwargs = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} "
            "if os.name == 'nt' else {'start_new_session': True}); "
            "subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]], "
            "**kwargs); time.sleep(60)"
        )
        payload = [
            sys.executable, "-c", payload_code,
            str(payload_pid_file), str(descendant_pid_file), descendant_code,
        ]
        guardian, child_id, _ready = self._spawn(payload)
        guardian.stdin.write(parallel_child.ACK_BYTE)
        guardian.stdin.flush()
        self.assertTrue(_wait_for(payload_pid_file.exists))
        self.assertTrue(_wait_for(descendant_pid_file.exists))
        acked = parallel_child.read_child_record(self.run_dir, 1, child_id)
        self.assertEqual(acked["state"], "acked")
        if not compat.IS_WINDOWS:
            self.assertEqual(
                acked["payload_pid"], int(payload_pid_file.read_text()))
        payload_pid = int(payload_pid_file.read_text())
        descendant_pid = int(descendant_pid_file.read_text())

        # A raw OS kill intentionally bypasses guardian cleanup/publication.
        guardian.kill()
        compat.wait_process(guardian, timeout=5)
        self.assertEqual(
            parallel_child.read_child_record(self.run_dir, 1, child_id), acked)

        recovered = parallel_child.recover_acked_child(
            self.run_dir,
            1,
            child_id,
            expected_record=acked,
        )
        self.assertEqual(recovered["state"], "reaped")
        self.assertEqual(
            recovered["returncode"], parallel_child.GUARDIAN_CANCELLED_RC)
        self.assertTrue(_wait_for(lambda: not compat.process_is_alive(payload_pid)))
        self.assertTrue(
            _wait_for(lambda: not compat.process_is_alive(descendant_pid)))

    def test_recovery_rejects_live_guardian_and_changed_compare_record(self):
        marker = self.run_dir / "live-recovery.pid"
        payload = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import os,sys,time; "
                "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
            ),
            str(marker),
        ]
        guardian, child_id, _ready = self._spawn(payload)
        guardian.stdin.write(parallel_child.ACK_BYTE)
        guardian.stdin.flush()
        self.assertTrue(_wait_for(marker.exists))
        acked = parallel_child.read_child_record(self.run_dir, 1, child_id)
        with self.assertRaisesRegex(
                parallel_child.ParallelChildError, "guardian is still alive"):
            parallel_child.recover_acked_child(
                self.run_dir, 1, child_id, expected_record=acked)
        changed = dict(acked, argv_hash="f" * 64)
        with self.assertRaisesRegex(
                parallel_child.ParallelChildError, "durable record changed"):
            parallel_child.recover_acked_child(
                self.run_dir, 1, child_id, expected_record=changed)
        self.assertEqual(
            parallel_child.read_child_record(self.run_dir, 1, child_id), acked)

        self._close_control(guardian)
        returncode, _stdout, _stderr = self._finish(guardian)
        self.assertEqual(returncode, parallel_child.GUARDIAN_CANCELLED_RC)

    def test_parent_process_hard_exit_leaves_guardian_to_fence_and_reap(self):
        child_id = "e" * 32
        payload_pid_file = self.run_dir / "parent-exit-payload.pid"
        descendant_pid_file = self.run_dir / "parent-exit-descendant.pid"
        guardian_pid_file = self.run_dir / "parent-exit-guardian.pid"
        supervisor_pid_file = self.run_dir / "parent-exit-supervisor.pid"
        descendant_code = (
            "from pathlib import Path; import os,sys,time; "
            "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
        )
        payload_code = (
            "from pathlib import Path; import os,subprocess,sys,time; "
            "Path(sys.argv[1]).write_text(str(os.getpid())); "
            "kwargs = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} "
            "if os.name == 'nt' else {'start_new_session': True}); "
            "subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]], "
            "**kwargs); time.sleep(60)"
        )
        payload = [
            sys.executable, "-c", payload_code,
            str(payload_pid_file), str(descendant_pid_file), descendant_code,
        ]
        parent_code = (
            "import json,os,subprocess,sys,time; from pathlib import Path; "
            "from engine import parallel_child as pc; "
            "from engine import platform_compat as c; "
            "run=Path(sys.argv[1]); child=sys.argv[2]; payload=json.loads(sys.argv[3]); "
            "argv=pc.build_guardian_argv(sys.executable,run,1,child,payload); "
            "argv[2]='tests.parallel_child_harness'; "
            "p=subprocess.Popen(argv,cwd=sys.argv[5],env=dict(os.environ),"
            "stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
            "**c.popen_group_kwargs()); "
            "assert c.attach_process_group(p) is True; "
            "ready=pc.child_record(run_id='a1b2c3d4',task=1,child_id=child,"
            "supervisor_session='a'*32,supervisor_generation=1,attempt=0,resume=False,"
            "guardian_pid=p.pid,argv_hash=pc.payload_argv_hash(payload),"
            "state='guardian_ready'); pc.write_child_record(run,ready); "
            "p.stdin.write(pc.ACK_BYTE); p.stdin.flush(); "
            "Path(sys.argv[4]).write_text(str(p.pid)); time.sleep(60)"
        )
        parent_command = [
            sys.executable, "-c", parent_code, str(self.run_dir), child_id,
            json.dumps(payload), str(guardian_pid_file), str(ROOT),
        ]
        if compat.IS_WINDOWS:
            # Model Dashboard -> supervisor -> guardian.  The launcher owns a
            # kill-on-close Job for the supervisor.  Hard-killing the launcher
            # closes that outer Job; only the guardian's explicit breakaway
            # lets it survive long enough to consume pipe EOF and reap.
            launcher_code = (
                "import json,os,subprocess,sys,time; from pathlib import Path; "
                "from engine import platform_compat as c; cmd=json.loads(sys.argv[1]); "
                "p=subprocess.Popen(cmd,cwd=sys.argv[3],env=dict(os.environ),"
                "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
                "stderr=subprocess.DEVNULL,**c.popen_group_kwargs()); "
                "assert c.attach_process_group(p) is True; "
                "Path(sys.argv[2]).write_text(str(p.pid)); time.sleep(60)"
            )
            owner = subprocess.Popen(
                [sys.executable, "-c", launcher_code,
                 json.dumps(parent_command), str(supervisor_pid_file), str(ROOT)],
                cwd=str(ROOT),
                env={**os.environ, "PYTHONUTF8": "1"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **compat.popen_group_kwargs(),
            )
            self.processes.append(owner)
            self.assertTrue(_wait_for(supervisor_pid_file.exists))
            supervisor_pid = int(supervisor_pid_file.read_text())
        else:
            owner = subprocess.Popen(
                parent_command,
                cwd=str(ROOT),
                env={**os.environ, "PYTHONUTF8": "1"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                **compat.popen_group_kwargs(),
            )
            self.processes.append(owner)
            self.assertIs(compat.attach_process_group(owner), True)
            supervisor_pid = owner.pid
        self.assertTrue(_wait_for(guardian_pid_file.exists))
        self.assertTrue(_wait_for(payload_pid_file.exists))
        self.assertTrue(_wait_for(descendant_pid_file.exists))
        guardian_pid = int(guardian_pid_file.read_text())
        payload_pid = int(payload_pid_file.read_text())
        descendant_pid = int(descendant_pid_file.read_text())
        self.assertEqual(
            parallel_child.read_child_record(
                self.run_dir, 1, child_id)["state"], "acked")

        # Kill only the supervisor process.  The guardian must survive its
        # supervisor Job (Windows breakaway) long enough to observe pipe EOF.
        owner.kill()
        compat.wait_process(owner, timeout=5)
        self.assertTrue(
            _wait_for(lambda: not compat.process_is_alive(supervisor_pid)))
        self.assertTrue(_wait_for(
            lambda: parallel_child.read_child_record(
                self.run_dir, 1, child_id)["state"] == "reaped",
            timeout=12,
        ))
        self.assertTrue(_wait_for(lambda: not compat.process_is_alive(guardian_pid)))
        self.assertTrue(_wait_for(lambda: not compat.process_is_alive(payload_pid)))
        self.assertTrue(
            _wait_for(lambda: not compat.process_is_alive(descendant_pid)))

    def test_recovery_is_fail_closed_when_posix_root_identity_is_gone(self):
        ready = parallel_child.child_record(
            run_id=RUN_ID,
            task=1,
            child_id="d" * 32,
            supervisor_session=SESSION,
            supervisor_generation=1,
            attempt=0,
            resume=False,
            guardian_pid=2222,
            guardian_start_token="test:guardian",
            argv_hash=parallel_child.payload_argv_hash(
                [sys.executable, "-c", "pass"]),
            state="guardian_ready",
        )
        parallel_child.write_child_record(self.run_dir, ready)
        acked = dict(
            ready,
            state="acked",
            payload_pid=3333,
            payload_start_token="test:payload",
            payload_group_id=3333,
            payload_containment="posix-exact-tree-v1",
        )
        parallel_child.write_child_record(self.run_dir, acked)
        with mock.patch.object(compat, "IS_WINDOWS", False), \
                mock.patch.object(compat, "process_matches_identity", return_value=False):
            with self.assertRaisesRegex(
                    parallel_child.ParallelChildError,
                    "root disappeared"):
                parallel_child.recover_acked_child(
                    self.run_dir, 1, "d" * 32, expected_record=acked)
        self.assertEqual(
            parallel_child.read_child_record(self.run_dir, 1, "d" * 32), acked)

    def test_recovery_reads_but_never_trusts_legacy_windows_job(self):
        child_id = "c" * 32
        ready = parallel_child.child_record(
            run_id=RUN_ID,
            task=1,
            child_id=child_id,
            supervisor_session=SESSION,
            supervisor_generation=1,
            attempt=0,
            resume=False,
            guardian_pid=2222,
            guardian_start_token="test:guardian",
            argv_hash=parallel_child.payload_argv_hash(
                [sys.executable, "-c", "pass"]),
            state="guardian_ready",
        )
        parallel_child.write_child_record(self.run_dir, ready)
        legacy = dict(
            ready,
            state="acked",
            payload_pid=3333,
            payload_start_token="test:payload",
            payload_group_id=3333,
            payload_containment=(
                parallel_child.WINDOWS_LEGACY_PAYLOAD_CONTAINMENT),
        )
        parallel_child.write_child_record(self.run_dir, legacy)
        self.assertEqual(parallel_child.validate_child_record(legacy), legacy)

        with mock.patch.object(compat, "IS_WINDOWS", True), \
                mock.patch.object(compat, "process_matches_identity",
                                  return_value=False), \
                mock.patch.object(compat, "fence_process_tree") as fence:
            with self.assertRaisesRegex(
                    parallel_child.ParallelChildError,
                    "legacy Windows payload containment"):
                parallel_child.recover_acked_child(
                    self.run_dir, 1, child_id, expected_record=legacy)
        fence.assert_not_called()
        self.assertEqual(
            parallel_child.read_child_record(self.run_dir, 1, child_id), legacy)


if __name__ == "__main__":
    unittest.main()
