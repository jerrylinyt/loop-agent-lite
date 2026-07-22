"""Real-Git coverage for Dashboard launcher ownership and manual recovery."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from engine import cli
from engine import dashboard
from engine import loop as loop_mod
from engine import platform_compat as compat
from engine import repo_owner


def _git(repo: Path, *args: str, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check)


def _make_repo(root: Path, name="repo") -> Path:
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Launcher Owner Test")
    _git(repo, "config", "user.email", "launcher-owner@example.invalid")
    (repo / "goal.md").write_text("old goal\n", encoding="utf-8")
    _git(repo, "add", "goal.md")
    _git(repo, "commit", "-qm", "initial")
    return repo.resolve()


def _dashboard_config() -> dict:
    return {
        "agent_cmds": [{"label": "agent", "cmd": "agent --test"}],
        "validate_cmds": [{"label": "validate", "cmd": "validator --test"}],
        "defaults": {"validate_timeout": 5},
        "notify_cmd": "",
        "extra_path_dirs": [],
        "ralph": {
            "scripts": [], "default_iterations": 1,
            "default_args_style": "positional",
            "default_usage_limit_action": "off",
            "default_fallback_models": [], "default_auto_restart_max": 0,
            "usage_limit_patterns": [],
        },
    }


class _Handler:
    def __init__(self):
        self.response = None

    def _out(self, code, body, _ctype="application/json; charset=utf-8"):
        self.response = code, json.loads(body)

    def _err(self, message, code=400):
        self.response = code, {"error": str(message)}


class DashboardLauncherOwnerTest(unittest.TestCase):
    def _foreign_owner(self, repo: Path, workspace: Path):
        fence = repo_owner.RepoOwnerFence.claim(
            repo,
            owner_kind=repo_owner.OwnerKind.LOOP,
            workspace=workspace,
            state_path=workspace / "state.json",
        )
        self.addCleanup(fence.close)
        return fence

    def test_foreign_owner_blocks_loop_before_branch_goal_plan_or_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _make_repo(root)
            workspace_root = root / "workspaces"
            workspace = workspace_root / "blocked-loop"
            branch_before = _git(repo, "branch", "--show-current").stdout.strip()
            head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
            goal_before = (repo / "goal.md").read_bytes()
            fence = self._foreign_owner(repo, workspace)
            fence.close()  # simulate a crashed owner; only the active marker remains
            handler = _Handler()
            with mock.patch.object(dashboard, "ROOT", workspace_root), \
                    mock.patch.object(loop_mod, "WORKSPACE_ROOT", workspace_root), \
                    mock.patch.object(dashboard, "load_config", return_value=_dashboard_config()), \
                    mock.patch.object(dashboard, "command_error", return_value=None), \
                    mock.patch.object(dashboard, "spawn_loop") as spawn, \
                    mock.patch.dict(dashboard.JOBS, {}, clear=True):
                dashboard.Handler.api_launch(handler, {
                    "repo": str(repo), "name": "blocked-loop",
                    "agent_idx": 0, "validate_idx": 0, "new_branch": True,
                    "goal_content": "new goal\n", "start_phase": "exec",
                    "plan_json": json.dumps([
                        {"order": 1, "task": "must not persist", "ref": None},
                    ]),
                })
            self.assertEqual(handler.response[0], 400)
            self.assertIn("owner", handler.response[1]["error"])
            spawn.assert_not_called()
            self.assertEqual(_git(repo, "branch", "--show-current").stdout.strip(), branch_before)
            self.assertEqual(_git(repo, "rev-parse", "HEAD").stdout.strip(), head_before)
            self.assertEqual((repo / "goal.md").read_bytes(), goal_before)
            self.assertFalse(workspace.exists())
            self.assertNotEqual(
                _git(repo, "rev-parse", "--verify", "--quiet", "loop/blocked-loop",
                     check=False).returncode, 0)

    def test_foreign_owner_blocks_ralph_before_branch_prd_or_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _make_repo(root)
            workspace_root = root / "workspaces"
            workspace = workspace_root / "blocked-ralph"
            branch_before = _git(repo, "branch", "--show-current").stdout.strip()
            fence = self._foreign_owner(repo, workspace)
            fence.close()
            handler = _Handler()
            with mock.patch.object(dashboard, "ROOT", workspace_root), \
                    mock.patch.object(loop_mod, "WORKSPACE_ROOT", workspace_root), \
                    mock.patch.object(dashboard, "load_config", return_value=_dashboard_config()), \
                    mock.patch.object(dashboard, "command_error", return_value=None), \
                    mock.patch.object(dashboard, "spawn_ralph") as spawn, \
                    mock.patch.dict(dashboard.JOBS, {}, clear=True):
                dashboard.Handler.api_launch_ralph(handler, {
                    "runner": "ralph", "repo": str(repo), "name": "blocked-ralph",
                    "ralph_custom": "ralph --test", "ralph_dir": str(repo),
                    "iterations": 1, "tool": "claude", "model": "",
                    "args_style": "positional", "new_branch": True,
                    "prd_path": "prd.json", "prd_content": "{}\n",
                })
            self.assertEqual(handler.response[0], 400)
            spawn.assert_not_called()
            self.assertEqual(_git(repo, "branch", "--show-current").stdout.strip(), branch_before)
            self.assertFalse((repo / "prd.json").exists())
            self.assertFalse(workspace.exists())

    def test_foreign_owner_blocks_parallel_pending_plan_and_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _make_repo(root)
            workspace_root = root / "workspaces"
            workspace = workspace_root / "blocked-parallel"
            fence = self._foreign_owner(repo, workspace)
            fence.close()
            handler = _Handler()
            with mock.patch.object(dashboard, "ROOT", workspace_root), \
                    mock.patch.object(loop_mod, "WORKSPACE_ROOT", workspace_root), \
                    mock.patch.object(dashboard, "load_config", return_value=_dashboard_config()), \
                    mock.patch.object(dashboard, "command_error", return_value=None), \
                    mock.patch.object(dashboard, "spawn_parallel") as spawn, \
                    mock.patch.dict(dashboard.JOBS, {}, clear=True):
                dashboard.Handler.api_launch_parallel(handler, {
                    "repo": str(repo), "name": "blocked-parallel",
                    "start_phase": "exec", "agent_idx": 0, "validate_idx": 0,
                    "plan_json": json.dumps([
                        {"order": 1, "task": "must not persist", "ref": None, "stack": 1},
                    ]),
                })
            self.assertEqual(handler.response[0], 400)
            spawn.assert_not_called()
            self.assertFalse(workspace.exists())

    def test_controlled_git_is_checkpointed_and_terminal_before_raw_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _make_repo(root)
            workspace_root = root / "workspaces"
            observed = {}

            def fake_spawn(*_args, **kwargs):
                observed.update(repo_owner.RepoOwnerFence.inspect(repo))
                self.assertTrue(kwargs["workspace_prepared"])
                return SimpleNamespace(pid=777)

            handler = _Handler()
            with mock.patch.object(dashboard, "ROOT", workspace_root), \
                    mock.patch.object(loop_mod, "WORKSPACE_ROOT", workspace_root), \
                    mock.patch.object(dashboard, "load_config", return_value=_dashboard_config()), \
                    mock.patch.object(dashboard, "command_error", return_value=None), \
                    mock.patch.object(dashboard, "spawn_loop", side_effect=fake_spawn), \
                    mock.patch.dict(dashboard.JOBS, {}, clear=True):
                dashboard.Handler.api_launch(handler, {
                    "repo": str(repo), "name": "owned-loop",
                    "agent_idx": 0, "validate_idx": 0, "new_branch": True,
                    "goal_content": "owned goal\n",
                })
            self.assertEqual(handler.response[0], 200, handler.response)
            self.assertEqual(observed["state"], "terminal")
            self.assertEqual(observed["owner_kind"], "dashboard-launcher")
            self.assertEqual(observed["child_state"], "idle")
            self.assertGreaterEqual(observed["child_generation"], 5)
            self.assertEqual(_git(repo, "branch", "--show-current").stdout.strip(),
                             "loop/owned-loop")
            self.assertEqual((repo / "goal.md").read_text(encoding="utf-8"), "owned goal\n")
            self.assertEqual(_git(repo, "status", "--porcelain").stdout, "")

    def test_ralph_git_is_controlled_and_terminal_before_raw_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _make_repo(root)
            workspace_root = root / "workspaces"
            observed = {}

            def fake_spawn(*_args, **kwargs):
                observed.update(repo_owner.RepoOwnerFence.inspect(repo))
                self.assertTrue(kwargs["workspace_prepared"])
                return SimpleNamespace(pid=778)

            handler = _Handler()
            with mock.patch.object(dashboard, "ROOT", workspace_root), \
                    mock.patch.object(loop_mod, "WORKSPACE_ROOT", workspace_root), \
                    mock.patch.object(dashboard, "load_config", return_value=_dashboard_config()), \
                    mock.patch.object(dashboard, "command_error", return_value=None), \
                    mock.patch.object(dashboard, "spawn_ralph", side_effect=fake_spawn), \
                    mock.patch.dict(dashboard.JOBS, {}, clear=True):
                dashboard.Handler.api_launch_ralph(handler, {
                    "repo": str(repo), "name": "owned-ralph",
                    "ralph_custom": "ralph --test", "ralph_dir": str(repo),
                    "iterations": 1, "tool": "claude", "model": "",
                    "args_style": "positional", "new_branch": True,
                    "prd_path": "prd.json", "prd_content": "{\"ok\": true}\n",
                })
            self.assertEqual(handler.response[0], 200, handler.response)
            self.assertEqual(observed["state"], "terminal")
            self.assertEqual(observed["owner_kind"], "dashboard-launcher")
            self.assertEqual(observed["child_state"], "idle")
            self.assertGreaterEqual(observed["child_generation"], 7)
            self.assertEqual(_git(repo, "branch", "--show-current").stdout.strip(),
                             "ralph/owned-ralph")
            self.assertTrue((repo / "prd.json").is_file())
            self.assertEqual(_git(repo, "status", "--porcelain").stdout, "")

    def test_parallel_pending_is_checkpointed_before_terminal_raw_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _make_repo(root)
            workspace_root = root / "workspaces"
            observed = {}
            events = []
            real_fsync_directory = dashboard.parallel_state._fsync_directory
            real_terminalize = repo_owner.RepoOwnerFence.terminalize

            def observed_fsync_directory(path):
                events.append(("fsync-directory", Path(path)))
                return real_fsync_directory(path)

            def observed_terminalize(fence, reason):
                events.append(("terminalize", reason))
                return real_terminalize(fence, reason)

            def fake_spawn(*_args, **kwargs):
                events.append(("spawn", kwargs["import_plan"]))
                observed["marker"] = repo_owner.RepoOwnerFence.inspect(repo)
                observed["spawn_kwargs"] = kwargs
                self.assertTrue(kwargs["workspace_prepared"])
                return SimpleNamespace(pid=779)

            handler = _Handler()
            with mock.patch.object(dashboard, "ROOT", workspace_root), \
                    mock.patch.object(loop_mod, "WORKSPACE_ROOT", workspace_root), \
                    mock.patch.object(dashboard, "load_config", return_value=_dashboard_config()), \
                    mock.patch.object(dashboard, "command_error", return_value=None), \
                    mock.patch.object(
                        dashboard.parallel_state, "_fsync_directory",
                        side_effect=observed_fsync_directory), \
                    mock.patch.object(
                        repo_owner.RepoOwnerFence, "terminalize",
                        new=observed_terminalize), \
                    mock.patch.object(dashboard, "spawn_parallel", side_effect=fake_spawn), \
                    mock.patch.dict(dashboard.JOBS, {}, clear=True):
                dashboard.Handler.api_launch_parallel(handler, {
                    "repo": str(repo), "name": "owned-parallel",
                    "start_phase": "exec", "agent_idx": 0, "validate_idx": 0,
                    "plan_json": json.dumps([
                        {"order": 1, "task": "frozen", "ref": None, "stack": 1},
                    ]),
                })
            self.assertEqual(handler.response[0], 200, handler.response)
            marker = observed["marker"]
            self.assertEqual(marker["state"], "terminal")
            self.assertEqual(marker["owner_kind"], "parallel-launcher")
            self.assertEqual(marker["child_state"], "idle")
            spawn_kwargs = observed["spawn_kwargs"]
            staged = Path(spawn_kwargs["import_plan"])
            expected_hash = spawn_kwargs["expected_plan_sha256"]
            raw = staged.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), expected_hash)
            self.assertIn(expected_hash, staged.name)
            self.assertEqual(json.loads(raw.decode("utf-8"))[0]["task"], "frozen")
            self.assertFalse(
                (workspace_root / "owned-parallel"
                 / "parallel-plan.pending.json").exists())
            event_names = [event[0] for event in events]
            staged_parent_fsync = [
                index for index, event in enumerate(events)
                if event == ("fsync-directory", staged.parent)
            ]
            self.assertEqual(len(staged_parent_fsync), 1)
            self.assertLess(
                staged_parent_fsync[0],
                event_names.index("terminalize"),
            )
            self.assertLess(
                event_names.index("terminalize"), event_names.index("spawn"))

    def test_reaped_child_error_terminalizes_failed_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _make_repo(root)
            workspace_root = root / "workspaces"
            with mock.patch.object(dashboard, "ROOT", workspace_root):
                def fail_after_reap(fence, _workspace):
                    result = dashboard._run_launcher_git(
                        fence, repo, "status", "--porcelain")
                    self.assertEqual(result.returncode, 0)
                    raise RuntimeError("bounded mutation failed")

                with self.assertRaisesRegex(RuntimeError, "bounded mutation failed"):
                    dashboard._run_dashboard_launcher(
                        repo, "failed-launcher",
                        repo_owner.OwnerKind.DASHBOARD_LAUNCHER,
                        fail_after_reap)
            marker = repo_owner.RepoOwnerFence.inspect(repo)
            self.assertEqual(marker["state"], "terminal")
            self.assertEqual(marker["child_state"], "idle")
            self.assertEqual(marker["terminal_reason"], "dashboard-launcher-failed")

    def test_failed_launcher_quiesces_retained_live_child_before_terminalizing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _make_repo(root)
            workspace_root = root / "workspaces"
            observed = {}

            def fail_with_live_child(fence, _workspace):
                child = fence.spawn_child(
                    repo_owner.ChildKind.TOOL,
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                observed["pid"] = child.pid
                self.assertTrue(compat.process_is_alive(child.pid))
                raise RuntimeError("launcher failed after spawn")

            with mock.patch.object(dashboard, "ROOT", workspace_root):
                with self.assertRaisesRegex(RuntimeError, "failed after spawn"):
                    dashboard._run_dashboard_launcher(
                        repo, "failed-live-child",
                        repo_owner.OwnerKind.DASHBOARD_LAUNCHER,
                        fail_with_live_child)

            self.assertFalse(compat.process_is_alive(observed["pid"]))
            marker = repo_owner.RepoOwnerFence.inspect(repo)
            self.assertEqual(marker["state"], "terminal")
            self.assertEqual(marker["child_state"], "child_reaped")
            self.assertEqual(marker["terminal_reason"], "dashboard-launcher-failed")

    def test_unknown_child_identity_leaves_nonterminal_fail_closed_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = _make_repo(root)
            workspace_root = root / "workspaces"
            with mock.patch.object(dashboard, "ROOT", workspace_root):
                def fail_in_publication_gap(fence, _workspace):
                    fence.begin_child(repo_owner.ChildKind.GIT, ["git", "status"])
                    raise RuntimeError("identity publication uncertain")

                with self.assertRaisesRegex(
                        repo_owner.OwnerBusy, "explicit recovery required"):
                    dashboard._run_dashboard_launcher(
                        repo, "uncertain-launcher",
                        repo_owner.OwnerKind.DASHBOARD_LAUNCHER,
                        fail_in_publication_gap)
            marker = repo_owner.RepoOwnerFence.inspect(repo)
            self.assertEqual(marker["state"], "active")
            self.assertEqual(marker["child_state"], "launching")


class CliRecoverOwnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = _make_repo(self.root)
        self.workspace_root = self.root / "workspaces"
        self.name = "recover-me"
        with mock.patch.object(loop_mod, "WORKSPACE_ROOT", self.workspace_root):
            workspace = loop_mod.Workspace(self.name)
            state = workspace.fresh_state()
            state["config"] = {"repo": str(self.repo)}
            state["repo_binding"] = str(self.repo)
            workspace.save_state(state)
        self.workspace = self.workspace_root / self.name

    def _claim(self, *, live_owner=False, marker_workspace=None):
        identity = (repo_owner.current_owner_identity() if live_owner else {
            "pid": 2_147_483_647,
            "creation_token": "definitely-gone-test-owner",
        })
        fence = repo_owner.RepoOwnerFence.claim(
            self.repo,
            owner_kind=repo_owner.OwnerKind.DASHBOARD_LAUNCHER,
            workspace=marker_workspace or self.workspace,
            state_path=(marker_workspace or self.workspace) / "state.json",
            owner_identity=identity,
            boot_identity=repo_owner.host_boot_identity(),
        )
        self.addCleanup(fence.close)
        return fence

    def _args(self, *, acknowledge=True, repo=None):
        return SimpleNamespace(
            workspace=self.name,
            acknowledge_child_gone=acknowledge,
            repo=repo,
        )

    def _recover(self, args):
        with mock.patch.object(loop_mod, "WORKSPACE_ROOT", self.workspace_root):
            return cli.command_recover_owner(args)

    def test_manual_recovery_generation_cas_audit_and_terminalize(self):
        fence = self._claim()
        fence.begin_child(repo_owner.ChildKind.GIT, ["git", "status"])
        fence.close()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self._recover(self._args()), 0)
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["state"], "terminal")
        self.assertEqual(marker["generation"], 2)
        self.assertEqual(marker["child_state"], "child_reaped")
        self.assertEqual(len(marker["recovery_history"]), 1)
        self.assertEqual(marker["recovery_history"][0]["from_generation"], 1)
        self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_recovery_rejects_without_acknowledgement(self):
        fence = self._claim()
        fence.close()
        before = repo_owner.RepoOwnerFence.inspect(self.repo)
        with self.assertRaisesRegex(ValueError, "acknowledge-child-gone"):
            self._recover(self._args(acknowledge=False))
        self.assertEqual(repo_owner.RepoOwnerFence.inspect(self.repo), before)

    def test_recovery_rejects_exact_live_owner(self):
        fence = self._claim(live_owner=True)
        fence.close()
        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            self._recover(self._args())
        self.assertEqual(repo_owner.RepoOwnerFence.inspect(self.repo)["state"], "active")

    def test_recovery_rejects_live_child_exact_identity(self):
        fence = self._claim()
        generation = fence.begin_child(repo_owner.ChildKind.GIT, ["git", "status"])
        live = repo_owner.current_owner_identity()
        fence.publish_child_running(generation, {
            **live,
            "containment_kind": "job" if compat.IS_WINDOWS else "process-group",
            "containment_id": "job:live-test" if compat.IS_WINDOWS else str(os.getpgrp()),
        })
        fence.close()
        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            self._recover(self._args())
        self.assertEqual(
            repo_owner.RepoOwnerFence.inspect(self.repo)["child_state"], "child_running")

    def test_recovery_rejects_legacy_absent_child_containment(self):
        fence = self._claim()
        generation = fence.begin_child(repo_owner.ChildKind.GIT, ["git", "status"])
        gone_pid = 2_147_483_647
        containment_id = ("job:gone-test" if compat.IS_WINDOWS
                          else str(os.getpid() + 1_000_000))
        identity = {
            "pid": gone_pid,
            "creation_token": "definitely-gone-test-child",
            "containment_kind": "job" if compat.IS_WINDOWS else "process-group",
            "containment_id": containment_id,
        }
        fence.publish_child_running(generation, identity)
        fence.close()
        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            self._recover(self._args())
        self.assertEqual(
            repo_owner.RepoOwnerFence.inspect(self.repo)["child_state"],
            "child_running")

    def test_recovery_accepts_durably_reaped_strict_child(self):
        fence = self._claim()
        generation = fence.begin_child(repo_owner.ChildKind.GIT, ["git", "status"])
        fence.publish_child_running(generation, {
            "pid": 2_147_483_647,
            "creation_token": "definitely-gone-test-child",
            "containment_kind": (
                "windows-job-no-breakaway-v2" if compat.IS_WINDOWS
                else "posix-subreaper-guardian-v2"),
            "containment_id": "strict:test",
        })
        fence.record_child_result(generation, 0)
        fence.close()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self._recover(self._args()), 0)
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["state"], "terminal")
        self.assertEqual(marker["child_result"]["status"], "recovered")

    def test_recovery_rejects_same_boot_idle_after_historical_child(self):
        fence = self._claim()
        generation = fence.begin_child(repo_owner.ChildKind.GIT, ["git", "status"])
        fence.publish_child_running(generation, {
            "pid": 2_147_483_647,
            "creation_token": "definitely-gone-test-child",
            "containment_kind": "job" if compat.IS_WINDOWS else "process-group",
            "containment_id": "legacy:test",
        })
        fence.record_child_result(generation, 0)
        fence.checkpoint_child(generation)
        fence.close()
        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            self._recover(self._args())

    @unittest.skipUnless(compat.IS_WINDOWS, "strict Job proof is Windows-only")
    def test_recovery_accepts_absent_strict_windows_job(self):
        fence = self._claim()
        generation = fence.begin_child(repo_owner.ChildKind.GIT, ["git", "status"])
        fence.publish_child_running(generation, {
            "pid": 2_147_483_647,
            "creation_token": "definitely-gone-test-child",
            "containment_kind": "windows-job-no-breakaway-v2",
            "containment_id": "strict-job:test",
        })
        fence.close()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self._recover(self._args()), 0)

    @unittest.skipIf(compat.IS_WINDOWS, "guardian crash proof is POSIX-only")
    def test_recovery_rejects_absent_strict_posix_guardian_before_reap(self):
        fence = self._claim()
        generation = fence.begin_child(repo_owner.ChildKind.GIT, ["git", "status"])
        fence.publish_child_running(generation, {
            "pid": 2_147_483_647,
            "creation_token": "definitely-gone-test-child",
            "containment_kind": "posix-subreaper-guardian-v2",
            "containment_id": "guardian:test",
        })
        fence.close()
        with self.assertRaises(repo_owner.OwnerRecoveryRequired):
            self._recover(self._args())

    def test_recovery_rejects_dirty_primary(self):
        fence = self._claim()
        fence.close()
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "clean"):
            self._recover(self._args())
        self.assertEqual(repo_owner.RepoOwnerFence.inspect(self.repo)["state"], "active")

    def test_recovery_rejects_workspace_and_state_path_mismatch(self):
        other = self.workspace_root / "other"
        fence = self._claim(marker_workspace=other)
        fence.close()
        with self.assertRaisesRegex(ValueError, "workspace"):
            self._recover(self._args())
        self.assertEqual(repo_owner.RepoOwnerFence.inspect(self.repo)["state"], "active")

    def test_repo_override_is_rejected_when_state_has_binding(self):
        fence = self._claim()
        fence.close()
        with self.assertRaisesRegex(ValueError, "state 已能識別"):
            self._recover(self._args(repo=str(self.repo)))
        self.assertEqual(repo_owner.RepoOwnerFence.inspect(self.repo)["state"], "active")

    def test_repo_override_is_allowed_only_when_state_cannot_identify_repo(self):
        missing_name = "missing-state"
        missing_workspace = self.workspace_root / missing_name
        fence = repo_owner.RepoOwnerFence.claim(
            self.repo,
            owner_kind=repo_owner.OwnerKind.CLI_LAUNCHER,
            workspace=missing_workspace,
            state_path=missing_workspace / "state.json",
            owner_identity={
                "pid": 2_147_483_647,
                "creation_token": "definitely-gone-test-owner",
            },
            boot_identity=repo_owner.host_boot_identity(),
        )
        fence.close()
        args = SimpleNamespace(
            workspace=missing_name,
            acknowledge_child_gone=True,
            repo=str(self.repo),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self._recover(args), 0)
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["state"], "terminal")
        self.assertEqual(marker["owner_kind"], "cli-launcher")


if __name__ == "__main__":
    unittest.main()
