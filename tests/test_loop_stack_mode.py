"""Ordinary Loop never silently serializes an imported parallel stack."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine import platform_compat as compat


REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("git"), "需要 PATH 上有 git")
class TestOrdinaryLoopStackMode(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Stack Test")
        _git(self.repo, "config", "user.email", "stack@example.invalid")
        (self.repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
        _git(self.repo, "add", "goal.md")
        _git(self.repo, "commit", "-qm", "goal")
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps([
            {"order": 1, "task": "first", "stack": 5},
            {"order": 2, "task": "second", "stack": 5},
        ]), encoding="utf-8")
        self.validator = self.root / "validator.py"
        self.validator.write_text("raise SystemExit(0)\n", encoding="utf-8")
        self.workspace_root = self.root / "workspaces"
        self.env = {
            **os.environ,
            "LOOP_AGENT_WORKSPACE_ROOT": str(self.workspace_root),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }

    def command(self, name="ordinary"):
        return [
            sys.executable, "-m", "engine.loop",
            "--repo", str(self.repo), "--name", name,
            "--goal", "goal.md",
            "--validate-cmd", compat.join_command([sys.executable, str(self.validator)]),
        ]

    def _run_loop(self, *extra, name="ordinary"):
        return subprocess.run(
            [*self.command(name), *extra], cwd=REPO_ROOT, env=self.env,
            capture_output=True, text=True, timeout=30,
        )

    def test_import_rejects_stack_before_state_commit_without_opt_in(self):
        result = self._run_loop(
            "--init-only", "--import-plan", str(self.plan), "--start-phase", "exec")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("請改用 Parallel Loop", result.stdout + result.stderr)
        self.assertFalse((self.workspace_root / "ordinary" / "state.json").exists())

    def test_explicit_serial_opt_in_preserves_stack_and_replays_config(self):
        result = self._run_loop(
            "--init-only", "--import-plan", str(self.plan), "--start-phase", "exec",
            "--allow-serial-stack",
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads(
            (self.workspace_root / "ordinary" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual([task["stack"] for task in state["plan"]], [5, 5])
        self.assertIs(state["config"]["allow_serial_stack"], True)

    def test_serial_opt_in_still_rejects_stack_in_plan_phase(self):
        result = self._run_loop(
            "--init-only", "--import-plan", str(self.plan), "--start-phase", "plan",
            "--allow-serial-stack",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("直接從 exec", result.stdout + result.stderr)
        self.assertFalse((self.workspace_root / "ordinary" / "state.json").exists())

    def test_existing_stack_state_rejects_low_level_run_and_resume_without_opt_in(self):
        created = self._run_loop(
            "--init-only", "--import-plan", str(self.plan), "--start-phase", "exec",
            "--allow-serial-stack",
        )
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        state_path = self.workspace_root / "ordinary" / "state.json"
        before = state_path.read_bytes()

        for extra in ((), ("--resume-interrupted",)):
            with self.subTest(extra=extra):
                result = self._run_loop(*extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--allow-serial-stack", result.stdout + result.stderr)
                self.assertEqual(state_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
