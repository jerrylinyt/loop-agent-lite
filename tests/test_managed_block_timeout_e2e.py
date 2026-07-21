"""A valid managed block remains terminal even if the reporting agent later times out."""

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


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("git"), "需要 PATH 上有 git")
class TestManagedBlockTimeoutEndToEnd(unittest.TestCase):
    def test_block_then_hang_is_blocked_without_round_validator_or_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-q")
            _git(repo, "config", "user.name", "Block Test")
            _git(repo, "config", "user.email", "block@example.invalid")
            (repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
            _git(repo, "add", "goal.md")
            _git(repo, "commit", "-qm", "goal")
            head = _git(repo, "rev-parse", "HEAD").stdout.strip()
            integration_ref = f"refs/heads/loop/{RUN_ID}/integration"
            task_ref = f"refs/heads/loop/{RUN_ID}/task-1"
            _git(repo, "update-ref", integration_ref, head)
            _git(repo, "update-ref", task_ref, head)
            _git(repo, "symbolic-ref", "HEAD", task_ref)

            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps([{"order": 1, "task": "must block", "stack": 1}]),
                encoding="utf-8",
            )
            agent_path = root / "agent.py"
            agent_path.write_text(
                """\
import os
import subprocess
import sys
import time

result = subprocess.run(
    [sys.executable, "-m", "engine.work", "block", "--reason", "human gate required"],
    env=os.environ,
)
if result.returncode:
    raise SystemExit(result.returncode)
time.sleep(30)
""",
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
            gate_marker = root / "gate-ran.txt"
            gate_path = root / "gate.py"
            gate_path.write_text(
                "from pathlib import Path\n"
                f"Path({str(gate_marker)!r}).write_text('ran', encoding='utf-8')\n",
                encoding="utf-8",
            )

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
                "--round-timeout", "0.05", "--validate-timeout", "20",
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

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads(
                (workspace_root / worker_name / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "exec")
            self.assertEqual(state["assignment"]["status"], "blocked")
            self.assertEqual(state["assignment"]["exit_reason"], "human gate required")
            self.assertTrue(state["last_round_timed_out"])
            self.assertEqual(validate_count.read_text(encoding="utf-8"), "1")
            self.assertFalse(gate_marker.exists())


if __name__ == "__main__":
    unittest.main()
