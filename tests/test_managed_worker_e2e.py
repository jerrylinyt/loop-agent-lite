"""One real managed worker round keeps Loop semantics and exits at its gate."""

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
class TestManagedWorkerEndToEnd(unittest.TestCase):
    def test_worker_integrates_assigned_task_without_global_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, "init", "-q")
            _git(repo, "config", "user.name", "Worker Test")
            _git(repo, "config", "user.email", "worker@example.invalid")
            (repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
            _git(repo, "add", "goal.md")
            _git(repo, "commit", "-qm", "goal")
            head = _git(repo, "rev-parse", "HEAD").stdout.strip()
            integration_ref = f"refs/heads/loop/{RUN_ID}/integration"
            task_ref = f"refs/heads/loop/{RUN_ID}/task-2"
            _git(repo, "update-ref", integration_ref, head)
            _git(repo, "update-ref", task_ref, head)
            _git(repo, "symbolic-ref", "HEAD", task_ref)

            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps([
                {"order": 1, "task": "parallel first", "stack": 1},
                {"order": 2, "task": "parallel second", "stack": 1},
            ]), encoding="utf-8")
            agent_path = root / "agent.py"
            agent_path.write_text(
                """\
import os
import subprocess
import sys

raise SystemExit(subprocess.run(
    [sys.executable, "-m", "engine.work", "done", "task-2"],
    env=os.environ,
).returncode)
""", encoding="utf-8")
            validator_path = root / "validator.py"
            validator_path.write_text("raise SystemExit(0)\n", encoding="utf-8")
            gate_path = root / "gate.py"
            gate_path.write_text(
                """\
import json
import os

print(json.dumps({
    "status": "merged",
    "run_id": os.environ["RUN_ID"],
    "task": int(os.environ["TASK"]),
    "request_id": os.environ["REQUEST_ID"],
    "validated_sha": os.environ["VALIDATED_SHA"],
}, separators=(",", ":")))
""", encoding="utf-8")

            workspace_root = root / "workspaces"
            worker_name = f"base--{RUN_ID}-task-2"
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
                "--start-task", "2", "--stop-after-task",
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
            notify_attempt = subprocess.run(
                [*command, "--notify-cmd", "notify-hook"],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertNotEqual(notify_attempt.returncode, 0)
            self.assertIn("不可直接送全域 notify", notify_attempt.stdout + notify_attempt.stderr)
            self.assertFalse((workspace_root / worker_name / "state.json").exists())

            result = subprocess.run(
                command, cwd=REPO_ROOT, env=env,
                capture_output=True, text=True, timeout=60,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            workspace = workspace_root / worker_name
            state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "exec")
            self.assertEqual(state["current_order"], 2)
            self.assertEqual(state["completed"], [])
            self.assertEqual(state["assignment"]["status"], "integrated")
            self.assertEqual(state["assignment"]["validated_sha"], head)
            self.assertEqual(state["assignment"]["validated_round"], 1)
            self.assertIsNone(state["loop"]["pid"])
            self.assertFalse((workspace / "REPORT.md").exists())
            prompt = (workspace / "prompts" / "round-0001.md").read_text(encoding="utf-8")
            self.assertIn(f"git merge --no-edit {integration_ref}", prompt)
            self.assertIn("engine.work block --reason", prompt)
            self.assertNotIn("<<SYNC_INTEGRATION>>", prompt)

            # A forged/stale resume argv must be rejected without allowing the caller
            # to terminalize or otherwise rewrite the durable worker state.
            state["assignment"].update({
                "status": "running",
                "validated_sha": None,
                "validated_round": None,
                "exit_reason": None,
                "gate_request": None,
            })
            state_bytes = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
            state_path = workspace / "state.json"
            checkpoint_path = workspace / "state.last-good.json"
            state_path.write_bytes(state_bytes)
            checkpoint_path.write_bytes(state_bytes)
            resume_command = list(command)
            for option in ("--import-plan", "--start-phase"):
                index = resume_command.index(option)
                del resume_command[index:index + 2]
            done_index = resume_command.index("--done-threshold")
            resume_command[done_index + 1] = "2"
            resume_command.append("--managed-worker-resume")

            mismatch = subprocess.run(
                resume_command, cwd=REPO_ROOT, env=env,
                capture_output=True, text=True, timeout=60,
            )

            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("runtime argv", mismatch.stdout + mismatch.stderr)
            self.assertEqual(state_path.read_bytes(), state_bytes)
            self.assertEqual(checkpoint_path.read_bytes(), state_bytes)


if __name__ == "__main__":
    unittest.main()
