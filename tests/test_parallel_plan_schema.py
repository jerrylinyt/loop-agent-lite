"""Parallel plan schema 與 managed-worker coordinator signal 的聚焦回歸測試。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine import loop as loop_mod
from engine import work


WORK_CMD = [sys.executable, "-m", "engine.work"]


class TestParallelPlanSchema(unittest.TestCase):
    def test_legacy_plan_normalization_is_unchanged(self):
        plan = [
            {"order": 2, "task": " second "},
            {"order": 1, "task": " first ", "ref": "PLAN.md#first"},
        ]

        normalized, errors = work.validate_plan(plan)

        self.assertEqual(errors, [])
        self.assertEqual(normalized, [
            {"order": 1, "task": "first", "ref": "PLAN.md#first"},
            {"order": 2, "task": "second", "ref": None},
        ])
        self.assertFalse(work.plan_has_stack(normalized))

    def test_valid_stack_is_retained_and_checked_in_task_order(self):
        plan = [
            {"order": 4, "task": "solo"},
            {"order": 2, "task": "parallel-b", "stack": 7},
            {"order": 1, "task": "parallel-a", "stack": 7},
            {"order": 3, "task": "another batch", "stack": 9},
        ]

        normalized, errors = work.validate_plan(plan)

        self.assertEqual(errors, [])
        self.assertEqual([task.get("stack") for task in normalized], [7, 7, 9, None])
        self.assertNotIn("stack", normalized[-1])
        self.assertTrue(work.plan_has_stack(normalized))

    def test_stack_rejects_bool_non_integer_and_non_positive_values(self):
        invalid_values = (True, False, 0, -1, 1.5, "1", None)
        for value in invalid_values:
            with self.subTest(value=value):
                normalized, errors = work.validate_plan(
                    [{"order": 1, "task": "one", "stack": value}]
                )
                self.assertIsNone(normalized)
                self.assertTrue(any("stack 必須是正整數" in error for error in errors))

    def test_same_stack_cannot_reappear_after_another_batch(self):
        for middle in ({"order": 2, "task": "solo"},
                       {"order": 2, "task": "other", "stack": 2}):
            with self.subTest(middle=middle):
                normalized, errors = work.validate_plan([
                    {"order": 3, "task": "last", "stack": 1},
                    {"order": 1, "task": "first", "stack": 1},
                    middle,
                ])
                self.assertIsNone(normalized)
                self.assertIn("stack 1 必須只出現在一段連續 task", errors)

    def test_serial_stack_helper_requires_literal_true_opt_in(self):
        plan = [{"order": 1, "task": "one", "stack": 3}]

        self.assertFalse(work.validate_serial_stack_opt_in(plan, allow_serial_stack=True))
        for opt_in in (False, None, 1, "true"):
            with self.subTest(opt_in=opt_in):
                errors = work.validate_serial_stack_opt_in(plan, allow_serial_stack=opt_in)
                self.assertEqual(len(errors), 1)
                self.assertIn("--allow-serial-stack", errors[0])
        self.assertEqual(
            work.validate_serial_stack_opt_in([{"order": 1, "task": "one"}]),
            [],
        )


class TestManagedWorkerBlockCommand(unittest.TestCase):
    def _workspace(self, root, dispatch):
        workspace_root = root / "workspaces"
        workspace = workspace_root / "managed"
        workspace.mkdir(parents=True)
        (workspace / "dispatch.json").write_text(
            json.dumps(dispatch), encoding="utf-8"
        )
        return workspace_root, workspace

    def _run_work(self, workspace_root, workspace, token, *args):
        env = {
            **os.environ,
            "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root),
            "LOOP_WS": str(workspace),
            "LOOP_ROUND_TOKEN": token,
        }
        return subprocess.run(
            [*WORK_CMD, *args], capture_output=True, text=True, env=env
        )

    def test_block_writes_one_token_and_task_bound_atomic_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = "round-token"
            workspace_root, workspace = self._workspace(root, {
                "phase": "exec",
                "task_id": "task-3",
                "round_token": token,
                "runner": "parallel-worker",
            })

            result = self._run_work(
                workspace_root, workspace, token,
                "block", "--reason", "human", "gate", "required",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(
                (workspace / f"pending_block.{token}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload, {
                "schema_version": 1,
                "round_token": token,
                "task_id": "task-3",
                "reason": "human gate required",
            })
            self.assertIn("blocked terminal", result.stdout)

    def test_block_rejects_ordinary_or_non_exec_dispatch(self):
        cases = (
            {"phase": "exec", "task_id": "task-1", "round_token": "tok"},
            {"phase": "plan", "task_id": "task-1", "round_token": "tok",
             "runner": "parallel-worker"},
        )
        for dispatch in cases:
            with self.subTest(dispatch=dispatch), tempfile.TemporaryDirectory() as directory:
                workspace_root, workspace = self._workspace(Path(directory), dispatch)
                result = self._run_work(
                    workspace_root, workspace, "tok", "block", "--reason", "stop"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((workspace / "pending_block.tok.json").exists())

    def test_block_rejects_invalid_or_oversized_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            token = "tok"
            workspace_root, workspace = self._workspace(Path(directory), {
                "phase": "exec",
                "task_id": "task-1",
                "round_token": token,
                "runner": "parallel-worker",
            })
            for args in (("block",), ("block", "reason"), ("block", "--reason"),
                         ("block", "--reason", "x" * (loop_mod.ISSUE_MAX_CHARS + 1))):
                with self.subTest(args=args):
                    result = self._run_work(workspace_root, workspace, token, *args)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((workspace / f"pending_block.{token}.json").exists())

    def test_block_rejects_symlink_artifact_without_writing_outside(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = "tok"
            workspace_root, workspace = self._workspace(root, {
                "phase": "exec",
                "task_id": "task-1",
                "round_token": token,
                "runner": "parallel-worker",
            })
            outside = root / "outside.json"
            outside.write_text('{"safe": true}\n', encoding="utf-8")
            (workspace / f"pending_block.{token}.json").symlink_to(outside)

            result = self._run_work(
                workspace_root, workspace, token, "block", "--reason", "must not escape"
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("協調檔案不安全", result.stderr)
            self.assertEqual(outside.read_text(encoding="utf-8"), '{"safe": true}\n')


class TestCreatePlanStackMode(unittest.TestCase):
    def _run_create(self, root: Path, dispatch: dict, plan: list):
        workspace_root = root / "workspaces"
        workspace = workspace_root / "planner"
        workspace.mkdir(parents=True)
        (workspace / "dispatch.json").write_text(json.dumps(dispatch), encoding="utf-8")
        env = {
            **os.environ,
            "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root),
            "LOOP_WS": str(workspace),
            "LOOP_ROUND_TOKEN": dispatch["round_token"],
        }
        return workspace, subprocess.run(
            [*WORK_CMD, "create-plan"],
            input=json.dumps(plan), capture_output=True, text=True, env=env,
        )

    def test_planning_create_plan_rejects_stack_even_with_serial_opt_in(self):
        plan = [{"order": 1, "task": "one", "stack": 1}]
        base = {"phase": "plan", "task_id": "", "round_token": "tok"}
        for dispatch in (base, {**base, "allow_serial_stack": True}):
            with self.subTest(dispatch=dispatch), tempfile.TemporaryDirectory() as directory:
                workspace, result = self._run_create(Path(directory), dispatch, plan)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("frozen plan", result.stderr)
                self.assertFalse((workspace / "pending_plan.tok.json").exists())


class TestPlanTransitionStackFence(unittest.TestCase):
    class Workspace:
        def __init__(self, signals=(), pending=None):
            self.signals = set(signals)
            self.pending = pending

        def signal(self, name, _token):
            return name in self.signals

        def take_pending_plan(self, _token):
            return self.pending

    @staticmethod
    def state():
        return {
            "phase": "plan", "flag": 0, "plan_version": 1,
            "plan": [{"order": 1, "task": "manual", "ref": None, "stack": 4}],
            "current_order": 0, "done_count": 0, "stall_rounds": 0,
            "red_streak": 0, "goal_changed": False, "notes": [],
        }

    def test_plan_to_exec_rejects_stack_even_with_serial_opt_in(self):
        state = self.state()
        event = loop_mod.process_plan_round(
            state, self.Workspace(signals={"signal_plan_ok"}), "tok",
            tampered=False, changed=False, agent_failed=False,
            completion_missing=False, flag_threshold=0, allow_serial_stack=True,
        )
        self.assertEqual(state["phase"], "plan")
        self.assertEqual(state["flag"], 0)
        self.assertIn("plan→exec 已拒絕", event)

    def test_ingest_revalidates_direct_stack_artifact_and_preserves_plan(self):
        state = self.state()
        pending = [{"order": 1, "task": "rewritten", "stack": 8}]
        event = loop_mod.process_plan_round(
            state, self.Workspace(signals={"called_create_plan"}, pending=pending), "tok",
            tampered=False, changed=False, agent_failed=False,
            completion_missing=False, flag_threshold=10, allow_serial_stack=True,
        )
        self.assertEqual(state["plan"][0]["task"], "manual")
        self.assertIn("ingest 已拒絕", event)


if __name__ == "__main__":
    unittest.main()
