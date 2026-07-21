"""Managed worker completion gate state transitions and exact-SHA fencing."""

import copy
import json
import subprocess
import tempfile
import threading
import time
import sys
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

from engine import loop
from engine import platform_compat as compat


RUN_ID = "a1b2c3d4"
SHA = "a" * 40


class RecordingWorkspace:
    def __init__(self):
        self.saved = []

    def save_state(self, state):
        self.saved.append(copy.deepcopy(state))


class ResetWorkspace(RecordingWorkspace):
    @staticmethod
    def signal(_name, _token):
        return False

    @staticmethod
    def restore_protected(_repo, _protected):
        return None


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True)


class TestManagedCompletionGate(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Gate Test")
        _git(self.repo, "config", "user.email", "gate@example.invalid")
        (self.repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-qm", "initial")
        self.sha = loop.head_sha(self.repo)
        self.task_ref = f"refs/heads/loop/{RUN_ID}/task-2"
        _git(self.repo, "update-ref", self.task_ref, self.sha)
        _git(self.repo, "symbolic-ref", "HEAD", self.task_ref)

    def state(self, *, done_count=3):
        return {
            "phase": "exec",
            "runner": "parallel-worker",
            "run_id": RUN_ID,
            "assigned_order": 2,
            "task_ref": self.task_ref,
            "complete_gate_cmd": "gate-client --spool fixed",
            "run_config_hash": "1" * 64,
            "launch_spec_hash": "2" * 64,
            "manifest_hash": "3" * 64,
            "done_count": done_count,
            "notes": [],
            "assignment": {
                "status": "running",
                "validated_sha": None,
                "validated_round": None,
                "exit_reason": None,
                "pause_generation": 0,
                "gate_request": None,
            },
        }

    @staticmethod
    def gate_result(returncode, status, *, reason=None, timed_out=False):
        def run(_cmd, _repo, env, _timeout):
            payload = {
                "status": status,
                "run_id": env["RUN_ID"],
                "task": int(env["TASK"]),
                "request_id": env["REQUEST_ID"],
                "validated_sha": env["VALIDATED_SHA"],
            }
            if reason is not None:
                payload["reason"] = reason
            return returncode, json.dumps(payload), "", timed_out
        return run

    def apply(self, state, side_effect):
        workspace = RecordingWorkspace()
        with mock.patch.object(loop, "run_completion_gate", side_effect=side_effect):
            event = loop.apply_managed_completion_gate(
                state, self.repo, workspace, round_number=7, validated_sha=self.sha,
                timeout_seconds=12,
            )
        self.assertEqual(len(workspace.saved), 1)
        durable = workspace.saved[0]["assignment"]
        self.assertEqual(durable["status"], "running")
        self.assertEqual(durable["validated_sha"], self.sha)
        self.assertEqual(durable["validated_round"], 7)
        self.assertRegex(durable["gate_request"]["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(durable["gate_request"]["validated_sha"], self.sha)
        self.assertEqual(durable["gate_request"]["validated_round"], 7)
        return event

    def test_merged_and_already_merged_become_integrated_without_global_done(self):
        for status in ("merged", "already-merged"):
            with self.subTest(status=status):
                state = self.state()
                event = self.apply(state, self.gate_result(0, status))
                self.assertIn("已由 supervisor gate 整合", event)
                self.assertEqual(state["phase"], "exec")
                self.assertEqual(state["assignment"]["status"], "integrated")
                self.assertEqual(state["assignment"]["validated_sha"], self.sha)
                self.assertEqual(state["assignment"]["validated_round"], 7)
                self.assertIsNone(state["assignment"]["gate_request"])
                self.assertEqual(state["done_count"], 0)

    def test_stale_resets_consensus_while_busy_preserves_it(self):
        stale = self.state()
        self.apply(stale, self.gate_result(10, "stale-integration"))
        self.assertEqual(stale["assignment"]["status"], "running")
        self.assertIsNone(stale["assignment"]["gate_request"])
        self.assertEqual(stale["done_count"], 0)
        self.assertTrue(any("integration 已前進" in note for note in stale["notes"]))

        busy = self.state(done_count=4)
        self.apply(busy, self.gate_result(11, "busy"))
        self.assertEqual(busy["assignment"]["status"], "running")
        self.assertIsNone(busy["assignment"]["gate_request"])
        self.assertEqual(busy["done_count"], 4)

    def test_terminal_gate_statuses_are_structured(self):
        cases = (
            (20, "paused", "paused"),
            (21, "cancelled", "cancelled"),
            (30, "fatal-invariant", "blocked"),
            (31, "recovery-required-after-claim", "recovery-required"),
        )
        for returncode, status, expected in cases:
            with self.subTest(status=status):
                state = self.state()
                self.apply(state, self.gate_result(returncode, status, reason="reason"))
                self.assertEqual(state["assignment"]["status"], expected)
                self.assertEqual(state["assignment"]["exit_reason"], "reason")
                self.assertEqual(state["done_count"], 0)

    def test_timeout_is_recovery_required_not_retryable_busy(self):
        state = self.state()
        self.apply(state, self.gate_result(11, "busy", timed_out=True))
        self.assertEqual(state["assignment"]["status"], "recovery-required")
        self.assertIn("claim 狀態未知", state["assignment"]["exit_reason"])
        self.assertIsNotNone(state["assignment"]["gate_request"])

    def test_malformed_or_mismatched_response_requires_recovery(self):
        state = self.state()
        self.apply(state, lambda *_args: (0, '{"status":"merged"}', "", False))
        self.assertEqual(state["assignment"]["status"], "recovery-required")
        self.assertIn("gate protocol fatal", state["assignment"]["exit_reason"])
        self.assertIsNotNone(state["assignment"]["gate_request"])

    def test_gate_client_git_mutation_is_blocked_even_with_success_json(self):
        state = self.state()

        def mutate_then_success(cmd, repo, env, timeout):
            del cmd, timeout
            (repo / "gate-dirty.txt").write_text("forbidden\n", encoding="utf-8")
            return self.gate_result(0, "merged")([], repo, env, 0)

        self.apply(state, mutate_then_success)
        self.assertEqual(state["assignment"]["status"], "recovery-required")
        self.assertIn("唯讀契約", state["assignment"]["exit_reason"])
        self.assertIsNotNone(state["assignment"]["gate_request"])

    def test_gate_client_cannot_switch_to_same_sha_sibling_branch(self):
        state = self.state()
        sibling = "refs/heads/sibling"
        _git(self.repo, "update-ref", sibling, self.sha)

        def switch_then_success(cmd, repo, env, timeout):
            del cmd, timeout
            _git(repo, "symbolic-ref", "HEAD", sibling)
            return self.gate_result(0, "merged")([], repo, env, 0)

        self.apply(state, switch_then_success)
        self.assertEqual(state["assignment"]["status"], "recovery-required")
        self.assertIn("唯讀契約", state["assignment"]["exit_reason"])

    def test_timeout_wins_over_repo_mutation_and_keeps_request_for_reconcile(self):
        state = self.state()

        def mutate_then_timeout(cmd, repo, env, timeout):
            del cmd, timeout
            (repo / "gate-dirty.txt").write_text("forbidden\n", encoding="utf-8")
            return self.gate_result(11, "busy", timed_out=True)([], repo, env, 0)

        event = self.apply(state, mutate_then_timeout)
        self.assertIn("recovery-required", event)
        self.assertEqual(state["assignment"]["status"], "recovery-required")
        self.assertIn("唯讀契約", state["assignment"]["exit_reason"])
        self.assertIsNotNone(state["assignment"]["gate_request"])

    def test_gate_requires_current_clean_exact_validated_sha_before_persist_or_spawn(self):
        state = self.state()
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        workspace = RecordingWorkspace()
        with mock.patch.object(loop, "run_completion_gate") as gate:
            event = loop.apply_managed_completion_gate(
                state, self.repo, workspace, round_number=7, validated_sha=self.sha,
                timeout_seconds=12,
            )
        gate.assert_not_called()
        self.assertEqual(workspace.saved, [])
        self.assertIn("exact SHA", event)
        self.assertEqual(state["assignment"]["status"], "blocked")

    def test_managed_block_signal_is_terminal_without_running_validator(self):
        state = self.state()
        state.update({
            "current_order": 2,
            "completed": [],
            "red_streak": 0,
            "stall_rounds": 0,
            "last_green_sha": self.sha,
            "task_reset_counts": {},
        })
        snapshot = loop.repository_snapshot(self.repo)
        with mock.patch.object(loop, "run_validate") as validate:
            event, note, after, rejected = loop.process_exec_round(
                state, object(), "round-token",
                task_id="task-2", round_number=1, repo=self.repo,
                protected=(), validate_cmd=["validator"], args=object(),
                head_before=self.sha, pre_validate_snapshot=snapshot,
                tampered=[], changed=False,
                managed_block_reason="human gate required",
                agent_failed=False, completion_missing=False,
            )
        validate.assert_not_called()
        self.assertIn("blocked", event)
        self.assertEqual(note, "BLOCKED")
        self.assertEqual(after, snapshot)
        self.assertFalse(rejected)
        self.assertEqual(state["assignment"]["status"], "blocked")
        self.assertEqual(state["assignment"]["exit_reason"], "human gate required")

    def test_worker_wrong_branch_blocks_before_validator(self):
        sibling = "refs/heads/sibling"
        _git(self.repo, "update-ref", sibling, self.sha)
        _git(self.repo, "symbolic-ref", "HEAD", sibling)
        state = self.state()
        state.update({
            "current_order": 2,
            "completed": [],
            "red_streak": 0,
            "stall_rounds": 0,
            "last_green_sha": self.sha,
            "task_reset_counts": {},
        })
        snapshot = loop.repository_snapshot(self.repo)

        with mock.patch.object(loop, "run_validate") as validate:
            event, note, _after, rejected = loop.process_exec_round(
                state, object(), "round-token",
                task_id="task-2", round_number=1, repo=self.repo,
                protected=(), validate_cmd=["validator"], args=object(),
                head_before=self.sha, pre_validate_snapshot=snapshot,
                tampered=[], changed=False, managed_block_reason=None,
                agent_failed=False, completion_missing=False,
            )

        validate.assert_not_called()
        self.assertIn("blocked", event)
        self.assertEqual(note, "BLOCKED")
        self.assertTrue(rejected)
        self.assertEqual(state["assignment"]["status"], "blocked")

    def test_managed_red_reset_returns_to_assigned_order_and_task_ref(self):
        state = self.state()
        state.update({
            "current_order": 2,
            "plan": [
                {"order": 1, "task": "other", "ref": None},
                {"order": 2, "task": "assigned", "ref": None},
            ],
            "completed": [{"order": 1, "sha": self.sha, "round": 0}],
            "red_streak": 0,
            "stall_rounds": 0,
            "last_green_sha": self.sha,
            "current_task_base_sha": self.sha,
            "task_reset_counts": {},
        })
        args = SimpleNamespace(
            validate_timeout=10, done_threshold=3,
            red_limit=1, stall_limit=999,
            stuck_stop=False, stuck_stop_count=999, notify_cmd=None,
        )
        snapshot = loop.repository_snapshot(self.repo)

        with mock.patch.object(loop, "run_validate", return_value=(False, "red", False)):
            event, note, after, _rejected = loop.process_exec_round(
                state, ResetWorkspace(), "round-token",
                task_id="task-2", round_number=1, repo=self.repo,
                protected=(), validate_cmd=["validator"], args=args,
                head_before=self.sha, pre_validate_snapshot=snapshot,
                tampered=[], changed=False, managed_block_reason=None,
                agent_failed=False, completion_missing=True,
            )

        self.assertIn("RESET", event)
        self.assertEqual(note, "FAIL")
        self.assertEqual(state["phase"], "exec")
        self.assertEqual(state["current_order"], 2)
        self.assertEqual(state["completed"], [])
        self.assertEqual(state["task_reset_counts"], {"2": 1})
        self.assertEqual(after.head_ref, self.task_ref)
        self.assertEqual(loop.managed_task_ref_error(self.repo, self.task_ref), None)

    def test_gate_timeout_never_waits_forever_for_escaped_pipe_holder(self):
        release = threading.Event()

        class BlockingStream:
            def read(self):
                release.wait(timeout=10)
                return ""

        class TimedOutProcess:
            returncode = -9
            stdout = BlockingStream()
            stderr = BlockingStream()

            @staticmethod
            def communicate(timeout=None):
                raise subprocess.TimeoutExpired(["gate"], timeout)

            @staticmethod
            def wait(timeout=None):
                return -9

            @staticmethod
            def kill():
                return None

        started = time.monotonic()
        try:
            with mock.patch.object(loop.subprocess, "Popen", return_value=TimedOutProcess()), \
                    mock.patch.object(loop, "safe_killpg"), \
                    mock.patch.object(loop.compat, "attach_process_group"), \
                    mock.patch.object(loop.compat, "close_process_group"), \
                    mock.patch.object(loop, "GATE_DRAIN_TIMEOUT_SEC", 0.02):
                _rc, _stdout, stderr, timed_out = loop.run_completion_gate(
                    ["gate"], self.repo, {}, 0.01)
        finally:
            release.set()

        self.assertTrue(timed_out)
        self.assertIn("仍有程序持有", stderr)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_gate_decodes_non_ascii_json_reason_as_utf8(self):
        gate = Path(self.temp.name) / "unicode_gate.py"
        gate.write_text(
            """\
import json
import os

print(json.dumps({
    "status": "paused",
    "run_id": os.environ["RUN_ID"],
    "task": int(os.environ["TASK"]),
    "request_id": os.environ["REQUEST_ID"],
    "validated_sha": os.environ["VALIDATED_SHA"],
    "reason": "等待人工確認：整合衝突",
}, ensure_ascii=False))
raise SystemExit(20)
""",
            encoding="utf-8",
        )
        state = self.state()
        state["complete_gate_cmd"] = compat.join_command([sys.executable, str(gate)])
        workspace = RecordingWorkspace()

        event = loop.apply_managed_completion_gate(
            state, self.repo, workspace,
            round_number=7, validated_sha=self.sha, timeout_seconds=12,
        )

        self.assertIn("等待人工確認：整合衝突", event)
        self.assertEqual(state["assignment"]["status"], "paused")
        self.assertEqual(state["assignment"]["exit_reason"], "等待人工確認：整合衝突")

    def test_invalid_utf8_gate_output_becomes_recovery_not_uncaught_crash(self):
        gate = Path(self.temp.name) / "invalid_utf8_gate.py"
        gate.write_text(
            "import sys\nsys.stdout.buffer.write(b'\\xff')\n",
            encoding="utf-8",
        )
        state = self.state()
        state["complete_gate_cmd"] = compat.join_command([sys.executable, str(gate)])

        event = loop.apply_managed_completion_gate(
            state, self.repo, RecordingWorkspace(),
            round_number=7, validated_sha=self.sha, timeout_seconds=12,
        )

        self.assertIn("recovery-required", event)
        self.assertEqual(state["assignment"]["status"], "recovery-required")
        self.assertIn("UTF-8", state["assignment"]["exit_reason"])
        self.assertIsNotNone(state["assignment"]["gate_request"])


if __name__ == "__main__":
    unittest.main()
