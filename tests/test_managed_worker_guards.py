"""Managed worker workspaces reject ordinary mutating Loop entrypoints early."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine import parallel_worker
from engine import platform_compat as compat


REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_ID = "a1b2c3d4"


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("git"), "需要 PATH 上有 git")
class TestManagedWorkerReadonlyGuard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Guard Test")
        _git(self.repo, "config", "user.email", "guard@example.invalid")
        (self.repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
        _git(self.repo, "add", "goal.md")
        _git(self.repo, "commit", "-qm", "goal")
        head = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

        integration_ref = f"refs/heads/loop/{RUN_ID}/integration"
        task_ref = f"refs/heads/loop/{RUN_ID}/task-1"
        _git(self.repo, "update-ref", integration_ref, head)
        _git(self.repo, "update-ref", task_ref, head)
        _git(self.repo, "symbolic-ref", "HEAD", task_ref)

        launch = parallel_worker.ManagedWorkerLaunch(
            resume=False,
            run_id=RUN_ID,
            assigned_order=1,
            stop_after_task=True,
            complete_gate_cmd="gate-client",
            integration_ref=integration_ref,
            parent_workspace="base",
            task_ref=task_ref,
            run_config_hash="1" * 64,
            launch_spec_hash="2" * 64,
            manifest_hash="3" * 64,
            dispatch_token="supervisor-dispatch-token",
            dispatch_request_id="4" * 32,
            supervisor_session="5" * 32,
            supervisor_generation=1,
            dispatch_attempt=0,
        )
        state = parallel_worker.initialize_state({
            "phase": "exec",
            "plan": [{"order": 1, "task": "managed task", "ref": None}],
            "completed": [],
        }, launch)

        self.workspace_root = self.root / "workspaces"
        self.workspace = self.workspace_root / "managed"
        self.workspace.mkdir(parents=True)
        self.state_path = self.workspace / "state.json"
        self.checkpoint_path = self.workspace / "state.last-good.json"
        state_bytes = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        self.state_path.write_bytes(state_bytes)
        self.checkpoint_path.write_bytes(state_bytes)
        self.startup_ready = self.workspace / "startup_ready.json"
        self.startup_ready.write_bytes(b'{"sentinel":true}')

        self.validator_marker = self.root / "validator-ran"
        self.validator = self.root / "validator.py"
        self.validator.write_text(
            "from pathlib import Path\n"
            f"Path({str(self.validator_marker)!r}).write_text('ran', encoding='utf-8')\n",
            encoding="utf-8",
        )
        self.plan = self.root / "plan.json"
        self.plan.write_text(
            json.dumps([{"order": 1, "task": "replacement"}]), encoding="utf-8")
        self.env = {
            **os.environ,
            "LOOP_AGENT_WORKSPACE_ROOT": str(self.workspace_root),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }

    def _run(self, *extra):
        return subprocess.run([
            sys.executable, "-m", "engine.loop",
            "--repo", str(self.repo), "--name", "managed",
            "--goal", "goal.md",
            "--validate-cmd", compat.join_command([sys.executable, str(self.validator)]),
            *extra,
        ], cwd=REPO_ROOT, env=self.env, capture_output=True, text=True, timeout=30)

    def _run_cli(self, *args):
        return subprocess.run([
            sys.executable, "-m", "engine.cli",
            "--workspace-root", str(self.workspace_root),
            *args,
        ], cwd=REPO_ROOT, env=self.env, capture_output=True, text=True, timeout=30)

    def test_reset_import_init_and_preflight_cannot_take_over_worker(self):
        original_state = self.state_path.read_bytes()
        original_checkpoint = self.checkpoint_path.read_bytes()
        original_ready = self.startup_ready.read_bytes()
        original_head = _git(self.repo, "rev-parse", "HEAD").stdout.strip()

        cases = (
            ("--reset-state",),
            ("--preflight-only",),
            ("--init-only", "--reset-state"),
            ("--init-only", "--import-plan", str(self.plan), "--start-phase", "exec"),
        )
        for extra in cases:
            with self.subTest(extra=extra):
                result = self._run(*extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("readonly workspace", result.stdout + result.stderr)
                self.assertEqual(self.state_path.read_bytes(), original_state)
                self.assertEqual(self.checkpoint_path.read_bytes(), original_checkpoint)
                self.assertEqual(self.startup_ready.read_bytes(), original_ready)
                self.assertFalse(self.validator_marker.exists())
                self.assertEqual(
                    _git(self.repo, "rev-parse", "HEAD").stdout.strip(), original_head)

    def test_high_level_run_check_config_and_stop_are_readonly_guarded(self):
        original_state = self.state_path.read_bytes()
        original_checkpoint = self.checkpoint_path.read_bytes()
        original_ready = self.startup_ready.read_bytes()

        cases = (
            ("run", "managed"),
            ("run", "managed", "--reset-state"),
            ("check", "managed"),
            ("config", "managed"),
            ("config", "managed", "--done-threshold", "2"),
            ("stop", "managed"),
            ("stop", "managed", "--now"),
        )
        for args in cases:
            with self.subTest(args=args):
                result = self._run_cli(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("readonly workspace", result.stdout + result.stderr)
                self.assertEqual(self.state_path.read_bytes(), original_state)
                self.assertEqual(self.checkpoint_path.read_bytes(), original_checkpoint)
                self.assertEqual(self.startup_ready.read_bytes(), original_ready)
                self.assertFalse((self.workspace / "stop-after-round.json").exists())
                self.assertFalse(self.validator_marker.exists())

    def test_direct_loop_cannot_reset_or_preflight_parallel_base(self):
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["runner"] = "parallel-supervisor"
        state.pop("assignment", None)
        state.pop("managed_readonly", None)
        payload = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
        self.state_path.write_bytes(payload)
        self.checkpoint_path.write_bytes(payload)
        original_ready = self.startup_ready.read_bytes()

        for extra in (("--reset-state",), ("--preflight-only",)):
            with self.subTest(extra=extra):
                result = self._run(*extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("parallel-supervisor base workspace",
                              result.stdout + result.stderr)
                self.assertEqual(self.state_path.read_bytes(), payload)
                self.assertEqual(self.checkpoint_path.read_bytes(), payload)
                self.assertEqual(self.startup_ready.read_bytes(), original_ready)
                self.assertFalse(self.validator_marker.exists())


if __name__ == "__main__":
    unittest.main()
