"""可安裝 CLI、Dashboard parallel backend 契約與 package runtime 資源回歸測試。"""
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from engine import cli, dashboard, loop, paths, status


class TestInstalledCli(unittest.TestCase):
    def test_dashboard_is_forwarded_and_parallel_commands_are_public(self):
        with mock.patch.object(dashboard, "run_dashboard", return_value=0) as run:
            result = cli.main(["dashboard", "--name", "demo", "--port", "9000"])
        self.assertEqual(result, 0)
        run.assert_called_once_with(name="demo", port=9000)
        self.assertEqual(cli.build_parser().parse_args(["status"]).command, "status")
        self.assertEqual(cli.build_parser().parse_args(["fleet"]).command, "fleet")
        with mock.patch("engine.status.main", return_value=0) as status_main:
            self.assertEqual(cli.main(["status", "--all", "--json"]), 0)
        status_main.assert_called_once_with(["--all", "--json"])

    def test_runtime_assets_are_inside_engine_package(self):
        package = files("engine")
        for relative in (
            "dashboard.config.shared.json",
            "prompts/plan.md",
            "prompts/exec.md",
            "prompts/external-agent-base.md",
            "prompts/external-agent-goal.md",
            "prompts/external-agent-plan.md",
            "prompts/external-agent-missing.md",
            "prompts/external-agent-default-context.md",
            "prompts/external-agent-team-template-example.md",
            "ui/index.html",
        ):
            with self.subTest(relative=relative):
                self.assertTrue(package.joinpath(relative).is_file())

    def test_wheel_style_defaults_use_user_data_not_package_directory(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(paths, "CHECKOUT_ROOT", None), \
                mock.patch.object(paths, "USER_DATA_ROOT", Path(directory)), \
                mock.patch.dict("os.environ", {}, clear=True):
            root = Path(directory).resolve()
            self.assertEqual(paths.default_workspace_root(), root / "workspace")
            self.assertEqual(paths.default_personal_config(), root / "dashboard.config.local.json")


class ResponseCapture:
    def __init__(self):
        self.response = None

    def _out(self, code, body, _ctype="application/json; charset=utf-8"):
        self.response = code, json.loads(body)

    def _err(self, message, code=400):
        self.response = code, {"error": message}


class TestParallelDashboardBackend(unittest.TestCase):
    RUN_ID = "a" * 32
    SESSION_ID = "b" * 32

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.old_dashboard_root = dashboard.ROOT
        self.old_loop_root = loop.WORKSPACE_ROOT
        dashboard.ROOT = self.root
        loop.WORKSPACE_ROOT = self.root
        with dashboard.JOBS_LOCK:
            dashboard.JOBS.clear()

    def tearDown(self):
        with dashboard.JOBS_LOCK:
            dashboard.JOBS.clear()
        dashboard.ROOT = self.old_dashboard_root
        loop.WORKSPACE_ROOT = self.old_loop_root
        self.temp.cleanup()

    def write_state(self, name, state):
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        loop.write_checkpointed_state(
            directory / "state.json", json.dumps(state, ensure_ascii=False).encode("utf-8"))
        return directory

    def parent_state(self, name="parallel"):
        state = loop.Workspace.__new__(loop.Workspace).fresh_state("fleet-parent", self.RUN_ID)
        state["config"] = {"repo": str(Path(self.temp.name) / "repo")}
        return self.write_state(name, state)

    def fleet(self, *, phase="stopped", plan=None, tracks=None, error=None):
        value = {
            "schema_version": 1, "workspace_kind": "fleet-parent", "run_id": self.RUN_ID,
            "phase": phase, "plan": plan or [], "tracks": tracks or [], "merge_queue": [],
            "order_map": {}, "plan_generation": 4, "plan_sha256": "plan-hash",
            "expected_integration_sha": "1" * 40,
            "loop": {"pid": None, "session_id": self.SESSION_ID},
            "config": {"repo": str(Path(self.temp.name) / "repo"), "agent_cmd": "true",
                       "validate_cmd": "true", "validate_timeout": 10},
        }
        if error is not None:
            value["error"] = error
        if phase == "failed":
            value["resume_phase"] = "exec"
        return value

    def write_fleet(self, directory, value, filename="fleet.json"):
        loop.atomic_write_bytes(directory / filename, json.dumps(value).encode("utf-8"))

    def test_fleet_read_error_is_distinct_from_legitimate_failed_error_and_checkpoint_fallback(self):
        parent = self.parent_state()
        failed = self.fleet(phase="failed", error="integration validate failed")
        self.write_fleet(parent, failed)
        projection = dashboard.read_parallel_run("parallel")
        self.assertEqual(projection["error"], "integration validate failed")
        self.assertNotIn("read_error", projection)

        (parent / "fleet.json").write_text("broken", encoding="utf-8")
        self.write_fleet(parent, failed, "fleet.last-good.json")
        recovered = dashboard.read_parallel_run("parallel")
        self.assertTrue(recovered["fleet_recovery_pending"])
        self.assertEqual(recovered["error"], "integration validate failed")

        (parent / "fleet.json").unlink()
        (parent / "fleet.last-good.json").unlink()
        self.assertIn("read_error", dashboard.read_parallel_run("parallel"))
        workspace = next(item for item in dashboard.list_workspaces() if item["name"] == "parallel")
        self.assertIn("error", workspace)
        self.assertEqual(dashboard.fleet_health_projection([workspace])["status"], "error")
        with self.assertRaisesRegex(ValueError, "fleet truth"):
            status.project_status("parallel")

    def test_parallel_projection_rejects_unknown_phase_and_exposes_audit_fields(self):
        parent = self.parent_state()
        invalid = self.fleet(phase="future-phase")
        self.write_fleet(parent, invalid)
        self.assertIn("read_error", dashboard.read_parallel_run("parallel"))
        with self.assertRaisesRegex(ValueError, "phase"):
            status.project_status("parallel")

        invalid_failed = self.fleet(phase="failed", error="broken")
        invalid_failed.pop("resume_phase")
        self.write_fleet(parent, invalid_failed)
        self.assertIn("read_error", dashboard.read_parallel_run("parallel"))

        valid = self.fleet(phase="stopped", tracks=[{
            "name": "alpha", "status": "cleaned", "child_workspace": "parallel--alpha",
            "last_integration_error": "rollback failed once",
            "event_history": [{"event": "cleaned", "at": "now"}],
        }])
        valid["stop_reason"] = "operator requested"
        valid["error"] = "last fleet failure"
        self.write_fleet(parent, valid)
        projected, error = dashboard.project_state_for_ui("parallel")
        self.assertIsNone(error)
        self.assertEqual(projected["parallel_error"], "last fleet failure")
        self.assertEqual(projected["parallel_stop_reason"], "operator requested")
        self.assertEqual(projected["parallel_track_events"][0]["track"], "alpha")
        issue = next(item for item in projected["issues"] if item.get("synthetic"))
        self.assertTrue(issue["resolved"])
        self.assertTrue(issue["read_only"])
        self.assertEqual(issue["child_workspace"], "parallel--alpha")

    def test_rollback_journal_is_unresolved_issue_in_summary_health_and_status(self):
        parent = self.parent_state()
        fleet = self.fleet(phase="merging", tracks=[{
            "name": "alpha", "status": "merging",
            "child_workspace": "parallel--alpha",
        }])
        fleet["merge_tx"] = {"track": "alpha", "stage": "rollback-prepared",
                             "validation_error": "integration candidate is red"}
        self.write_fleet(parent, fleet)
        projected, error = dashboard.project_state_for_ui("parallel")
        self.assertIsNone(error)
        issue = next(item for item in projected["issues"] if item.get("synthetic"))
        self.assertFalse(issue["resolved"])
        self.assertEqual(issue["track"], "alpha")
        summary = next(item for item in dashboard.list_workspaces()
                       if item["name"] == "parallel")
        self.assertEqual(summary["unread_issues"], 1)
        self.assertEqual(dashboard.fleet_health_projection([summary])["status"], "degraded")
        status_projection = status.project_status("parallel")
        self.assertEqual(status_projection["unread_issues"], 1)

        fleet["tracks"][0].update(status="cleaned",
                                  last_integration_error="integration candidate is red")
        fleet["merge_tx"] = None
        fleet["phase"] = "done"
        self.write_fleet(parent, fleet)
        resolved, error = dashboard.project_state_for_ui("parallel")
        self.assertIsNone(error)
        resolved_issue = next(item for item in resolved["issues"] if item.get("synthetic"))
        self.assertTrue(resolved_issue["resolved"])

    def test_spawn_fleet_passes_red_and_stall_limits(self):
        process = SimpleNamespace(pid=123, stdout=io.StringIO(), poll=lambda: 0,
                                  returncode=0)
        with mock.patch.object(dashboard.subprocess, "Popen", return_value=process) as popen:
            dashboard.spawn_fleet("parallel", "/repo", "agent", "validate",
                                  red_limit=37, stall_limit=411)
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--red-limit") + 1], "37")
        self.assertEqual(command[command.index("--stall-limit") + 1], "411")
        dashboard.JOBS.pop("parallel", None)

    def test_parent_progress_uses_master_orders_and_valid_child_identity(self):
        parent = self.parent_state()
        plan = [{"order": 1, "task": "alpha first", "track": "alpha"},
                {"order": 2, "task": "beta done", "track": "beta"}]
        tracks = [
            {"name": "alpha", "status": "running", "child_workspace": "parallel--alpha"},
            {"name": "beta", "status": "cleaned", "child_workspace": "parallel--beta", "tip": "2" * 40},
        ]
        fleet = self.fleet(phase="exec", plan=plan, tracks=tracks)
        fleet["order_map"] = {"alpha": {"1": 1}, "beta": {"1": 2}}
        self.write_fleet(parent, fleet)
        child = loop.Workspace.__new__(loop.Workspace).fresh_state("fleet-child", self.RUN_ID)
        child.update(fleet_parent="parallel", fleet_parent_session_id=self.SESSION_ID,
                     track="alpha", merge_target_ref="refs/heads/main", current_order=1,
                     plan=[{"order": 1, "task": "alpha first", "track": "alpha"}])
        self.write_state("parallel--alpha", child)
        projected, error = dashboard.project_state_for_ui("parallel")
        self.assertIsNone(error)
        self.assertEqual([entry["order"] for entry in projected["completed"]], [2])
        self.assertEqual(projected["current_order"], 1)
        self.assertEqual(projected["parallel_current_orders"], [1])

    def test_awaiting_approval_run_rejects_stale_plan_identity(self):
        parent = self.parent_state()
        fleet = self.fleet(phase="awaiting-approval")
        self.write_fleet(parent, fleet)
        config = {"agent_cmds": [{"cmd": "true"}], "defaults": {}}
        stale = ResponseCapture()
        with mock.patch.object(dashboard, "load_config", return_value=config), \
                mock.patch.object(dashboard, "spawn_fleet_resume") as spawn:
            dashboard.Handler.api_run(stale, {"name": "parallel", "run_id": self.RUN_ID,
                                              "plan_generation": 3, "plan_sha256": "old"})
        self.assertEqual(stale.response[0], 409)
        spawn.assert_not_called()

        current = ResponseCapture()
        process = SimpleNamespace(pid=456)
        with mock.patch.object(dashboard, "load_config", return_value=config), \
                mock.patch.object(dashboard, "spawn_fleet_resume", return_value=process) as spawn:
            dashboard.Handler.api_run(current, {"name": "parallel", "run_id": self.RUN_ID,
                                                "plan_generation": 4, "plan_sha256": "plan-hash"})
        self.assertEqual(current.response[0], 200)
        self.assertTrue(current.response[1]["starting"])
        spawn.assert_called_once()

    def test_legitimate_failed_fleet_remains_resumable(self):
        parent = self.parent_state()
        self.write_fleet(parent, self.fleet(phase="failed", error="integration validate failed"))
        handler = ResponseCapture()
        config = {"agent_cmds": [{"cmd": "true"}], "defaults": {}}
        process = SimpleNamespace(pid=789)
        with mock.patch.object(dashboard, "load_config", return_value=config), \
                mock.patch.object(dashboard, "spawn_fleet_resume", return_value=process) as spawn:
            dashboard.Handler.api_run(handler, {"name": "parallel", "run_id": self.RUN_ID})
        self.assertEqual(handler.response[0], 200)
        self.assertTrue(handler.response[1]["starting"])
        spawn.assert_called_once()

    def test_fleet_startup_handshake_uses_persisted_fleet_pid(self):
        process = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(5)"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            parent = self.parent_state()
            fleet = self.fleet(phase="exec")
            fleet["loop"]["pid"] = process.pid
            self.write_fleet(parent, fleet)
            dashboard.JOBS["parallel"] = dashboard.Job(
                "parallel", str(fleet["config"]["repo"]), process, kind="fleet")
            startup = dashboard.job_startup_status("parallel", process.pid)
            self.assertEqual(startup, {"status": "ready", "pid": process.pid,
                                       "run_id": self.RUN_ID})
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            dashboard.JOBS.pop("parallel", None)

    def test_fleet_child_rejects_agent_cli_tests_cancel_and_stop(self):
        child = loop.Workspace.__new__(loop.Workspace).fresh_state("fleet-child", self.RUN_ID)
        child.update(fleet_parent="parallel", fleet_parent_session_id=self.SESSION_ID,
                     track="alpha", merge_target_ref="refs/heads/main")
        self.write_state("parallel--alpha", child)
        for method, body in (
                (dashboard.Handler.api_test_agent, {"name": "parallel--alpha", "agent_idx": 0}),
                (dashboard.Handler.api_test_cli, {"name": "parallel--alpha", "agent_cmd": "true"}),
                (dashboard.Handler.api_cancel_drain, {"name": "parallel--alpha"}),
                (dashboard.Handler.api_stop, {"name": "parallel--alpha"})):
            with self.subTest(method=method.__name__):
                handler = ResponseCapture()
                method(handler, body)
                self.assertEqual(handler.response[0], 409)

    def make_repo(self):
        repo = Path(self.temp.name) / "repo"
        repo.mkdir()
        for command in (("git", "init", "-q"), ("git", "symbolic-ref", "HEAD", "refs/heads/main"),
                        ("git", "config", "user.email", "test@example.invalid"),
                        ("git", "config", "user.name", "test")):
            subprocess.run(command, cwd=repo, check=True)
        (repo / "goal.md").write_text("goal\n", encoding="utf-8")
        subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
        return repo

    def test_group_delete_preflights_every_track_before_removing_any_worktree(self):
        repo = self.make_repo()
        parent = self.parent_state()
        session = "c" * 32
        tracks = []
        for track_name in ("alpha", "beta"):
            worktree = parent / "worktrees" / track_name
            branch = f"refs/heads/loop/{self.RUN_ID}/{track_name}"
            subprocess.run(["git", "worktree", "add", "-b", branch.removeprefix("refs/heads/"),
                            str(worktree)], cwd=repo, check=True, capture_output=True)
            child_name = f"parallel--{track_name}"
            child = loop.Workspace.__new__(loop.Workspace).fresh_state("fleet-child", self.RUN_ID)
            child.update(fleet_parent="parallel", fleet_parent_session_id=session,
                         track=track_name, merge_target_ref="refs/heads/main",
                         config={"repo": str(worktree), "agent_cmd": "true",
                                 "validate_cmd": "true"})
            self.write_state(child_name, child)
            tracks.append({"name": track_name, "safe_name": track_name, "status": "stopped",
                           "worktree": str(worktree), "branch_ref": branch,
                           "child_workspace": child_name})
        fleet = self.fleet(phase="stopped", tracks=tracks)
        fleet.update(integration_worktree=str(repo), integration_ref="refs/heads/main")
        fleet["supervisor_session_history"] = [session, self.SESSION_ID]
        fleet["config"]["repo"] = str(repo)
        self.write_fleet(parent, fleet)
        dirty = Path(tracks[1]["worktree"]) / "dirty.txt"
        dirty.write_text("preserve\n", encoding="utf-8")
        handler = ResponseCapture()
        dashboard.Handler.api_delete_workspace(handler, {"name": "parallel", "run_id": self.RUN_ID})
        self.assertEqual(handler.response[0], 409)
        self.assertTrue(parent.exists())
        self.assertTrue(all(Path(track["worktree"]).exists() for track in tracks))

        tracks[0]["cleanup_stage"] = "child-removing"
        self.write_fleet(parent, fleet)
        removing = ResponseCapture()
        dashboard.Handler.api_delete_workspace(
            removing, {"name": "parallel", "run_id": self.RUN_ID})
        self.assertEqual(removing.response[0], 409)
        self.assertTrue(parent.exists())
        tracks[0].pop("cleanup_stage")
        self.write_fleet(parent, fleet)
        self.assertEqual(dirty.read_text(encoding="utf-8"), "preserve\n")
        dirty.unlink()

        tampered_child = loop.Workspace.__new__(loop.Workspace).fresh_state("fleet-child", self.RUN_ID)
        tampered_child.update(fleet_parent="parallel", fleet_parent_session_id=session,
                              track="alpha", merge_target_ref="refs/heads/main", plan=[],
                              config={"repo": str(repo), "agent_cmd": "true",
                                      "validate_cmd": "true"})
        self.write_state("parallel--alpha", tampered_child)
        tampered = ResponseCapture()
        dashboard.Handler.api_delete_workspace(
            tampered, {"name": "parallel", "run_id": self.RUN_ID})
        self.assertEqual(tampered.response[0], 409)
        self.assertTrue(parent.exists())
        self.assertTrue(all(Path(track["worktree"]).exists() for track in tracks))
        restored_alpha = loop.Workspace.__new__(loop.Workspace).fresh_state("fleet-child", self.RUN_ID)
        restored_alpha.update(fleet_parent="parallel", fleet_parent_session_id=session,
                              track="alpha", merge_target_ref="refs/heads/main", plan=[],
                              config={"repo": tracks[0]["worktree"], "agent_cmd": "true",
                                      "validate_cmd": "true"})
        self.write_state("parallel--alpha", restored_alpha)

        unknown_child = loop.Workspace.__new__(loop.Workspace).fresh_state("fleet-child", self.RUN_ID)
        unknown_child.update(fleet_parent="parallel", fleet_parent_session_id="d" * 32,
                             track="beta", merge_target_ref="refs/heads/main",
                             config={"repo": tracks[1]["worktree"], "agent_cmd": "true",
                                     "validate_cmd": "true"})
        self.write_state("parallel--beta", unknown_child)
        unknown = ResponseCapture()
        dashboard.Handler.api_delete_workspace(
            unknown, {"name": "parallel", "run_id": self.RUN_ID})
        self.assertEqual(unknown.response[0], 409)
        self.assertTrue(parent.exists())
        restored_child = loop.Workspace.__new__(loop.Workspace).fresh_state("fleet-child", self.RUN_ID)
        restored_child.update(fleet_parent="parallel", fleet_parent_session_id=session,
                              track="beta", merge_target_ref="refs/heads/main",
                              config={"repo": tracks[1]["worktree"], "agent_cmd": "true",
                                      "validate_cmd": "true"})
        self.write_state("parallel--beta", restored_child)

        alpha_path = self.root / "parallel--alpha"
        preserved_alpha = Path(self.temp.name) / "preserved-alpha"

        def mutate_child_state(stage, candidate):
            if stage == "child-before-lock" and candidate == "parallel--alpha":
                changed = json.loads((alpha_path / "state.json").read_text(encoding="utf-8"))
                changed["config"]["repo"] = str(repo)
                loop.write_checkpointed_state(
                    alpha_path / "state.json", json.dumps(changed).encode("utf-8"))

        state_raced = ResponseCapture()
        with mock.patch.object(dashboard, "_delete_race_hook", side_effect=mutate_child_state):
            dashboard.Handler.api_delete_workspace(
                state_raced, {"name": "parallel", "run_id": self.RUN_ID})
        self.assertEqual(state_raced.response[0], 409)
        self.assertIn("locked state", state_raced.response[1]["error"])
        self.assertTrue(parent.exists())
        self.write_state("parallel--alpha", restored_alpha)

        def replace_child(stage, candidate):
            if stage == "child-before-lock" and candidate == "parallel--alpha":
                alpha_path.rename(preserved_alpha)
                shutil.copytree(preserved_alpha, alpha_path)
                (alpha_path / ".run.lock").unlink(missing_ok=True)
                (alpha_path / "replacement-marker.txt").write_text(
                    "must survive\n", encoding="utf-8")

        raced = ResponseCapture()
        with mock.patch.object(dashboard, "_delete_race_hook", side_effect=replace_child):
            dashboard.Handler.api_delete_workspace(
                raced, {"name": "parallel", "run_id": self.RUN_ID})
        self.assertEqual(raced.response[0], 409)
        self.assertEqual((alpha_path / "replacement-marker.txt").read_text(), "must survive\n")
        self.assertFalse((alpha_path / ".run.lock").exists())
        self.assertTrue(parent.exists())
        shutil.rmtree(alpha_path)
        preserved_alpha.rename(alpha_path)

        active_child = ResponseCapture()
        with mock.patch.object(
                dashboard, "ws_running",
                side_effect=lambda candidate, _state: candidate == "parallel--beta"):
            dashboard.Handler.api_delete_workspace(
                active_child, {"name": "parallel", "run_id": self.RUN_ID})
        self.assertEqual(active_child.response[0], 409)
        self.assertTrue(parent.exists())
        self.assertTrue(all(Path(track["worktree"]).exists() for track in tracks))

        worktree_faulted = False

        def fail_after_first_worktree(stage, _path):
            nonlocal worktree_faulted
            if not worktree_faulted and stage == "after-remove":
                worktree_faulted = True
                raise OSError("injected after first worktree removal")

        partial_git = ResponseCapture()
        with mock.patch.object(dashboard, "_delete_worktree_hook",
                               side_effect=fail_after_first_worktree):
            dashboard.Handler.api_delete_workspace(
                partial_git, {"name": "parallel", "run_id": self.RUN_ID})
        self.assertEqual(partial_git.response[0], 409)
        self.assertTrue(dashboard._delete_journal_path("parallel").is_file())
        self.assertEqual(sum(Path(track["worktree"]).exists() for track in tracks), 1)

        delete_faulted = False

        def fail_mid_child_delete(stage, candidate):
            nonlocal delete_faulted
            if (not delete_faulted and stage == "after-unlink" and
                    candidate == "parallel--alpha"):
                delete_faulted = True
                raise OSError("injected mid child delete")

        partial_workspace = ResponseCapture()
        with mock.patch.object(dashboard, "_delete_fault_hook",
                               side_effect=fail_mid_child_delete):
            dashboard.Handler.api_delete_workspace(
                partial_workspace, {"name": "parallel", "run_id": self.RUN_ID})
        self.assertEqual(partial_workspace.response[0], 409)
        self.assertTrue(dashboard._delete_journal_path("parallel").is_file())

        clear_faulted = False

        def fail_group_journal_clear(stage, candidate):
            nonlocal clear_faulted
            if (not clear_faulted and stage == "before-journal-clear" and
                    candidate == "parallel"):
                clear_faulted = True
                raise OSError("injected group journal clear failure")

        late_failure = ResponseCapture()
        with mock.patch.object(dashboard, "_delete_fault_hook",
                               side_effect=fail_group_journal_clear):
            dashboard.Handler.api_delete_workspace(
                late_failure, {"name": "parallel", "run_id": self.RUN_ID})
        self.assertEqual(late_failure.response[0], 409)
        self.assertFalse(parent.exists())
        self.assertTrue(dashboard._delete_journal_path("parallel").is_file())

        success = ResponseCapture()
        dashboard.Handler.api_delete_workspace(
            success, {"name": "parallel", "run_id": self.RUN_ID})
        self.assertEqual(success.response[0], 200)
        self.assertTrue(success.response[1]["resumed_delete"])
        self.assertFalse(parent.exists())
        self.assertFalse(dashboard._delete_journal_path("parallel").exists())
        self.assertTrue(all(subprocess.run(
            ["git", "show-ref", "--verify", track["branch_ref"]], cwd=repo,
            capture_output=True).returncode == 0 for track in tracks))

    def test_parallel_launch_rejects_any_existing_workspace_before_repo_or_plan_mutation(self):
        repo = self.make_repo()
        state = loop.Workspace.__new__(loop.Workspace).fresh_state()
        self.write_state("parallel", state)
        before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                text=True, capture_output=True).stdout.strip()
        handler = ResponseCapture()
        config = {"agent_cmds": [{"cmd": "true"}], "validate_cmds": [{"cmd": "true"}],
                  "defaults": {}, "notify_cmd": ""}
        with mock.patch.object(dashboard, "load_config", return_value=config), \
                mock.patch.object(dashboard, "spawn_fleet") as spawn:
            dashboard.Handler.api_launch(handler, {
                "name": "parallel", "repo": str(repo), "agent_idx": 0, "validate_idx": 0,
                "parallel": True, "goal_content": "changed\n",
            })
        self.assertEqual(handler.response[0], 409)
        spawn.assert_not_called()
        self.assertEqual((repo / "goal.md").read_text(encoding="utf-8"), "goal\n")
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                        text=True, capture_output=True).stdout.strip(), before)

    def test_parallel_launch_stages_import_plan_outside_fleet_workspace(self):
        repo = self.make_repo()
        plan = [
            {"order": 1, "task": "alpha task", "track": "alpha"},
            {"order": 2, "task": "beta task", "track": "beta"},
        ]
        handler = ResponseCapture()
        config = {"agent_cmds": [{"cmd": "true"}], "validate_cmds": [{"cmd": "true"}],
                  "defaults": {}, "notify_cmd": ""}
        process = SimpleNamespace(pid=987)
        with mock.patch.object(dashboard, "load_config", return_value=config), \
                mock.patch.object(dashboard, "spawn_fleet", return_value=process) as spawn:
            dashboard.Handler.api_launch(handler, {
                "name": "parallel", "repo": str(repo), "agent_idx": 0, "validate_idx": 0,
                "parallel": True, "plan_json": json.dumps(plan), "start_phase": "exec",
            })
        self.assertEqual(handler.response[0], 200)
        import_plan = Path(spawn.call_args.kwargs["import_plan"])
        self.assertEqual(import_plan.parent, self.root / ".launch-inputs")
        self.assertFalse((self.root / "parallel").exists())
        normalized, errors = dashboard.validate_plan(plan)
        self.assertFalse(errors)
        self.assertEqual(json.loads(import_plan.read_text(encoding="utf-8")), normalized)


if __name__ == "__main__":
    unittest.main()
