"""Real-runner coverage for ordinary Loop/Ralph common-dir ownership."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from engine import loop as loop_mod
from engine import platform_compat as compat
from engine import repo_owner


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True,
    )


@unittest.skipUnless(shutil.which("git"), "git is required")
class OrdinaryOwnerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Owner Integration Test")
        _git(self.repo, "config", "user.email", "owner@example.invalid")
        (self.repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
        _git(self.repo, "add", "goal.md")
        _git(self.repo, "commit", "-qm", "initial")

    def _env(self, workspace_root: Path) -> dict:
        return {
            **os.environ,
            "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root),
            "PYTHONPATH": str(PROJECT_ROOT),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }

    def _loop(self, workspace_root: Path, name: str, *extra,
              timeout=40) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "engine.loop",
             "--repo", str(self.repo), "--name", name,
             "--goal", "goal.md", *extra],
            cwd=PROJECT_ROOT, env=self._env(workspace_root),
            capture_output=True, text=True, timeout=timeout,
        )

    def test_real_preflight_claims_once_and_terminalizes_marker(self):
        workspace_root = self.root / "preflight-workspaces"
        validator = compat.join_command(
            [sys.executable, "-c", "raise SystemExit(0)"])

        result = self._loop(
            workspace_root, "preflight", "--preflight-only",
            "--validate-cmd", validator)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["owner_kind"], repo_owner.OwnerKind.LOOP.value)
        self.assertEqual(marker["state"], "terminal")
        self.assertEqual(marker["child_state"], "idle")
        self.assertEqual(marker["terminal_reason"], "loop-returned")

    def test_failed_preflight_checkpoints_child_before_terminalizing(self):
        workspace_root = self.root / "failed-workspaces"
        validator = compat.join_command(
            [sys.executable, "-c", "raise SystemExit(7)"])

        result = self._loop(
            workspace_root, "failed", "--preflight-only",
            "--validate-cmd", validator)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["state"], "terminal")
        self.assertEqual(marker["child_state"], "idle")
        self.assertEqual(marker["terminal_reason"], "loop-failed")

    def test_pause_after_plan_reaps_real_grandchild_and_terminalizes(self):
        workspace_root = self.root / "pause-workspaces"
        late_write = self.root / "escaped-grandchild.txt"
        plan = self.root / "plan.json"
        plan.write_text(
            json.dumps([{"order": 1, "task": "only task"}]),
            encoding="utf-8")
        agent = self.root / "plan-agent.py"
        grandchild = (
            "import pathlib,sys,time; time.sleep(1.2); "
            "pathlib.Path(sys.argv[1]).write_text('escaped',encoding='utf-8')"
        )
        agent.write_text(
            "import os,subprocess,sys\n"
            "sys.stdin.buffer.read()\n"
            f"code={grandchild!r}\n"
            f"target={str(late_write)!r}\n"
            "subprocess.Popen([sys.executable,'-c',code,target],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL)\n"
            "subprocess.run([sys.executable,'-m','engine.work','plan-ok'],"
            "env=dict(os.environ),check=True)\n",
            encoding="utf-8")
        command = compat.join_command([sys.executable, str(agent)])
        validator = compat.join_command(
            [sys.executable, "-c", "raise SystemExit(0)"])

        result = self._loop(
            workspace_root, "paused",
            "--agent-cmd", command,
            "--validate-cmd", validator,
            "--import-plan", str(plan),
            "--start-phase", "plan",
            "--flag-threshold", "1",
            "--pause-after-plan",
            "--max-rounds", "2")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        time.sleep(1.5)
        self.assertFalse(late_write.exists(),
                         "grandchild survived the controlled owner Job/group")
        state = json.loads(
            (workspace_root / "paused" / "state.json").read_text(
                encoding="utf-8"))
        self.assertEqual(state["phase"], "exec")
        self.assertIsNone(state["loop"]["pid"])
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["state"], "terminal")
        self.assertEqual(marker["child_state"], "idle")

    def test_two_workspace_roots_share_one_owner_and_strong_kill_stays_nonterminal(self):
        first_root = self.root / "workspaces-a"
        second_root = self.root / "workspaces-b"
        ready = self.root / "validator-ready"
        validator_script = self.root / "slow-validator.py"
        validator_script.write_text(
            "from pathlib import Path\n"
            "import sys,time\n"
            "Path(sys.argv[1]).write_text('ready',encoding='utf-8')\n"
            "time.sleep(30)\n",
            encoding="utf-8")
        slow_validator = compat.join_command(
            [sys.executable, str(validator_script), str(ready)])
        first = subprocess.Popen(
            [sys.executable, "-m", "engine.loop",
             "--repo", str(self.repo), "--name", "first",
             "--goal", "goal.md", "--preflight-only",
             "--validate-cmd", slow_validator],
            cwd=PROJECT_ROOT, env=self._env(first_root),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

        def cleanup_first():
            if first.poll() is None:
                first.kill()
            try:
                first.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
            if first.stdout is not None:
                first.stdout.close()

        self.addCleanup(cleanup_first)
        deadline = time.monotonic() + 15
        while not ready.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists(), "first Loop never entered its validator")
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["state"], "active")
        self.assertEqual(marker["child_state"], "child_running")

        child_identity = marker["child_identity"]

        def cleanup_recorded_child():
            try:
                if compat.IS_WINDOWS:
                    compat.kill_process_group(child_identity["pid"])
                else:
                    os.killpg(
                        int(child_identity["containment_id"]), signal.SIGKILL)
            except (OSError, ProcessLookupError, PermissionError, ValueError):
                pass

        self.addCleanup(cleanup_recorded_child)

        validator = compat.join_command(
            [sys.executable, "-c", "raise SystemExit(0)"])
        second = self._loop(
            second_root, "second", "--preflight-only",
            "--validate-cmd", validator, timeout=15)
        self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
        self.assertIn("repository owner fence blocked", second.stdout + second.stderr)

        first.kill()
        first.wait(timeout=15)
        if first.stdout is not None:
            first.stdout.close()
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertNotEqual(marker["state"], "terminal")
        self.assertEqual(marker["child_state"], "child_running")

    def test_keyboard_interrupt_runs_state_checkpoint_then_terminalizes(self):
        workspace_root = self.root / "keyboard-workspaces"
        with mock.patch.object(loop_mod, "WORKSPACE_ROOT", workspace_root):
            workspace = loop_mod.Workspace("keyboard")

            def interrupted(_argv):
                loop_mod._claim_repo_owner(
                    self.repo, workspace, repo_owner.OwnerKind.LOOP)

                def checkpoint():
                    state = workspace.fresh_state()
                    state["loop"] = {"pid": None}
                    workspace.save_state(state)

                loop_mod._REPO_OWNER_STOP_CHECKPOINT = checkpoint
                raise KeyboardInterrupt

            with mock.patch.object(loop_mod, "_main_impl", side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    loop_mod.main([])

        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["state"], "terminal")
        self.assertEqual(marker["terminal_reason"], "loop-interrupted")
        self.assertTrue((workspace.dir / "state.json").is_file())

    @unittest.skipUnless(
        compat.IS_WINDOWS or sys.platform.startswith("linux"),
        "controlled owner guardian requires Windows or Linux",
    )
    def test_keyboard_interrupt_quiesces_retained_child_before_checkpoint(self):
        workspace_root = self.root / "keyboard-child-workspaces"
        observed = {}
        with mock.patch.object(loop_mod, "WORKSPACE_ROOT", workspace_root):
            workspace = loop_mod.Workspace("keyboard-child")

            def interrupted(_argv):
                fence = loop_mod._claim_repo_owner(
                    self.repo, workspace, repo_owner.OwnerKind.LOOP)
                child = fence.spawn_child(
                    repo_owner.ChildKind.GIT,
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                observed["child"] = child

                def checkpoint():
                    observed["child_was_quiescent"] = child.poll() is not None
                    state = workspace.fresh_state()
                    state["loop"] = {"pid": None}
                    workspace.save_state(state)

                loop_mod._REPO_OWNER_STOP_CHECKPOINT = checkpoint
                raise KeyboardInterrupt

            with mock.patch.object(loop_mod, "_main_impl", side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    loop_mod.main([])

        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertTrue(observed["child_was_quiescent"])
        self.assertEqual(marker["state"], "terminal")
        self.assertEqual(marker["child_state"], "child_reaped")
        self.assertEqual(marker["terminal_reason"], "loop-interrupted")

    def test_ralph_payload_grandchild_is_reaped_and_marker_terminal(self):
        workspace_root = self.root / "ralph-workspaces"
        late_write = self.root / "ralph-grandchild.txt"
        prd = self.repo / "prd.json"
        prd.write_text(json.dumps({
            "project": "owner test",
            "userStories": [{"id": "US-001", "title": "one", "passes": False}],
        }), encoding="utf-8")
        payload = self.root / "fake-ralph.py"
        grandchild = (
            "import pathlib,sys,time; time.sleep(1.2); "
            "pathlib.Path(sys.argv[1]).write_text('escaped',encoding='utf-8')"
        )
        payload.write_text(
            "import subprocess,sys\n"
            f"code={grandchild!r}\n"
            f"target={str(late_write)!r}\n"
            "subprocess.Popen([sys.executable,'-c',code,target],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL)\n"
            "print('<promise>COMPLETE</promise>',flush=True)\n",
            encoding="utf-8")
        _git(self.repo, "add", "prd.json")
        _git(self.repo, "commit", "-qm", "add prd")
        ralph_command = compat.join_command([sys.executable, str(payload)])

        result = subprocess.run(
            [sys.executable, "-m", "engine.ralph",
             "--repo", str(self.repo), "--name", "ralph",
             "--ralph-cmd", ralph_command,
             "--ralph-dir", str(self.repo),
             "--prd-path", "prd.json",
             "--iterations", "1", "--tool", "claude",
             "--args-style", "positional"],
            cwd=PROJECT_ROOT, env=self._env(workspace_root),
            capture_output=True, text=True, timeout=40,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        time.sleep(1.5)
        self.assertFalse(late_write.exists(),
                         "Ralph payload grandchild escaped its owner Job/group")
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["owner_kind"], repo_owner.OwnerKind.RALPH.value)
        self.assertEqual(marker["state"], "terminal")
        self.assertEqual(marker["child_state"], "idle")
        self.assertEqual(marker["terminal_reason"], "ralph-returned")

    def test_failed_ralph_payload_still_terminalizes_owner(self):
        workspace_root = self.root / "ralph-failed-workspaces"
        (self.repo / "prd.json").write_text(json.dumps({
            "project": "owner failure test",
            "userStories": [{"id": "US-001", "title": "one", "passes": False}],
        }), encoding="utf-8")
        payload = self.root / "failed-ralph.py"
        payload.write_text("raise SystemExit(7)\n", encoding="utf-8")
        _git(self.repo, "add", "prd.json")
        _git(self.repo, "commit", "-qm", "add failure prd")

        result = subprocess.run(
            [sys.executable, "-m", "engine.ralph",
             "--repo", str(self.repo), "--name", "ralph-failed",
             "--ralph-cmd", compat.join_command([sys.executable, str(payload)]),
             "--ralph-dir", str(self.repo), "--prd-path", "prd.json",
             "--iterations", "1", "--tool", "claude",
             "--args-style", "positional"],
            cwd=PROJECT_ROOT, env=self._env(workspace_root),
            capture_output=True, text=True, timeout=40,
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["owner_kind"], repo_owner.OwnerKind.RALPH.value)
        self.assertEqual(marker["state"], "terminal")
        self.assertEqual(marker["child_state"], "idle")
        self.assertEqual(marker["terminal_reason"], "ralph-failed")


if __name__ == "__main__":
    unittest.main()
