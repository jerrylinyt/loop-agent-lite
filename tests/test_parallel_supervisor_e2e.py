"""Real two-worker ParallelSupervisor integration through stale revalidation."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine import parallel
from engine import platform_compat as compat


REPO_ROOT = Path(__file__).resolve().parent.parent


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("git"), "requires git")
class TestParallelSupervisorEndToEnd(unittest.TestCase):
    def test_two_workers_stale_sync_merge_receipts_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            workspace_root = root / "workspaces"
            repo.mkdir()
            workspace_root.mkdir()
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "Parallel E2E")
            git(repo, "config", "user.email", "parallel@example.invalid")
            (repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
            git(repo, "add", "goal.md")
            git(repo, "commit", "-qm", "initial")

            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps([
                {"order": 1, "task": "write task one", "stack": 1},
                {"order": 2, "task": "write task two", "stack": 1},
            ]), encoding="utf-8")
            validator = root / "validator.py"
            validator.write_text("raise SystemExit(0)\n", encoding="utf-8")
            agent = root / "agent.py"
            agent.write_text(
                """\
import os
import pathlib
import subprocess
import sys

branch = subprocess.run(
    ["git", "symbolic-ref", "--short", "HEAD"], check=True,
    capture_output=True, text=True).stdout.strip()
parts = branch.split("/")
run_id = parts[-2]
order = int(parts[-1].removeprefix("task-"))
sync_ref = f"refs/heads/loop/{run_id}/integration"
subprocess.run(["git", "merge", "--no-edit", sync_ref], check=True,
               capture_output=True, text=True)
target = pathlib.Path(f"task-{order}.txt")
if not target.exists():
    target.write_text(f"task {order}\\n", encoding="utf-8")
subprocess.run(["git", "add", str(target)], check=True)
if subprocess.run(["git", "status", "--porcelain"], check=True,
                  capture_output=True, text=True).stdout.strip():
    subprocess.run(["git", "commit", "-qm", f"task {order}"], check=True)
subprocess.run([sys.executable, "-m", "engine.work", "done", f"task-{order}"],
               check=True, env=os.environ.copy())
""", encoding="utf-8")

            old_env = os.environ.get("LOOP_AGENT_WORKSPACE_ROOT")
            try:
                os.environ["LOOP_AGENT_WORKSPACE_ROOT"] = str(workspace_root)
                result = parallel.main([
                    "--workspace-root", str(workspace_root),
                    "start",
                    "--repo", str(repo),
                    "--name", "base",
                    "--import-plan", str(plan_path),
                    "--goal", "goal.md",
                    "--agent-cmd", compat.join_command(
                        [sys.executable, str(agent)]),
                    "--validate-cmd", compat.join_command(
                        [sys.executable, str(validator)]),
                    "--done-threshold", "1",
                    "--max-parallel", "2",
                    "--validate-timeout", "5",
                    "--round-timeout", "1",
                ])
            finally:
                if old_env is None:
                    os.environ.pop("LOOP_AGENT_WORKSPACE_ROOT", None)
                else:
                    os.environ["LOOP_AGENT_WORKSPACE_ROOT"] = old_env

            self.assertEqual(result, 0)
            state = json.loads(
                (workspace_root / "base" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["runner"], "parallel-supervisor")
            self.assertEqual(state["phase"], "done")
            self.assertEqual(state["parallel"]["status"], "completed")
            self.assertIsNone(state["loop"]["pid"])
            self.assertEqual([item["order"] for item in state["completed"]], [1, 2])
            self.assertEqual((repo / "task-1.txt").read_text(), "task 1\n")
            self.assertEqual((repo / "task-2.txt").read_text(), "task 2\n")
            run_id = state["parallel"]["run_id"]
            run_dir = workspace_root / "base" / "parallel" / run_id
            self.assertTrue((run_dir / "receipts" / "task-1.json").is_file())
            self.assertTrue((run_dir / "receipts" / "task-2.json").is_file())
            self.assertFalse(
                (workspace_root / "base" / "worktrees" / f"{run_id}-task-1").exists())
            self.assertFalse(
                (workspace_root / "base" / "worktrees" / f"{run_id}-task-2").exists())
            self.assertTrue((workspace_root / "base" / "REPORT.md").is_file())


if __name__ == "__main__":
    unittest.main()
