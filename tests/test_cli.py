"""專案內 Python 入口、單 workspace CLI 與固定 runtime 路徑回歸測試。"""
import contextlib
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest import mock

import dashboard as dashboard_launcher
import loop as loop_launcher
from engine import cli, dashboard, loop as loop_engine, paths, platform_compat as compat


REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "cli-test@example.invalid")
    _git(repo, "config", "user.name", "CLI Test")
    (repo / "goal.md").write_text("# Goal\n\n完成單一測試任務。\n", encoding="utf-8")
    _git(repo, "add", "goal.md")
    _git(repo, "commit", "-qm", "initial goal")
    return repo


def _runtime_config(repo: Path, **updates) -> dict:
    config = {
        "repo": str(repo.resolve()),
        "agent_cmd": "agent --mode test",
        "validate_cmd": "validator --quick",
        "goal": "goal.md",
        "plan_doc": "",
        "flag_threshold": 10,
        "done_threshold": 3,
        "red_limit": 20,
        "stall_limit": 300,
        "stuck_stop": False,
        "stuck_stop_count": 100,
        "round_timeout": 30.0,
        "agent_backoff_max": 60.0,
        "validate_timeout": 120.0,
        "pause_after_plan": False,
        "notify_cmd": "",
    }
    config.update(updates)
    return config


def _seed_workspace(workspace_root: Path, name: str, repo: Path, **state_updates):
    with mock.patch.object(loop_engine, "WORKSPACE_ROOT", workspace_root):
        workspace = loop_engine.Workspace(name)
        state = workspace.fresh_state()
        state["config"] = _runtime_config(repo)
        state.update(state_updates)
        workspace.save_state(state)
    return workspace_root / name, state


class TestProjectDashboard(unittest.TestCase):
    def test_root_dashboard_options_are_forwarded(self):
        with mock.patch.object(dashboard, "run_dashboard", return_value=0) as run:
            result = dashboard_launcher.main(["--name", "demo", "--port", "9000", "--read-only"])
        self.assertEqual(result, 0)
        run.assert_called_once_with(name="demo", port=9000, read_only=True)

    def test_runtime_assets_are_inside_engine_package(self):
        package = files("engine")
        for relative in (
            "dashboard.config.shared.json",
            "prompts/plan.md",
            "prompts/exec.md",
            "prompts/external-agent-base.md",
            "prompts/external-agent-goal.md",
            "prompts/external-agent-goal-template.md",
            "prompts/external-agent-plan.md",
            "prompts/external-agent-missing.md",
            "prompts/external-agent-team-template-example.md",
            "ui/index.html",
        ):
            with self.subTest(relative=relative):
                self.assertTrue(package.joinpath(relative).is_file())

    def test_defaults_stay_under_project_root(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(paths, "PROJECT_ROOT", Path(directory)), \
                mock.patch.dict("os.environ", {}, clear=True):
            root = Path(directory).resolve()
            self.assertEqual(paths.default_workspace_root(), root / "workspace")
            self.assertEqual(paths.default_personal_config(), root / "dashboard.config.local.json")
            self.assertEqual(paths.legacy_config_path(), root / "dashboard.config.json")

    def test_explicit_workspace_override_is_kept_for_isolated_runs(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                "os.environ", {"LOOP_AGENT_WORKSPACE_ROOT": directory}, clear=True):
            self.assertEqual(paths.default_workspace_root(), Path(directory).resolve())


class TestSingleWorkspaceCli(unittest.TestCase):
    def test_run_replays_every_saved_runtime_parameter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_root = root / "workspaces"
            repo = root / "target repo"
            repo.mkdir()
            workspace_dir, _state = _seed_workspace(workspace_root, "replay", repo)
            state = json.loads((workspace_dir / "state.json").read_text(encoding="utf-8"))
            state["config"] = _runtime_config(
                repo,
                agent_cmd="agent --profile 'night shift'",
                validate_cmd="sh -c 'python -m unittest && echo green'",
                goal="specs/goal.md",
                plan_doc="docs/PLAN.md",
                flag_threshold=7,
                done_threshold=5,
                red_limit=11,
                stall_limit=41,
                stuck_stop=True,
                stuck_stop_count=13,
                round_timeout=4.5,
                agent_backoff_max=9.25,
                validate_timeout=17.5,
                pause_after_plan=True,
                notify_cmd="notify --status {status} --name {name}",
            )
            data = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
            loop_engine.write_checkpointed_state(workspace_dir / "state.json", data)

            with mock.patch.object(loop_engine, "WORKSPACE_ROOT", workspace_root), \
                    mock.patch.object(cli, "_exec_engine") as execute:
                result = loop_launcher.main(
                    ["--workspace-root", str(workspace_root), "run", "replay"])

            self.assertEqual(result, 0)
            execute.assert_called_once()
            replayed = execute.call_args.args[0]
            options = loop_engine.parse_runtime_options(replayed)
            self.assertEqual(options.workspace_name, "replay")
            self.assertEqual(options.repo, repo.resolve())
            self.assertEqual(options.agent_cmd, ["agent", "--profile", "night shift"])
            # Windows 上裸 sh 會被釘成絕對路徑(CreateProcess 先搜 System32 的 WSL stub)。
            if compat.IS_WINDOWS:
                self.assertTrue(Path(options.validate_cmd[0]).is_absolute())
                self.assertIn(Path(options.validate_cmd[0]).name.casefold(),
                              {"sh.exe", "bash.exe"})
            else:
                self.assertEqual(options.validate_cmd[0], "sh")
            self.assertEqual(
                options.validate_cmd[1:],
                ["-c", "python -m unittest && echo green"],
            )
            self.assertEqual(options.args.goal, "specs/goal.md")
            self.assertEqual(options.args.plan_doc, "docs/PLAN.md")
            self.assertEqual(options.args.flag_threshold, 7)
            self.assertEqual(options.args.done_threshold, 5)
            self.assertEqual(options.args.red_limit, 11)
            self.assertEqual(options.args.stall_limit, 41)
            self.assertTrue(options.args.stuck_stop)
            self.assertEqual(options.args.stuck_stop_count, 13)
            self.assertEqual(options.args.round_timeout, 4.5)
            self.assertEqual(options.args.agent_backoff_max, 9.25)
            self.assertEqual(options.args.validate_timeout, 17.5)
            self.assertTrue(options.args.pause_after_plan)
            self.assertEqual(
                options.args.notify_cmd, "notify --status {status} --name {name}")

    def test_done_workspace_requires_explicit_reset_before_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_root = root / "workspaces"
            repo = root / "repo"
            repo.mkdir()
            _seed_workspace(workspace_root, "finished", repo, phase="done")

            stderr = io.StringIO()
            with mock.patch.object(loop_engine, "WORKSPACE_ROOT", workspace_root), \
                    mock.patch.object(cli, "_exec_engine") as execute, \
                    contextlib.redirect_stderr(stderr):
                result = loop_launcher.main(
                    ["--workspace-root", str(workspace_root), "run", "finished"])

            self.assertEqual(result, 1)
            self.assertIn("已完成", stderr.getvalue())
            self.assertIn("--reset-state", stderr.getvalue())
            execute.assert_not_called()

    def test_run_rejects_config_repo_that_differs_from_immutable_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_root = root / "workspaces"
            bound_repo = root / "bound-repo"
            substituted_repo = root / "substituted-repo"
            bound_repo.mkdir()
            substituted_repo.mkdir()
            workspace_dir, _state = _seed_workspace(
                workspace_root, "bound", bound_repo,
                phase="exec",
                plan=[{"order": 1, "task": "不可帶到另一個 repo", "ref": None}],
                current_order=1,
                last_green_sha="a" * 40,
                repo_binding=str(bound_repo.resolve()),
            )
            state = json.loads((workspace_dir / "state.json").read_text(encoding="utf-8"))
            state["config"]["repo"] = str(substituted_repo.resolve())
            before = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
            loop_engine.write_checkpointed_state(workspace_dir / "state.json", before)

            stderr = io.StringIO()
            with mock.patch.object(loop_engine, "WORKSPACE_ROOT", workspace_root), \
                    mock.patch.object(cli, "_exec_engine") as execute, \
                    contextlib.redirect_stderr(stderr):
                result = loop_launcher.main(
                    ["--workspace-root", str(workspace_root), "run", "bound"])

            self.assertEqual(result, 1)
            self.assertIn("config.repo", stderr.getvalue())
            self.assertIn("repo_binding", stderr.getvalue())
            execute.assert_not_called()
            self.assertEqual((workspace_dir / "state.json").read_bytes(), before)
            self.assertEqual((workspace_dir / "state.last-good.json").read_bytes(), before)

    def test_config_update_is_stopped_only_and_keeps_checkpoint_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_root = root / "workspaces"
            repo = root / "repo"
            repo.mkdir()
            workspace_dir, _state = _seed_workspace(
                workspace_root, "configurable", repo,
                phase="exec", round=12, current_order=1,
            )

            try:
                with mock.patch.object(loop_engine, "WORKSPACE_ROOT", workspace_root):
                    result = loop_launcher.main([
                        "--workspace-root", str(workspace_root),
                        "config", "configurable",
                        "--done-threshold", "7",
                        "--round-timeout", "4.5",
                        "--pause-after-plan",
                    ])
            finally:
                # 正常 CLI 進程會在退出時由 atexit 釋放；單元測試留在同一進程需顯式清理。
                loop_engine.release_run_locks()

            self.assertEqual(result, 0)
            primary = (workspace_dir / "state.json").read_bytes()
            checkpoint = (workspace_dir / "state.last-good.json").read_bytes()
            self.assertEqual(primary, checkpoint)
            updated = json.loads(primary)
            self.assertEqual(updated["phase"], "exec")
            self.assertEqual(updated["round"], 12)
            self.assertEqual(updated["current_order"], 1)
            self.assertEqual(updated["config"]["done_threshold"], 7)
            self.assertEqual(updated["config"]["round_timeout"], 4.5)
            self.assertTrue(updated["config"]["pause_after_plan"])

            before_locked_attempt = primary
            lock_path = workspace_dir / ".run.lock"
            with lock_path.open("a+b") as held_lock:
                compat.lock_file(held_lock, blocking=False)
                locked = subprocess.run(
                    [
                        sys.executable, str(REPO_ROOT / "loop.py"),
                        "--workspace-root", str(workspace_root),
                        "config", "configurable", "--done-threshold", "9",
                    ],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                compat.unlock_file(held_lock)

            self.assertEqual(locked.returncode, 1, locked.stdout + locked.stderr)
            self.assertIn("已有另一個 loop", locked.stdout + locked.stderr)
            self.assertEqual((workspace_dir / "state.json").read_bytes(), before_locked_attempt)
            self.assertEqual(
                (workspace_dir / "state.last-good.json").read_bytes(),
                before_locked_attempt,
            )

    def test_init_then_run_completes_one_task_through_engine_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _make_repo(root)
            workspace_root = root / "workspaces"
            plan = root / "plan.json"
            plan.write_text(json.dumps([
                {"order": 1, "task": "確認既有目標已完成", "ref": "goal.md"},
            ], ensure_ascii=False), encoding="utf-8")
            marker = root / "agent-started"
            agent = root / "fake_agent.py"
            agent.write_text(
                "from pathlib import Path\n"
                "import subprocess\n"
                "import sys\n"
                f"Path({str(marker)!r}).write_text('started', encoding='utf-8')\n"
                "sys.stdin.read()\n"
                "result = subprocess.run([sys.executable, '-m', 'engine.work', "
                "'done', 'task-1'], check=False)\n"
                "raise SystemExit(result.returncode)\n",
                encoding="utf-8",
            )
            agent_cmd = shlex.join([sys.executable, str(agent)])
            base_command = [
                sys.executable, str(REPO_ROOT / "loop.py"),
                "--workspace-root", str(workspace_root),
            ]

            initialized = subprocess.run(
                [
                    *base_command, "init",
                    "--repo", str(repo),
                    "--name", "one-task",
                    "--agent-cmd", agent_cmd,
                    "--validate-cmd", "true",
                    "--plan", str(plan),
                    "--start-phase", "exec",
                    "--done-threshold", "1",
                    "--round-timeout", "1",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(
                initialized.returncode, 0, initialized.stdout + initialized.stderr)
            self.assertFalse(marker.exists(), "--init-only 不得啟動 Agent")

            workspace_dir = workspace_root / "one-task"
            initial_primary = (workspace_dir / "state.json").read_bytes()
            self.assertEqual(
                initial_primary, (workspace_dir / "state.last-good.json").read_bytes())
            initial_state = json.loads(initial_primary)
            self.assertEqual(initial_state["phase"], "exec")
            self.assertIsNone(initial_state["loop"]["pid"])
            self.assertEqual(initial_state["config"]["done_threshold"], 1)

            completed = subprocess.run(
                [*base_command, "run", "one-task"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertTrue(marker.exists())
            final_primary = (workspace_dir / "state.json").read_bytes()
            self.assertEqual(
                final_primary, (workspace_dir / "state.last-good.json").read_bytes())
            final_state = json.loads(final_primary)
            self.assertEqual(final_state["phase"], "done")
            self.assertEqual([entry["order"] for entry in final_state["completed"]], [1])
            self.assertIsNone(final_state["loop"]["pid"])
            self.assertEqual(final_state["config"]["done_threshold"], 1)
            self.assertTrue((workspace_dir / "REPORT.md").is_file())

    def test_stop_now_timeout_never_escalates_to_sigkill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_root = root / "workspaces"
            repo = root / "repo"
            repo.mkdir()
            _seed_workspace(
                workspace_root, "slow-stop", repo,
                loop={"pid": 4242, "session_id": "session", "started_at": "2026-01-01T00:00:00"},
            )

            stderr = io.StringIO()
            with mock.patch.object(loop_engine, "WORKSPACE_ROOT", workspace_root), \
                    mock.patch.object(loop_engine, "active_run_lock_owner",
                                      return_value={"pid": 4242}), \
                    mock.patch.object(cli.status_mod, "pid_is_loop_alive", return_value=True), \
                    mock.patch.object(cli, "_pid_matches_workspace", return_value=True), \
                    mock.patch.object(loop_engine, "safe_kill") as safe_kill, \
                    mock.patch.object(cli.time, "monotonic", side_effect=[0.0, 16.0]), \
                    contextlib.redirect_stderr(stderr):
                result = loop_launcher.main([
                    "--workspace-root", str(workspace_root),
                    "stop", "slow-stop", "--now",
                ])

            self.assertEqual(result, 1)
            self.assertIn("未自動 SIGKILL", stderr.getvalue())
            safe_kill.assert_called_once_with(4242, cli.signal.SIGINT)


if __name__ == "__main__":
    unittest.main()
