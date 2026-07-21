#!/usr/bin/env python3
"""Validate 後 exact Git snapshot 的聚焦回歸測試。"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine import loop as loop_mod  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    )


class _DoneWorkspace:
    """只提供 process_exec_round 此測試路徑會讀取的 done 訊號。"""

    @staticmethod
    def signal(name: str, _round_token: str) -> bool:
        return name == "signal_done"


@unittest.skipUnless(shutil.which("git"), "需要 PATH 上有 git")
class TestPostValidatorSnapshot(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init")
        _git(self.repo, "config", "user.name", "Loop Test")
        _git(self.repo, "config", "user.email", "loop-test@example.invalid")
        (self.repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-m", "initial")
        self.initial_sha = loop_mod.head_sha(self.repo)

    def _state(self, *, done_count: int = 0) -> dict:
        return {
            "phase": "exec",
            "current_order": 1,
            "plan": [{"order": 1, "task": "test task", "ref": None}],
            "completed": [],
            "done_count": done_count,
            "red_streak": 0,
            "stall_rounds": 0,
            "last_green_sha": self.initial_sha,
            "current_task_base_sha": self.initial_sha,
            "notes": [],
            "task_reset_counts": {},
        }

    @staticmethod
    def _args(*, done_threshold: int = 1) -> SimpleNamespace:
        return SimpleNamespace(
            validate_timeout=10,
            done_threshold=done_threshold,
            red_limit=999,
            stall_limit=999,
            stuck_stop=False,
            stuck_stop_count=999,
            notify_cmd=None,
        )

    def _validator(self, name: str, source: str) -> list[str]:
        script = self.root / f"validator_{name}.py"
        script.write_text(source, encoding="utf-8")
        return [sys.executable, str(script)]

    def _run_round(
        self,
        state: dict,
        validate_cmd: list[str],
        *,
        done_threshold: int = 1,
        changed: bool = False,
    ):
        before = loop_mod.repository_snapshot(self.repo)
        result = loop_mod.process_exec_round(
            state,
            _DoneWorkspace(),
            "round-token",
            task_id="task-1",
            round_number=1,
            repo=self.repo,
            protected=(),
            validate_cmd=validate_cmd,
            args=self._args(done_threshold=done_threshold),
            head_before=before.head,
            pre_validate_snapshot=before,
            tampered=[],
            changed=changed,
            agent_failed=False,
            completion_missing=False,
        )
        return before, result

    def _assert_side_effect_rejected(self, state: dict, before, result):
        event, validate_note, after, rejected = result
        self.assertEqual(event, "")
        self.assertEqual(validate_note, "SIDE-EFFECT")
        self.assertTrue(rejected)
        self.assertEqual(after, loop_mod.repository_snapshot(self.repo))
        self.assertNotEqual(after, before)
        self.assertEqual(state["done_count"], 0)
        self.assertEqual(state["completed"], [])
        self.assertEqual(state["phase"], "exec")
        self.assertEqual(state["last_green_sha"], self.initial_sha)
        self.assertTrue(any("Validator side effect" in note for note in state["notes"]))

    def test_clean_validator_keeps_existing_done_behavior(self):
        state = self._state()
        command = self._validator("clean", "raise SystemExit(0)\n")

        before, result = self._run_round(state, command)

        event, validate_note, after, rejected = result
        self.assertIn("task-1 完成", event)
        self.assertEqual(validate_note, "PASS")
        self.assertFalse(rejected)
        self.assertEqual(after, before)
        self.assertFalse(after.dirty)
        self.assertEqual(state["phase"], "done")
        self.assertEqual(state["completed"][0]["sha"], self.initial_sha)
        self.assertEqual(state["last_green_sha"], self.initial_sha)

    def test_existing_dirty_snapshot_is_rejected_without_side_effect_label(self):
        (self.repo / "tracked.txt").write_text("agent dirty\n", encoding="utf-8")
        state = self._state()
        command = self._validator("dirty_unchanged", "raise SystemExit(0)\n")

        before, result = self._run_round(state, command, changed=True)

        event, validate_note, after, rejected = result
        self.assertEqual(event, "")
        self.assertEqual(validate_note, "DIRTY")
        self.assertTrue(rejected)
        self.assertEqual(after, before)
        self.assertTrue(after.dirty)
        self.assertEqual(state["done_count"], 0)
        self.assertEqual(state["completed"], [])
        self.assertFalse(any("Validator side effect" in note for note in state["notes"]))
        self.assertTrue(any("validator 前已存在" in note for note in state["notes"]))

    def test_validator_commit_cannot_supply_done_vote(self):
        state = self._state()
        command = self._validator(
            "commit",
            """\
import subprocess
from pathlib import Path

Path("tracked.txt").write_text("validator commit\\n", encoding="utf-8")
subprocess.run(["git", "add", "tracked.txt"], check=True)
subprocess.run(["git", "commit", "-m", "validator side effect"], check=True)
""",
        )

        before, result = self._run_round(state, command)

        self._assert_side_effect_rejected(state, before, result)
        after = result[2]
        self.assertNotEqual(after.head, before.head)
        self.assertFalse(after.dirty)
        self.assertEqual(state["stall_rounds"], 1)

    def test_validator_cannot_switch_to_same_sha_sibling_branch(self):
        sibling = "refs/heads/sibling"
        _git(self.repo, "update-ref", sibling, self.initial_sha)
        state = self._state()
        command = self._validator(
            "switch_branch",
            f'''\
import subprocess

subprocess.run(["git", "symbolic-ref", "HEAD", "{sibling}"], check=True)
''',
        )

        before, result = self._run_round(state, command)

        self._assert_side_effect_rejected(state, before, result)
        after = result[2]
        self.assertEqual(after.head, before.head)
        self.assertNotEqual(after.head_ref, before.head_ref)

    def test_validator_tracked_write_cannot_supply_done_vote(self):
        state = self._state()
        command = self._validator(
            "tracked",
            """\
from pathlib import Path

Path("tracked.txt").write_text("validator dirty\\n", encoding="utf-8")
""",
        )

        before, result = self._run_round(state, command)

        self._assert_side_effect_rejected(state, before, result)
        after = result[2]
        self.assertEqual(after.head, before.head)
        self.assertTrue(after.dirty)
        self.assertIn("tracked.txt", after.status)

    def test_validator_untracked_write_cannot_supply_done_vote(self):
        state = self._state()
        command = self._validator(
            "untracked",
            """\
from pathlib import Path

Path("validator-output.txt").write_text("side effect\\n", encoding="utf-8")
""",
        )

        before, result = self._run_round(state, command)

        self._assert_side_effect_rejected(state, before, result)
        after = result[2]
        self.assertEqual(after.head, before.head)
        self.assertTrue(after.dirty)
        self.assertIn("validator-output.txt", after.status)

    def test_failing_validator_keeps_red_and_done_reset_semantics(self):
        state = self._state(done_count=1)
        command = self._validator("failure", "raise SystemExit(7)\n")

        before, result = self._run_round(state, command, done_threshold=2)

        event, validate_note, after, rejected = result
        self.assertEqual(event, "")
        self.assertEqual(validate_note, "FAIL")
        self.assertFalse(rejected)
        self.assertEqual(after, before)
        self.assertEqual(state["red_streak"], 1)
        self.assertEqual(state["done_count"], 0)
        self.assertEqual(state["completed"], [])
        self.assertTrue(any("失敗" in note for note in state["notes"]))


if __name__ == "__main__":
    unittest.main()
