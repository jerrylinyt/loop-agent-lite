"""Parallel CLI/Dashboard/status routing and readonly integration tests."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from engine import cli, dashboard, parallel, status


def _runtime_config(repo: Path) -> dict:
    return {
        "repo": str(repo.resolve()), "goal": "goal.md", "plan_doc": "",
        "agent_cmd": "agent --test", "validate_cmd": "validator --test",
        "flag_threshold": 10, "done_threshold": 3, "red_limit": 20,
        "stall_limit": 300, "stuck_stop": False, "stuck_stop_count": 100,
        "round_timeout": 30.0, "agent_backoff_max": 60.0,
        "validate_timeout": 120.0, "notify_cmd": "", "max_parallel": 2,
        "worker_restart_limit": 3,
    }


def _parallel_state(repo: Path, state_status="paused") -> dict:
    return {
        "runner": "parallel-supervisor", "phase": "exec", "round": 0,
        "flag": 0, "done_count": 0, "plan_version": 1,
        "plan": [{"order": 1, "task": "one", "ref": None, "stack": 1}],
        "completed": [], "current_order": 1, "red_streak": 0,
        "stall_rounds": 0, "issues": [], "config": _runtime_config(repo),
        "loop": {"pid": None},
        "parallel": {
            "run_id": "a" * 32, "status": state_status,
            "terminal_intent": None, "batch": 1,
            "tasks": [{
                "order": 1, "batch": 1, "outcome": "pending",
                "resource_state": "paused", "restart_count": 0, "error": None,
            }],
            "error": None,
        },
    }


class FakeHandler:
    def __init__(self):
        self.responses = []

    def _err(self, message, code=400):
        self.responses.append((code, {"error": message}))

    def _out(self, code, body, _ctype="application/json; charset=utf-8"):
        self.responses.append((code, json.loads(body)))


class TestParallelCliRouting(unittest.TestCase):
    def test_resume_alias_is_available_to_route_parallel_base(self):
        parsed = cli.build_argument_parser().parse_args(["resume", "base"])
        self.assertEqual(parsed.command, "resume")
        self.assertEqual(parsed.name, "base")

    def test_run_and_stop_route_to_parallel_entrypoint(self):
        state = _parallel_state(Path("repo"))
        run_args = SimpleNamespace(
            name="base", reset_state=False, resume_interrupted=False)
        stop_args = SimpleNamespace(name="base", now=False)
        with mock.patch.object(cli, "_workspace_state", return_value=(Path("ws"), state, False)), \
                mock.patch.object(cli, "_exec_parallel") as execute:
            self.assertEqual(cli.command_run(run_args), 0)
            execute.assert_called_once_with("resume", "base")
            execute.reset_mock()
            self.assertEqual(cli.command_stop(stop_args), 0)
            execute.assert_called_once_with("pause", "base")

    def test_parallel_stop_now_cannot_bypass_supervisor(self):
        state = _parallel_state(Path("repo"), "running")
        with mock.patch.object(cli, "_workspace_state", return_value=(Path("ws"), state, False)), \
                mock.patch.object(cli, "_exec_parallel") as execute:
            with self.assertRaisesRegex(ValueError, "--now"):
                cli.command_stop(SimpleNamespace(name="base", now=True))
        execute.assert_not_called()

    def test_managed_worker_rejects_every_cli_mutation(self):
        state = {"runner": "parallel-worker", "managed_readonly": True}
        with self.assertRaisesRegex(ValueError, "readonly"):
            cli.assert_workspace_cli_operation_allowed(state, "config")

    def test_init_cannot_alias_a_managed_worker_repo_under_another_name(self):
        with mock.patch.object(cli, "_managed_workspace_for_repo", return_value="parent--task-1"):
            with self.assertRaisesRegex(ValueError, "parent supervisor"):
                cli.command_init(SimpleNamespace(repo="worker-repo", name="alias"))


class TestParallelDashboardBoundary(unittest.TestCase):
    def test_stop_rejects_live_parallel_job_when_state_is_unreadable(self):
        handler = FakeHandler()
        stop = mock.Mock(return_value=True)
        job = SimpleNamespace(
            name="base", kind="parallel-supervisor", repo="C:/repo",
            popen=SimpleNamespace(pid=4242), alive=lambda: True, stop=stop,
        )
        with mock.patch.dict(dashboard.JOBS, {"base": job}, clear=True), \
                mock.patch.object(
                    dashboard, "read_state", return_value=(None, "corrupt state")), \
                mock.patch.object(dashboard, "spawn_parallel") as spawn:
            dashboard.Handler.api_stop(handler, {"name": "base"})
        self.assertEqual(handler.responses[0][0], 409)
        self.assertIn("拒絕以普通 signal stop", handler.responses[0][1]["error"])
        stop.assert_not_called()
        spawn.assert_not_called()

    def test_stop_routes_live_parallel_job_through_typed_pause(self):
        handler = FakeHandler()
        stop = mock.Mock(return_value=True)
        job = SimpleNamespace(
            name="base", kind="parallel-supervisor", repo="C:/repo",
            popen=SimpleNamespace(pid=4242), alive=lambda: True, stop=stop,
        )
        state = _parallel_state(Path("repo"), "running")
        with mock.patch.dict(dashboard.JOBS, {"base": job}, clear=True), \
                mock.patch.object(dashboard, "read_state", return_value=(state, None)), \
                mock.patch.object(dashboard.Handler, "_parallel_control") as control:
            dashboard.Handler.api_stop(handler, {"name": "base"})
        control.assert_called_once_with(handler, "base", state, "pause")
        stop.assert_not_called()

    def test_stop_all_jobs_preserves_parallel_pause_grace(self):
        class FakeJob:
            def __init__(self, name, kind, pid):
                self.name = name
                self.kind = kind
                self.popen = SimpleNamespace(pid=pid)
                self.calls = []
                self.running = True

            def alive(self):
                return self.running

            def stop(self, **kwargs):
                self.calls.append(kwargs)
                self.running = False
                return True

        ordinary = FakeJob("ordinary", "runner", 11)
        parallel_job = FakeJob("base", "parallel-supervisor", 22)
        with mock.patch.dict(
                dashboard.JOBS,
                {"ordinary": ordinary, "base": parallel_job}, clear=True):
            dashboard.stop_all_jobs()
        self.assertEqual(ordinary.calls, [{}])
        self.assertEqual(parallel_job.calls, [{
            "force_after": dashboard.PARALLEL_CONTROL_STARTUP_TIMEOUT,
        }])

    def test_nonterminal_guard_does_not_depend_on_pid(self):
        state = _parallel_state(Path("repo"), "blocked")
        state["loop"] = {"pid": None}
        error = dashboard.dashboard_operation_error(state, "edit-config")
        self.assertIn("blocked", error)
        self.assertIn("Pause/Resume/Abort", error)

    def test_plan_edit_is_conflict_even_when_parallel_is_paused(self):
        handler = FakeHandler()
        state = _parallel_state(Path("repo"), "paused")
        with mock.patch.object(dashboard, "_load_state_or_err", return_value=state), \
                mock.patch.object(dashboard, "write_state") as write:
            dashboard.Handler.api_edit_state.__wrapped__(handler, {
                "name": "base", "plan_edit": True, "tasks": [], "plan_version": 1,
            })
        self.assertEqual(handler.responses[0][0], 409)
        self.assertIn("已凍結", handler.responses[0][1]["error"])
        write.assert_not_called()

    def test_worker_is_readonly_and_parallel_controls_are_allowed(self):
        worker = {"runner": "parallel-worker", "managed_readonly": True}
        self.assertIn("readonly", dashboard.dashboard_operation_error(worker, "stop"))
        base = _parallel_state(Path("repo"), "paused")
        self.assertIsNone(dashboard.dashboard_operation_error(
            base, "resume", parallel_action="resume"))

    def test_parallel_resume_rejects_running_before_spawning_control_client(self):
        base = _parallel_state(Path("repo"), "running")
        error = dashboard.dashboard_operation_error(
            base, "resume", parallel_action="resume")
        self.assertIn("paused/blocked", error)

    def test_parallel_finalizing_completion_delegates_resume_to_recovery_owner(self):
        base = _parallel_state(Path("repo"), "finalizing")
        base["parallel"]["terminal_intent"] = "completed"
        self.assertIsNone(dashboard.dashboard_operation_error(
            base, "resume", parallel_action="resume"))

        base["parallel"]["terminal_intent"] = None
        error = dashboard.dashboard_operation_error(
            base, "resume", parallel_action="resume")
        self.assertIn("terminal transition", error)

    def test_parallel_cancel_transition_delegates_resume_to_recovery_owner(self):
        for run_status in ("cancel_requested", "finalizing_cancel"):
            with self.subTest(status=run_status):
                base = _parallel_state(Path("repo"), run_status)
                base["parallel"]["terminal_intent"] = "cancelled"
                self.assertIsNone(dashboard.dashboard_operation_error(
                    base, "resume", parallel_action="resume"))

    def test_repo_alias_guard_finds_managed_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker_repo = root / "worker-repo"
            worker_repo.mkdir()
            worker_dir = root / "parent--task-1"
            worker_dir.mkdir()
            worker = {
                "runner": "parallel-worker", "managed_readonly": True,
                "config": {"repo": str(worker_repo)},
            }
            with mock.patch.object(dashboard, "ROOT", root), \
                    mock.patch.object(dashboard, "read_state", return_value=(worker, None)):
                found = dashboard.managed_workspace_for_repo(worker_repo)
        self.assertEqual(found, "parent--task-1")

    def test_repo_alias_guard_finds_paused_parallel_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            workspace = root / "base"
            workspace.mkdir()
            state = _parallel_state(repo, "paused")
            with mock.patch.object(dashboard, "ROOT", root), \
                    mock.patch.object(dashboard, "read_state", return_value=(state, None)):
                protected = dashboard.protected_workspace_for_repo(repo)
        self.assertEqual(protected[0], "base")
        self.assertEqual(protected[1]["parallel"]["status"], "paused")

    def test_control_job_id_can_be_polled_without_name_or_pid(self):
        job_id = "base:pause:1234abcd"
        job = SimpleNamespace(
            name="base", job_id=job_id, kind="parallel-pause-control",
            popen=SimpleNamespace(pid=4242), alive=lambda: True,
        )
        # Supervisors stay keyed by workspace while exposing a per-spawn id;
        # startup polling must resolve either storage shape by public job_id.
        with mock.patch.dict(dashboard.JOBS, {"base": job}, clear=True):
            projection = dashboard.job_startup_status(job_id=job_id)
        self.assertEqual(projection, {
            "status": "starting", "pid": 4242, "job_id": job_id,
        })

    def test_parallel_resume_response_exposes_unique_pollable_job_id(self):
        handler = FakeHandler()
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            (repo / ".git").mkdir(parents=True)
            state = _parallel_state(repo, "paused")
            process = SimpleNamespace(
                pid=4343, dashboard_job_id="base:resume:feedbeef")
            mutable_extra = Path(directory) / "mutable-dashboard-bin"
            with mock.patch.dict(os.environ, {"PATH": "ambient-path"}, clear=True), \
                    mock.patch.object(
                        dashboard, "load_config",
                        return_value={"extra_path_dirs": [str(mutable_extra)]}) as load, \
                    mock.patch.object(
                        dashboard, "spawn_parallel", return_value=process) as spawn, \
                    mock.patch.object(dashboard, "workspace_console_log"), \
                    mock.patch.dict(dashboard.JOBS, {}, clear=True):
                dashboard.Handler._start_existing_parallel(
                    handler, "base", state)
            load.assert_not_called()
            self.assertEqual(spawn.call_args.kwargs["env"]["PATH"], "ambient-path")
            self.assertNotIn(
                str(mutable_extra), spawn.call_args.kwargs["env"]["PATH"])
        self.assertEqual(handler.responses, [(200, {
            "ok": True, "starting": True, "name": "base", "pid": 4343,
            "job_id": "base:resume:feedbeef", "startup_timeout": 135.0,
        })])

    def test_parallel_control_timeout_covers_full_fail_closed_protocol(self):
        handler = FakeHandler()
        state = _parallel_state(Path("repo"), "running")
        process = SimpleNamespace(
            pid=4444, dashboard_job_id="base:pause:facecafe")
        with mock.patch.object(dashboard, "load_config", return_value={}), \
                mock.patch.object(dashboard, "spawn_parallel", return_value=process), \
                mock.patch.object(dashboard, "workspace_console_log"), \
                mock.patch.dict(dashboard.JOBS, {}, clear=True):
            dashboard.Handler._parallel_control(handler, "base", state, "pause")
        response = handler.responses[0][1]
        self.assertEqual(response["job_id"], "base:pause:facecafe")
        self.assertGreaterEqual(response["startup_timeout"], 110)

    def test_ordinary_import_rejects_parallel_stack_plan(self):
        handler = FakeHandler()
        state = {"runner": "loop", "phase": "plan", "config": {}}
        with mock.patch.object(dashboard, "_load_state_or_err", return_value=state), \
                mock.patch.object(dashboard, "ws_running", return_value=False), \
                mock.patch.object(dashboard, "write_state") as write:
            dashboard.Handler.api_import_plan.__wrapped__(handler, {
                "name": "ordinary",
                "plan_json": json.dumps([
                    {"order": 1, "task": "one", "stack": 1},
                ]),
            })
        self.assertEqual(handler.responses[0][0], 400)
        self.assertIn("普通 Loop", handler.responses[0][1]["error"])
        write.assert_not_called()

    def test_start_command_is_exact_parallel_module_contract(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(dashboard, "ROOT", Path(directory) / "workspaces"):
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            frozen_bin = root / "frozen-bin"
            frozen_bin.mkdir()
            plan = root / "plan.json"
            plan.write_text("[]", encoding="utf-8")
            plan_sha256 = hashlib.sha256(plan.read_bytes()).hexdigest()
            config = _runtime_config(repo)
            config["environment"] = {
                "path_additions": [str(frozen_bin)],
                "non_secret": {"MODE": "test", "TRACE": False},
                "required_secret_names": ["API_TOKEN"],
            }
            with mock.patch.dict(
                    os.environ, {"API_TOKEN": "secret-value"}, clear=False):
                command = dashboard.build_parallel_command(
                    "start", "base", repo=repo, import_plan=plan,
                    config=config, expected_plan_sha256=plan_sha256,
                    expected_primary_ref="refs/heads/main",
                    expected_primary_sha="1" * 40)
        self.assertEqual(command[1:4], ["-m", "engine.parallel", "--workspace-root"])
        self.assertEqual(command[5], "start")
        self.assertEqual(command[command.index("--name") + 1], "base")
        self.assertEqual(command[command.index("--import-plan") + 1], str(plan.resolve()))
        self.assertEqual(
            command[command.index("--expected-plan-sha256") + 1],
            plan_sha256,
        )
        self.assertEqual(
            command[command.index("--expected-primary-ref") + 1],
            "refs/heads/main",
        )
        self.assertEqual(
            command[command.index("--expected-primary-sha") + 1],
            "1" * 40,
        )
        self.assertNotIn("engine.loop", command)
        parsed = parallel.build_argument_parser().parse_args(command[3:])
        self.assertEqual(parsed.command, "start")
        self.assertEqual(parsed.name, "base")
        self.assertEqual(parsed.expected_plan_sha256, plan_sha256)
        self.assertEqual(parsed.expected_primary_ref, "refs/heads/main")
        self.assertEqual(parsed.expected_primary_sha, "1" * 40)
        round_trip = parallel._runtime_config_from_args(parsed, repo.resolve())
        self.assertEqual(round_trip["environment"], {
            "path_additions": [str(frozen_bin.resolve())],
            "non_secret": {"MODE": "test", "TRACE": False},
            "required_secret_names": ["API_TOKEN"],
        })
        self.assertNotIn("secret-value", "\0".join(command))

    def test_parallel_launch_rejects_non_exec_before_side_effects(self):
        handler = FakeHandler()
        with mock.patch.object(dashboard, "spawn_parallel") as spawn:
            dashboard.Handler.api_launch_parallel(handler, {
                "runner": "parallel", "start_phase": "plan", "plan_json": "[]",
            })
        self.assertEqual(handler.responses[0][0], 400)
        self.assertIn("start_phase=exec", handler.responses[0][1]["error"])
        spawn.assert_not_called()

    def test_parallel_launch_writes_frozen_plan_and_spawns_start(self):
        handler = FakeHandler()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_root = root / "workspaces"
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Parallel Dashboard Test"],
                cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "parallel@example.invalid"],
                cwd=repo, check=True)
            (repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
            subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial"], cwd=repo, check=True)
            cfg = {
                "agent_cmds": [{"label": "agent", "cmd": "agent --test"}],
                "validate_cmds": [{"label": "validate", "cmd": "validator --test"}],
                "defaults": {}, "notify_cmd": "",
                "extra_path_dirs": [str(root / "agent-bin")],
            }
            (root / "agent-bin").mkdir()
            with mock.patch.object(dashboard, "ROOT", workspace_root), \
                    mock.patch.object(dashboard, "load_config", return_value=cfg), \
                    mock.patch.object(dashboard, "command_error", return_value=None), \
                    mock.patch.object(dashboard, "spawn_parallel",
                                      return_value=SimpleNamespace(pid=777)) as spawn, \
                    mock.patch.object(
                        dashboard.parallel_state, "_fsync_directory",
                        wraps=dashboard.parallel_state._fsync_directory) as fsync_dir, \
                    mock.patch.dict(dashboard.JOBS, {}, clear=True):
                dashboard.Handler.api_launch_parallel(handler, {
                    "runner": "parallel", "repo": str(repo), "name": "base",
                    "start_phase": "exec", "agent_idx": 0, "validate_idx": 0,
                    "plan_json": json.dumps([
                        {"order": 1, "task": "one", "ref": None, "stack": 1},
                        {"order": 2, "task": "two", "ref": None, "stack": 1},
                    ]),
                })
            plan_path = spawn.call_args.kwargs["import_plan"]
            plan_sha256 = spawn.call_args.kwargs["expected_plan_sha256"]
            raw = plan_path.read_bytes()
            self.assertEqual(len(json.loads(raw.decode("utf-8"))), 2)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), plan_sha256)
            self.assertIn(plan_sha256, plan_path.name)
            self.assertFalse(
                (workspace_root / "base" / "parallel-plan.pending.json").exists())
            fsync_dir.assert_called_once_with(workspace_root / "base")
        self.assertEqual(handler.responses[-1][0], 200)
        self.assertTrue(handler.responses[-1][1]["starting"])
        self.assertEqual(spawn.call_args.kwargs["action"], "start")
        self.assertEqual(spawn.call_args.kwargs["import_plan"], plan_path)
        self.assertEqual(
            spawn.call_args.kwargs["expected_plan_sha256"], plan_sha256)
        self.assertTrue(
            spawn.call_args.kwargs["expected_primary_ref"].startswith(
                "refs/heads/"))
        self.assertRegex(
            spawn.call_args.kwargs["expected_primary_sha"],
            r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
        self.assertEqual(
            spawn.call_args.kwargs["config"]["environment"], {
                "path_additions": [str((root / "agent-bin").resolve())],
                "non_secret": {}, "required_secret_names": [],
            })

    def test_parallel_staging_race_is_unique_and_raw_hash_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "base"
            workspace.mkdir()
            plans = [
                [{"order": 1, "task": "first", "ref": None, "stack": 1}],
                [{"order": 1, "task": "second", "ref": None, "stack": 1}],
            ]
            barrier = threading.Barrier(2)

            def stage(plan):
                barrier.wait(timeout=5)
                return dashboard._stage_parallel_plan(workspace, plan)

            with ThreadPoolExecutor(max_workers=2) as executor:
                staged = list(executor.map(stage, plans))

            (first_path, first_hash), (second_path, second_hash) = staged
            self.assertNotEqual(first_path, second_path)
            self.assertNotEqual(first_hash, second_hash)
            self.assertFalse((workspace / "parallel-plan.pending.json").exists())
            for path, digest in staged:
                raw = path.read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
                self.assertIn(digest, path.name)

            # Even if a caller accidentally swaps the two staging paths, the
            # raw-byte digest from its own submission prevents plan exchange.
            with self.assertRaisesRegex(parallel.ParallelError, "SHA-256 mismatch"):
                parallel.load_frozen_plan(
                    second_path, expected_raw_sha256=first_hash)

    def test_parallel_base_checkpoint_is_startup_handshake(self):
        state = _parallel_state(Path("repo"), "running")
        state["loop"] = {"pid": 4242}
        job = SimpleNamespace(
            popen=SimpleNamespace(pid=4242), alive=lambda: True)
        with mock.patch.dict(dashboard.JOBS, {"base": job}, clear=True), \
                mock.patch.object(dashboard, "read_state", return_value=(state, None)):
            projection = dashboard.job_startup_status("base", "4242")
        self.assertEqual(projection, {"status": "ready", "pid": 4242})

    def test_parallel_initial_checkpoint_is_not_ready_before_preflight(self):
        state = _parallel_state(Path("repo"), "initializing")
        state["loop"] = {"pid": 4242}
        job = SimpleNamespace(popen=SimpleNamespace(pid=4242), alive=lambda: True)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(dashboard, "ROOT", Path(directory)), \
                mock.patch.dict(dashboard.JOBS, {"base": job}, clear=True), \
                mock.patch.object(dashboard, "read_state", return_value=(state, None)):
            projection = dashboard.job_startup_status("base", "4242")
        self.assertEqual(projection, {"status": "starting"})

    def test_discovery_includes_managed_worker_but_health_counts_only_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("base", "base--task"):
                path = root / name
                path.mkdir()
                (path / "state.json").write_text("{}", encoding="utf-8")
            base = _parallel_state(root / "repo", "paused")
            worker = {
                "runner": "parallel-worker", "managed_readonly": True,
                "parent_workspace": "base", "run_id": "a" * 32,
                "assigned_order": 1, "phase": "exec", "round": 2,
                "loop": {"pid": None}, "config": {"repo": str(root / "worker-repo")},
                "plan": base["plan"], "completed": [], "issues": [],
            }
            with mock.patch.object(dashboard, "ROOT", root), \
                    mock.patch.object(dashboard, "read_state", side_effect=lambda name, **_:
                                      ((base, None) if name == "base" else (worker, None))), \
                    mock.patch.object(dashboard, "ws_running", return_value=False):
                fleet = dashboard.list_workspaces()
                health = dashboard.fleet_health_projection(fleet)
        self.assertEqual([item["name"] for item in fleet], ["base", "base--task"])
        self.assertEqual(fleet[0]["runner"], "parallel-supervisor")
        self.assertEqual(fleet[0]["parallel"]["status"], "paused")
        self.assertEqual(fleet[1]["runner"], "parallel-worker")
        self.assertTrue(fleet[1]["managed_readonly"])
        self.assertEqual(fleet[1]["plan_len"], 1)
        self.assertEqual(health["workspace_count"], 1)
        self.assertEqual(health["running"], 0)

    def test_discovery_advertises_no_owner_terminal_transition_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "base"
            path.mkdir()
            (path / "state.json").write_text("{}", encoding="utf-8")
            state = _parallel_state(root / "repo", "finalizing")
            state["parallel"]["terminal_intent"] = "completed"
            with mock.patch.object(dashboard, "ROOT", root), \
                    mock.patch.object(
                        dashboard, "read_state", return_value=(state, None)), \
                    mock.patch.object(dashboard, "ws_running", return_value=False):
                self.assertTrue(dashboard.list_workspaces()[0]["resume_available"])

            state["parallel"]["status"] = "finalizing_cancel"
            state["parallel"]["terminal_intent"] = "cancelled"
            with mock.patch.object(dashboard, "ROOT", root), \
                    mock.patch.object(
                        dashboard, "read_state", return_value=(state, None)), \
                    mock.patch.object(dashboard, "ws_running", return_value=False):
                self.assertTrue(dashboard.list_workspaces()[0]["resume_available"])


class TestParallelStatusProjection(unittest.TestCase):
    def test_managed_worker_projection_exposes_only_assigned_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            (root / "child").mkdir(parents=True)
            state = {
                    "runner": "parallel-worker",
                    "managed_readonly": True,
                    "parent_workspace": "base",
                    "run_id": "a1b2c3d4",
                    "assigned_order": 2,
                    "current_order": 2,
                    "phase": "exec",
                    "plan": [
                        {"order": 1, "task": "one", "ref": None, "stack": 1},
                        {"order": 2, "task": "two", "ref": None, "stack": 1},
                        {"order": 3, "task": "three", "ref": None, "stack": 2},
                    ],
                    "completed": [{"order": 1, "sha": "1" * 40}],
                    "assignment": {"status": "running"},
                }
            with mock.patch.object(status.loop, "WORKSPACE_ROOT", root), \
                    mock.patch.object(
                        status.loop, "load_checkpointed_state",
                        return_value=(state, b"", False)), \
                    mock.patch.object(
                        status.loop, "active_run_lock_owner", return_value={}):
                projection = status.project_status("child")
        self.assertEqual(projection["plan_len"], 1)
        self.assertEqual(projection["completed"], 0)
        self.assertEqual(projection["current_order"], 2)
        self.assertEqual(projection["current_task"], "two")

    def test_legacy_readonly_marker_canonicalizes_worker_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            (root / "child").mkdir(parents=True)
            state = {
                "runner": "loop", "managed_readonly": True,
                "parent_workspace": "base", "run_id": "a1b2c3d4",
                "assigned_order": 2, "current_order": 2, "phase": "exec",
                "plan": [
                    {"order": 1, "task": "one", "ref": None, "stack": 1},
                    {"order": 2, "task": "two", "ref": None, "stack": 1},
                ],
                "completed": [], "assignment": {"status": "running"},
            }
            with mock.patch.object(status.loop, "WORKSPACE_ROOT", root), \
                    mock.patch.object(
                        status.loop, "load_checkpointed_state",
                        return_value=(state, b"", False)), \
                    mock.patch.object(
                        status.loop, "active_run_lock_owner", return_value={}):
                projection = status.project_status("child")
        self.assertEqual(projection["runner"], "parallel-worker")
        self.assertEqual(projection["plan_len"], 1)
        self.assertEqual(projection["current_order"], 2)
        self.assertEqual(projection["current_task"], "two")

    def test_fleet_summary_excludes_worker_projection(self):
        summary = status.summarize_status([
            {"name": "base", "runner": "parallel-supervisor", "phase": "exec",
             "plan_len": 2, "completed": 1, "running": False,
             "parallel": {"status": "paused"}},
            {"name": "child", "runner": "parallel-worker", "managed_readonly": True,
             "phase": "exec", "plan_len": 2, "completed": 0, "running": False},
        ])
        self.assertEqual(summary["workspace_count"], 1)
        self.assertEqual(summary["tasks_total"], 2)


if __name__ == "__main__":
    unittest.main()
