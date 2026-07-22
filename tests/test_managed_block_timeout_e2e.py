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
from tests.parallel_worker_harness import prepare_authorized_worker


REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_ID = "a1b2c3d4"


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("git"), "需要 PATH 上有 git")
class TestManagedBlockTimeoutEndToEnd(unittest.TestCase):
    def test_block_then_hang_is_blocked_without_round_validator_or_gate(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        with self.subTest(authority="immutable-parent"):
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-q")
            _git(repo, "config", "user.name", "Block Test")
            _git(repo, "config", "user.email", "block@example.invalid")
            (repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
            _git(repo, "add", "goal.md")
            _git(repo, "commit", "-qm", "goal")
            plan = [{"order": 1, "task": "must block", "stack": 1}]
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

            harness = prepare_authorized_worker(
                root=root, primary_repo=repo, plan=plan, order=1,
                agent_cmd=compat.join_command([sys.executable, str(agent_path)]),
                validate_cmd=compat.join_command([sys.executable, str(validator_path)]),
                gate_cmd=compat.join_command([sys.executable, str(gate_path)]),
                round_timeout=0.05,
            )
            self.addCleanup(harness.close)
            workspace_root = harness.workspace_root
            worker_name = harness.worker_workspace
            command = harness.command
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
