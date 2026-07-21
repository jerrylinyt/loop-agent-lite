"""A worker that changes branches is blocked before any automatic reset/clean."""

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
RUN_ID = "a1b2c3d4"


def _git(repo: Path, *args: str, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("git"), "需要 PATH 上有 git")
class TestManagedBranchTamperEndToEnd(unittest.TestCase):
    def test_wrong_branch_and_protected_change_are_not_reset_on_sibling_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-q")
            _git(repo, "config", "user.name", "Branch Test")
            _git(repo, "config", "user.email", "branch@example.invalid")
            (repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
            _git(repo, "add", "goal.md")
            _git(repo, "commit", "-qm", "goal")
            original = _git(repo, "rev-parse", "HEAD").stdout.strip()
            integration_ref = f"refs/heads/loop/{RUN_ID}/integration"
            task_ref = f"refs/heads/loop/{RUN_ID}/task-1"
            sibling_ref = "refs/heads/sibling"
            for ref in (integration_ref, task_ref):
                _git(repo, "update-ref", ref, original)
            _git(repo, "symbolic-ref", "HEAD", task_ref)

            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps([{"order": 1, "task": "stay on task ref", "stack": 1}]),
                encoding="utf-8",
            )
            agent_path = root / "agent.py"
            agent_path.write_text(
                f'''\
import subprocess
from pathlib import Path

subprocess.run(["git", "update-ref", "{sibling_ref}", "HEAD"], check=True)
subprocess.run(["git", "symbolic-ref", "HEAD", "{sibling_ref}"], check=True)
Path("goal.md").write_text("# changed by wrong branch agent\\n", encoding="utf-8")
''',
                encoding="utf-8",
            )
            validate_count = root / "validate-count.txt"
            validator_path = root / "validator.py"
            validator_path.write_text(
                "from pathlib import Path\n"
                f"path = Path({str(validate_count)!r})\n"
                "count = int(path.read_text(encoding='utf-8')) if path.exists() else 0\n"
                "path.write_text(str(count + 1), encoding='utf-8')\n",
                encoding="utf-8",
            )
            gate_path = root / "gate.py"
            gate_path.write_text("raise SystemExit(99)\n", encoding="utf-8")

            workspace_root = root / "workspaces"
            worker_name = f"base--{RUN_ID}-task-1"
            command = [
                sys.executable, "-m", "engine.loop",
                "--repo", str(repo), "--name", worker_name,
                "--goal", "goal.md",
                "--agent-cmd", compat.join_command([sys.executable, str(agent_path)]),
                "--validate-cmd", compat.join_command([sys.executable, str(validator_path)]),
                "--done-threshold", "1", "--flag-threshold", "1",
                "--red-limit", "3", "--stall-limit", "20",
                "--round-timeout", "1", "--validate-timeout", "20",
                "--import-plan", str(plan_path), "--start-phase", "exec",
                "--start-task", "1", "--stop-after-task",
                "--complete-gate-cmd", compat.join_command([sys.executable, str(gate_path)]),
                "--integration-ref", integration_ref,
                "--parent-workspace", "base", "--task-ref", task_ref,
                "--run-config-hash", "1" * 64,
                "--launch-spec-hash", "2" * 64,
                "--manifest-hash", "3" * 64,
            ]
            env = {
                **os.environ,
                "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root),
                "PYTHONPATH": str(REPO_ROOT),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            }

            result = subprocess.run(
                command, cwd=REPO_ROOT, env=env,
                capture_output=True, text=True, timeout=30,
            )

            self.assertNotEqual(result.returncode, 0)
            state = json.loads(
                (workspace_root / worker_name / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["assignment"]["status"], "blocked")
            self.assertIn("refs/heads/sibling", state["assignment"]["exit_reason"])
            self.assertEqual(validate_count.read_text(encoding="utf-8"), "1")
            self.assertEqual(_git(repo, "rev-parse", task_ref).stdout.strip(), original)
            self.assertEqual(_git(repo, "rev-parse", sibling_ref).stdout.strip(), original)
            self.assertEqual(
                _git(repo, "symbolic-ref", "HEAD").stdout.strip(), sibling_ref)
            self.assertEqual(
                (repo / "goal.md").read_text(encoding="utf-8"),
                "# changed by wrong branch agent\n",
            )


if __name__ == "__main__":
    unittest.main()
